"""Rank the largest Binance spot coins by technicals and a crypto market score.

Crypto has no financial statements, so the second score is about standing in
the market instead: market-cap rank, strength against BTC, and USD turnover.
Candles come from Binance itself (the USDT pair that would be traded) and fall
back to Yahoo's USD prices when Binance cannot be reached; market caps come
from Yahoo either way. Daily bars run on UTC days; the bar of the day still
trading is dropped, so every figure is from completed days.
"""

import logging
from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

from idx_bot import binance_feed, config, indodax_feed
from idx_bot.indicators import atr
from idx_bot.risk import plan_trade
from idx_bot.screener import TECH_WEIGHT, technical_score


def symbol(code: str) -> str:
    return f"{code}-USD"


def complete_days(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop the UTC day still in progress."""
    today = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return df[df.index.strftime("%Y-%m-%d") < today]


logger = logging.getLogger(__name__)


def fetch(codes: list[str], period: str = "1y") -> tuple[dict[str, pd.DataFrame], str]:
    """Completed daily candles per code and where they came from ("Binance" or "Yahoo")."""
    if not codes:
        return {}, "Binance"
    days = {"3mo": 100, "1y": 400}[period]
    try:
        api = binance_feed.client()
        return {c: complete_days(binance_feed.klines(api, c, days)) for c in codes}, "Binance"
    except Exception as exc:  # unreachable or refused: Yahoo has the same coins in USD
        logger.warning("Binance klines unavailable (%s); using Yahoo", exc)
    return _yahoo(codes, period), "Yahoo"


def fetch_rupiah(codes: list[str], period: str = "1y",
                 rules: dict | None = None) -> tuple[dict[str, pd.DataFrame], str]:
    """Completed daily rupiah candles from Indodax, the market traded. A coin
    Indodax has no candles for is left out. When Indodax cannot be reached at
    all, Binance (or Yahoo) prices and volume times the USD/IDR rate."""
    if not codes:
        return {}, "Indodax"
    days = {"3mo": 100, "1y": 400}[period]
    prices = {}
    for c in codes:
        try:
            symbols = (rules or {}).get(c, {}).get("chart_symbols")
            prices[c] = complete_days(indodax_feed.klines(c, days, symbols))
        except LookupError as exc:
            logger.warning("%s", exc)
        except Exception as exc:
            logger.warning("Indodax candles unavailable (%s); converting USD prices", exc)
            break
    else:
        return prices, "Indodax"
    prices, source = fetch(codes, period)
    fx = usd_idr()
    converted = {}
    for code, df in in_currency(prices, fx).items():
        df["Volume"] = df["Volume"] * fx
        converted[code] = df
    return converted, f"{source} x kurs USD/IDR"


def local_premium(local: dict[str, pd.DataFrame], world: dict[str, pd.DataFrame], fx: float) -> dict[str, float]:
    """How far each coin's latest local close sits from the world close of the same
    day times ``fx`` (0.05 = 5% dearer locally)."""
    out = {}
    for code, df in local.items():
        ref = world.get(code)
        if ref is None or df.empty:
            continue
        day = df.index[-1].strftime("%Y-%m-%d")
        same = ref[ref.index.strftime("%Y-%m-%d") == day]
        if not same.empty:
            out[code] = round(float(df["Close"].iloc[-1] / (same["Close"].iloc[-1] * fx) - 1), 4)
    return out


def _yahoo(codes: list[str], period: str) -> dict[str, pd.DataFrame]:
    symbols = [symbol(c) for c in codes]
    data = yf.download(symbols, period=period, interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    present = set(data.columns.get_level_values(0))
    return {c: complete_days(data[s].dropna(subset=["Close"]))
            for c, s in zip(codes, symbols, strict=True) if s in present}


def usd_idr() -> float:
    """Latest USD/IDR rate from Yahoo."""
    close = yf.download("IDR=X", period="10d", progress=False, auto_adjust=True)["Close"].squeeze().dropna()
    if close.empty:
        raise RuntimeError("USD/IDR rate unavailable")
    return float(close.iloc[-1])


def in_currency(prices: dict[str, pd.DataFrame], fx: float) -> dict[str, pd.DataFrame]:
    """Prices times ``fx``; Volume stays in USD, since liquidity is judged in USD."""
    out = {}
    for code, df in prices.items():
        df = df.copy()
        df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]] * fx
        out[code] = df
    return out


def market_score(code: str, df: pd.DataFrame, btc: pd.DataFrame, rank: int,
                 tiers: tuple[float, float] = (5e8, 1e8)) -> tuple[int, dict]:
    """``tiers`` are the average daily turnovers, in the candles' currency, worth 30
    and 15 volume points: USD on Binance by default."""
    close, btc_close = df["Close"], btc["Close"]
    ret_30 = close.iloc[-1] / close.iloc[-31] - 1
    ret_90 = close.iloc[-1] / close.iloc[-91] - 1
    rs_30 = ret_30 - (btc_close.iloc[-1] / btc_close.iloc[-31] - 1)
    rs_90 = ret_90 - (btc_close.iloc[-1] / btc_close.iloc[-91] - 1)
    volume = df["Volume"].tail(20).mean()

    score = 30 if rank <= 3 else 20 if rank <= 8 else 10
    if code == "BTC":
        score += 12 + 8  # strength against itself is neutral
    else:
        score += (25 if rs_30 > 0 else 0) + (15 if rs_90 > 0 else 0)
    score += 30 if volume >= tiers[0] else 15 if volume >= tiers[1] else 0
    return score, {"rank": rank, "rs_btc_30d": round(float(rs_30), 4),
                   "rs_btc_90d": round(float(rs_90), 4), "volume": float(volume)}


def screen(pool: list[str] = config.CRYPTO_POOL, top_n: int = config.CRYPTO_TOP_N,
           market: config.Market = config.INDODAX) -> dict:
    rules, tiers = {}, (5e8, 1e8)
    if market.currency == "Rp":
        tiers = config.INDODAX_VOLUME_TIERS
        try:
            listed = indodax_feed.trading_rules()
        except Exception as exc:  # without the pair list, keep the pool and the market's default steps
            logger.warning("Indodax pair list unavailable (%s)", exc)
        else:
            pool = [c for c in pool if c in listed and not listed[c]["suspended"]]
            rules = {c: listed[c] for c in pool}
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
    premium = {}
    if market.currency == "Rp":
        prices, source = fetch_rupiah(universe, rules=rules)
        if source == "Indodax":
            world, _ = fetch(universe)
            premium = local_premium(prices, world, usd_idr())
    else:
        prices, source = fetch(universe)
        if source == "Binance":
            api = binance_feed.client()
            rules = {code: binance_feed.trading_rules(api, code) for code in universe}
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
        mkt, mkt_d = market_score(code, df, btc, rank, tiers)
        last_move = df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1
        atr_value = float(atr(df).iloc[-1])
        flags = []
        if abs(last_move) >= market.extreme_move:
            flags.append(f"pergerakan harian ekstrem {last_move:+.1%}: cek berita")
        if code in premium and abs(premium[code]) > config.MAX_LOCAL_PREMIUM:
            flags.append(f"harga Indodax {premium[code]:+.1%} dari harga global: pasar tipis")
        if mkt_d["volume"] < market.min_daily_value:
            unit = "Rp" if market.currency == "Rp" else "USD"
            flags.append(f"volume harian < {unit} {market.min_daily_value / 1e6:,.0f} juta")
        rows.append({
            "code": code,
            "name": names[code],
            "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "score": round(TECH_WEIGHT * tech + (1 - TECH_WEIGHT) * mkt, 1),
            "technical_score": tech,
            "market_score": mkt,
            "last_move": round(float(last_move), 4),
            "avg_daily_value": mkt_d["volume"],
            "local_premium": premium.get(code),
            "market_cap": caps[code],
            "technical": tech_d,
            "market": mkt_d,
            "atr": atr_value,
            "rules": rules.get(code),
            "plan": plan_trade(float(df["Close"].iloc[-1]), atr_value, market=market, rules=rules.get(code)),
            "flags": flags,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    eligible = [r for r in rows
                if r["avg_daily_value"] >= market.min_daily_value
                and r["technical"]["close"] > r["technical"]["sma50"]
                and abs(r["last_move"]) < market.extreme_move
                and abs(r["local_premium"] or 0) <= config.MAX_LOCAL_PREMIUM
                and r["plan"]["lots"] > 0]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "universe": f"Top {top_n} kripto {'Indodax' if market.currency == 'Rp' else 'Binance'} (market cap)",
        "price_source": source,
        "settings": {"equity": market.equity, "currency": market.currency,
                     "risk_per_trade": config.RISK_PER_TRADE,
                     "monthly_risk_cap": config.MONTHLY_RISK_CAP, "tech_weight": TECH_WEIGHT},
        "candidates": [r["code"] for r in eligible[:config.TOP_N]],
        "rows": rows,
        "skipped": skipped,
    }
