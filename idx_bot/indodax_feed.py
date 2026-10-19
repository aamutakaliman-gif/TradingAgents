"""Indodax public data: daily rupiah candles and each pair's trading rules.

Public endpoints only, no API key. Candles come from the chart feed the
Indodax site uses; they run on UTC days, like Binance's.
"""

import time

import pandas as pd
import requests

BASE = "https://indodax.com"


def _get(path: str, **params) -> object:
    resp = requests.get(BASE + path, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def trading_rules() -> dict[str, dict]:
    """Per coin code (e.g. "BTC"): quantity step, price step, minimum order value
    in rupiah, taker fee and whether trading is suspended, for its IDR pair."""
    rules = {}
    for p in _get("/api/pairs"):
        if p.get("base_currency") != "idr":
            continue
        rules[p["traded_currency"].upper()] = {
            "step": float(p["quantity_increment"]),
            "tick": float(p["price_precision"]),
            "min_notional": float(p["trade_min_base_currency"]),
            "taker_fee": float(p["trade_fee_percent_taker"]) / 100,
            "suspended": bool(p.get("is_market_suspended") or p.get("is_maintenance")),
            # The chart feed still knows some pairs by an old code (XLM is STRIDR there).
            "chart_symbols": [p["symbol"]] + ([p["old_ticker_id"].replace("_", "").upper()]
                                              if p.get("old_ticker_id") else []),
        }
    return rules


def klines(code: str, days: int = 400, symbols: list[str] | None = None) -> pd.DataFrame:
    """Daily candles of ``code``/IDR, indexed by UTC day. ``Volume`` is the day's
    turnover in rupiah (coins traded times the close). ``symbols`` are the chart
    codes to try in order (``chart_symbols`` from trading_rules)."""
    now = int(time.time())
    rows = []
    for sym in symbols or [f"{code}IDR"]:
        rows = _get("/tradingview/history_v2", symbol=sym, tf="1D", **{"from": now - days * 86_400, "to": now})
        if rows:
            break
    if not rows:
        raise LookupError(f"Indodax has no daily candles for {code}")
    df = pd.DataFrame([{
        "Date": pd.to_datetime(r["Time"], unit="s", utc=True),
        "Open": float(r["Open"]), "High": float(r["High"]), "Low": float(r["Low"]), "Close": float(r["Close"]),
        "Volume": float(r["Volume"]) * float(r["Close"]),
    } for r in rows])
    return df.set_index("Date")
