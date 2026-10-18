"""Rank the largest Binance spot coins by technicals and a crypto market score.

Crypto has no financial statements, so the second score is about standing in
the market instead: market-cap rank, strength against BTC, and USD turnover.
Yahoo's daily crypto bars run on UTC days; the bar of the day still trading is
dropped, so every figure is from completed days.
"""

from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

from idx_bot import config
from idx_bot.indicators import atr
from idx_bot.risk import plan_trade
from idx_bot.screener import TECH_WEIGHT, technical_score


def symbol(code: str) -> str:
    return f"{code}-USD"


def complete_days(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop the UTC day still in progress."""
    today = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return df[df.index.strftime("%Y-%m-%d") < today]


def fetch(codes: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    if not codes:
        return {}
    symbols = [symbol(c) for c in codes]
    data = yf.download(symbols, period=period, interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    present = set(data.columns.get_level_values(0))
    return {c: complete_days(data[s].dropna(subset=["Close"]))
            for c, s in zip(codes, symbols, strict=True) if s in present}


def market_score(code: str, df: pd.DataFrame, btc: pd.DataFrame, rank: int) -> tuple[int, dict]:
    close, btc_close = df["Close"], btc["Close"]
    ret_30 = close.iloc[-1] / close.iloc[-31] - 1
    ret_90 = close.iloc[-1] / close.iloc[-91] - 1
    rs_30 = ret_30 - (btc_close.iloc[-1] / btc_close.iloc[-31] - 1)
    rs_90 = ret_90 - (btc_close.iloc[-1] / btc_close.iloc[-91] - 1)
    volume = df["Volume"].tail(20).mean()  # Yahoo quotes crypto volume in USD

    score = 30 if rank <= 3 else 20 if rank <= 8 else 10
    if code == "BTC":
        score += 12 + 8  # strength against itself is neutral
    else:
        score += (25 if rs_30 > 0 else 0) + (15 if rs_90 > 0 else 0)
    score += 30 if volume >= 1e9 else 15 if volume >= 2e8 else 0
    return score, {"rank": rank, "rs_btc_30d": round(float(rs_30), 4),
                   "rs_btc_90d": round(float(rs_90), 4), "volume_usd": float(volume)}


def screen(pool: list[str] = config.CRYPTO_POOL, top_n: int = config.CRYPTO_TOP_N) -> dict:
    market = config.CRYPTO
    caps, names = {}, {}
    for code in pool:
        try:
            info = yf.Ticker(symbol(code)).info
        except Exception:  # a coin Yahoo cannot describe is left out of the ranking
            continue
        if info.get("marketCap"):
            caps[code] = info["marketCap"]
            names[code] = info.get("name") or info.get("shortName") or code
    universe = sorted(caps, key=caps.get, reverse=True)[:top_n]
    prices = fetch(universe)
    btc = prices.get("BTC")
    if btc is None:
        raise RuntimeError("BTC price history unavailable; crypto screen cannot score strength against BTC")

    rows, skipped = [], []
    for rank, code in enumerate(universe, start=1):
        df = prices.get(code)
        if df is None or len(df) < 200:
            skipped.append({"code": code, "reason": "data harga kurang dari 200 hari"})
            continue
        tech, tech_d = technical_score(df)
        mkt, mkt_d = market_score(code, df, btc, rank)
        last_move = df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1
        atr_value = float(atr(df).iloc[-1])
        flags = []
        if abs(last_move) >= market.extreme_move:
            flags.append(f"pergerakan harian ekstrem {last_move:+.1%}: cek berita")
        if mkt_d["volume_usd"] < market.min_daily_value:
            flags.append("volume harian < USD 50 juta")
        rows.append({
            "code": code,
            "name": names[code],
            "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "score": round(TECH_WEIGHT * tech + (1 - TECH_WEIGHT) * mkt, 1),
            "technical_score": tech,
            "market_score": mkt,
            "last_move": round(float(last_move), 4),
            "avg_daily_value": mkt_d["volume_usd"],
            "market_cap": caps[code],
            "technical": tech_d,
            "market": mkt_d,
            "atr": atr_value,
            "plan": plan_trade(float(df["Close"].iloc[-1]), atr_value, market=market),
            "flags": flags,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    eligible = [r for r in rows
                if r["avg_daily_value"] >= market.min_daily_value
                and r["technical"]["close"] > r["technical"]["sma50"]
                and abs(r["last_move"]) < market.extreme_move
                and r["plan"]["lots"] > 0]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "universe": f"Top {top_n} kripto Binance (market cap)",
        "settings": {"equity": market.equity, "currency": market.currency,
                     "risk_per_trade": config.RISK_PER_TRADE,
                     "monthly_risk_cap": config.MONTHLY_RISK_CAP, "tech_weight": TECH_WEIGHT},
        "candidates": [r["code"] for r in eligible[:config.TOP_N]],
        "rows": rows,
        "skipped": skipped,
    }
