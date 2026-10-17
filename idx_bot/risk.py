"""Trade sizing under Elder's 2% and 6% rules, in IDX lots and tick sizes."""

import math

from idx_bot import config


def tick_size(price: float) -> int:
    """IDX price fraction for a stock trading at ``price``."""
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_down_to_tick(price: float) -> float:
    tick = tick_size(price)
    return math.floor(price / tick) * tick


def round_up_to_tick(price: float) -> float:
    tick = tick_size(price)
    return math.ceil(price / tick) * tick


def plan_trade(entry: float, atr_value: float, equity: float = config.EQUITY,
               cash: float | None = None, open_risk: float = 0.0,
               month_loss: float = 0.0) -> dict:
    """Stop, target and lot count for a long entry at ``entry``.

    ``open_risk`` is what the open positions lose if every stop is hit and
    ``month_loss`` the month's realised losses, both in IDR. The 6% rule caps
    their sum plus this trade's risk; ``lots`` is 0 when no trade fits.
    """
    cash = equity if cash is None else cash
    stop = round_down_to_tick(entry - config.ATR_STOP_MULTIPLE * atr_value)
    risk_per_share = entry - stop
    target = round_up_to_tick(entry + config.REWARD_RISK * risk_per_share)

    budget = min(equity * config.RISK_PER_TRADE,
                 equity * config.MONTHLY_RISK_CAP - open_risk - month_loss)
    lots = 0
    if budget > 0 and risk_per_share > 0 and stop > 0:
        lot_cost = entry * config.LOT_SIZE * (1 + config.BUY_FEE)
        lots = min(int(budget // (risk_per_share * config.LOT_SIZE)), int(cash // lot_cost))
    shares = lots * config.LOT_SIZE
    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "lots": lots,
        "position_value": shares * entry,
        "risk_idr": shares * risk_per_share,
        "stop_pct": risk_per_share / entry,
    }
