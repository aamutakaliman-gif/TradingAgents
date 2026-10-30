"""Rehearse the crypto paper trades on Binance Spot Testnet: real order flow, play money.

This module only ever builds a testnet client. Its keys come from the
environment variables BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET,
made at testnet.binance.vision; without them the rehearsal is skipped.

Testnet prices drift away from the real market, so each bracket is placed at
the paper trade's stop and target *distances* around the testnet fill. The
paper ledger stays the record of performance; the testnet shows whether the
orders themselves go through: sizes, price steps, signatures and the OCO.
"""

import os
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from binance.client import Client

from idx_bot import binance_feed

STOP_LIMIT_SLACK = 0.005  # the stop's limit price sits 0.5% under its trigger, so it still fills in a fast fall


def connect() -> Client | None:
    key, secret = os.environ.get("BINANCE_TESTNET_API_KEY"), os.environ.get("BINANCE_TESTNET_API_SECRET")
    if not key or not secret:
        return None
    return Client(key, secret, testnet=True, ping=False, requests_params={"timeout": 10})


def _step(x: float, step: float, rounding: str) -> Decimal:
    d = Decimal(str(step))
    return (Decimal(str(x)) / d).to_integral_value(rounding=rounding) * d


def _text(d: Decimal) -> str:
    return format(d.normalize(), "f")


def open_bracket(api: Client, pos: dict) -> dict:
    """Market-buy ``pos['units']`` of the coin, then an OCO sell: a LIMIT_MAKER at
    the target above and a STOP_LOSS_LIMIT at the stop below."""
    code, symbol = pos["code"], f"{pos['code']}USDT"
    rules = binance_feed.trading_rules(api, code)
    if rules is None:
        raise ValueError(f"{symbol} tidak ada di testnet")
    qty = _step(pos["units"], rules["step"], ROUND_FLOOR)
    if qty <= 0:
        raise ValueError(f"jumlah {pos['units']} di bawah kelipatan {rules['step']}")
    buy = api.order_market_buy(symbol=symbol, quantity=_text(qty), newOrderRespType="FULL")
    filled = Decimal(buy["executedQty"])
    fill = float(Decimal(buy["cummulativeQuoteQty"]) / filled)

    stop_pct = (pos["entry"] - pos["stop"]) / pos["entry"]
    reward = (pos["target"] - pos["entry"]) / pos["entry"]
    stop = _step(fill * (1 - stop_pct), rules["tick"], ROUND_FLOOR)
    limit = _step(float(stop) * (1 - STOP_LIMIT_SLACK), rules["tick"], ROUND_FLOOR)
    target = _step(fill * (1 + reward), rules["tick"], ROUND_CEILING)
    sell_qty = _step(float(filled), rules["step"], ROUND_FLOOR)
    oco = api.create_oco_order(
        symbol=symbol, side="SELL", quantity=_text(sell_qty),
        aboveType="LIMIT_MAKER", abovePrice=_text(target),
        belowType="STOP_LOSS_LIMIT", belowStopPrice=_text(stop), belowPrice=_text(limit),
        belowTimeInForce="GTC",
    )
    return {
        "symbol": symbol, "qty": float(sell_qty), "fill": fill,
        "stop": float(stop), "target": float(target),
        "buy_id": buy["orderId"], "oco_ids": [o["orderId"] for o in oco["orders"]],
        "status": "open",
    }


def _leg_filled(api: Client, rec: dict) -> dict | None:
    for order_id in rec["oco_ids"]:
        order = api.get_order(symbol=rec["symbol"], orderId=order_id)
        if order["status"] == "FILLED":
            return order
    return None


def close(api: Client, rec: dict) -> None:
    """Cancel the bracket (cancelling one leg cancels the OCO) and sell at market."""
    api.cancel_order(symbol=rec["symbol"], orderId=rec["oco_ids"][0])
    api.order_market_sell(symbol=rec["symbol"], quantity=_text(Decimal(str(rec["qty"]))))
    rec["status"] = "ditutup mengikuti paper"


def sync(api: Client, ledger: dict) -> list[str]:
    """Open a testnet bracket for each new paper position, record brackets the
    testnet has already closed, and close the testnet side of positions the paper
    ledger has closed. A failure on one coin is reported and the rest carry on."""
    events = []
    for pos in ledger["open"]:
        try:
            rec = pos.get("testnet")
            if rec is None:
                pos["testnet"] = rec = open_bracket(api, pos)
                events.append(f"testnet {pos['code']}: beli {rec['qty']:g} di {rec['fill']:,.6g}, "
                              f"OCO stop {rec['stop']:,.6g} / target {rec['target']:,.6g}")
            elif rec["status"] == "open" and (leg := _leg_filled(api, rec)):
                rec["status"] = f"ditutup di testnet ({leg['type']})"
                events.append(f"testnet {pos['code']}: {rec['status']}")
        except Exception as exc:
            pos["testnet"] = {"status": "gagal", "error": str(exc)[:200]}
            events.append(f"testnet {pos['code']}: GAGAL ({str(exc)[:120]})")
    for trade in ledger["closed"]:
        rec = trade.get("testnet")
        if not rec or rec.get("status") != "open":
            continue
        try:
            if leg := _leg_filled(api, rec):
                rec["status"] = f"ditutup di testnet ({leg['type']})"
            else:
                close(api, rec)
            events.append(f"testnet {trade['code']}: {rec['status']}")
        except Exception as exc:
            rec["status"] = "gagal ditutup"
            rec["error"] = str(exc)[:200]
            events.append(f"testnet {trade['code']}: GAGAL menutup ({str(exc)[:120]})")
    return events
