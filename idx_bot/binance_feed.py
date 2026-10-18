"""Binance spot data through python-binance: daily klines and each pair's trading rules.

Public endpoints only, so no API key is needed. ``crypto.fetch`` falls back to
Yahoo when Binance cannot be reached.
"""

from decimal import Decimal

import pandas as pd
from binance.client import Client

# Binance's public market-data mirror. api.binance.com answers 451 from cloud
# regions Binance does not serve; this host carries the same public endpoints.
DATA_API_URL = "https://data-api.binance.vision/api"


def client() -> Client:
    # ping=False: the constructor would otherwise call Binance before we decide to.
    api = Client(ping=False, requests_params={"timeout": 10})
    api.API_URL = DATA_API_URL
    return api


def klines(api: Client, code: str, days: int = 400) -> pd.DataFrame:
    """Daily candles of ``code``/USDT, indexed by UTC day. ``Volume`` is the
    USDT turnover (Binance's quote volume), matching Yahoo's USD volume."""
    rows = api.get_klines(symbol=f"{code}USDT", interval=Client.KLINE_INTERVAL_1DAY, limit=min(days, 1000))
    df = pd.DataFrame([{
        "Date": pd.to_datetime(r[0], unit="ms", utc=True),
        "Open": float(r[1]), "High": float(r[2]), "Low": float(r[3]), "Close": float(r[4]),
        "Volume": float(r[7]),
    } for r in rows])
    return df.set_index("Date")


def trading_rules(api: Client, code: str) -> dict | None:
    """Quantity step, price step and minimum order value of ``code``/USDT, or None
    when Binance does not list the pair."""
    info = api.get_symbol_info(f"{code}USDT")
    if not info:
        return None
    filters = {f["filterType"]: f for f in info["filters"]}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return {
        "step": float(Decimal(filters["LOT_SIZE"]["stepSize"]).normalize()),
        "tick": float(Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize()),
        "min_notional": float(notional.get("minNotional", 0)),
    }
