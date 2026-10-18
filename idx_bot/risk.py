"""Trade sizing under Elder's 2% and 6% rules, in each market's lots and price steps."""

import math

from idx_bot import config


def tick_size(price: float, market: config.Market = config.IDX) -> float:
    """Price step at ``price``: BEI's fractions on IDX, five significant digits for crypto."""
    if market.ticks == "sig5":
        return 10.0 ** (math.floor(math.log10(price)) - 4)
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_down_to_tick(price: float, market: config.Market = config.IDX) -> float:
    tick = tick_size(price, market)
    return round(math.floor(price / tick) * tick, 10)


def round_up_to_tick(price: float, market: config.Market = config.IDX) -> float:
    tick = tick_size(price, market)
    return round(math.ceil(price / tick) * tick, 10)


def plan_trade(entry: float, atr_value: float, equity: float | None = None,
               cash: float | None = None, open_risk: float = 0.0,
               month_loss: float = 0.0, market: config.Market = config.IDX) -> dict:
    """Stop, target and lot count for a long entry at ``entry``.

    ``open_risk`` is what the open positions lose if every stop is hit and
    ``month_loss`` the month's realised losses, both in the market's currency.
    The 6% rule caps their sum plus this trade's risk; ``lots`` is 0 when no
    trade fits. ``units`` is what is bought: shares on IDX, coins for crypto.
    """
    equity = market.equity if equity is None else equity
    cash = equity if cash is None else cash
    stop = round_down_to_tick(entry - config.ATR_STOP_MULTIPLE * atr_value, market)
    risk_per_unit = entry - stop
    target = round_up_to_tick(entry + config.REWARD_RISK * risk_per_unit, market)

    budget = min(equity * config.RISK_PER_TRADE,
                 equity * config.MONTHLY_RISK_CAP - open_risk - month_loss)
    lots = 0
    if budget > 0 and risk_per_unit > 0 and stop > 0:
        lot_cost = entry * market.lot_size * (1 + market.buy_fee)
        lots = min(int(budget // (risk_per_unit * market.lot_size)), int(cash // lot_cost))
    units = round(lots * market.lot_size, 8)
    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "lots": lots,
        "units": units,
        "position_value": units * entry,
        "risk": units * risk_per_unit,
        "stop_pct": risk_per_unit / entry,
    }
