"""Rank the LQ45 by technical and fundamental scores, and size a trade for each.

Run with ``python -m idx_bot.screener [--out PATH]``. Scores are 0-100; the
combined score weights technicals 60/40 because the trial is swing trading.
"""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from idx_bot import config
from idx_bot.indicators import adx, atr, bollinger, macd_hist, rsi, sma, stochastic
from idx_bot.risk import plan_trade

logger = logging.getLogger(__name__)

TECH_WEIGHT = 0.6
EXTREME_MOVE = 0.10  # a daily move this large is flagged for a manual check


_ADX_TREND = 25        # ADX above this marks a genuine trend rather than chop
_STOCH_OVERBOUGHT = 80  # a fresh entry into this zone warns of a stretched move


def _signals(pct_b: float, stoch_k: float, stoch_d: float, adx_v: float) -> dict:
    """Plain-language confirmation notes from Bollinger %B, Stochastic and ADX."""
    def nan(x):
        return None if x != x else round(x, 1)
    notes = []
    if adx_v == adx_v:
        notes.append("tren kuat (ADX > 25)" if adx_v >= _ADX_TREND else "tren lemah/sideways (ADX < 25)")
    if pct_b == pct_b:
        if pct_b > 1:
            notes.append("harga di atas pita Bollinger atas: mungkin jenuh beli")
        elif pct_b < 0:
            notes.append("harga di bawah pita Bollinger bawah: mungkin jenuh jual")
    if stoch_k == stoch_k and stoch_k >= _STOCH_OVERBOUGHT:
        notes.append("Stochastic di zona jenuh beli (>80)")
    return {"bb_pct_b": nan(pct_b), "stoch_k": nan(stoch_k), "stoch_d": nan(stoch_d),
            "adx": nan(adx_v), "notes": notes}


def technical_score(df: pd.DataFrame) -> tuple[int, dict]:
    close = df["Close"]
    last = close.iloc[-1]
    s20, s50, s200 = sma(close, 20).iloc[-1], sma(close, 50).iloc[-1], sma(close, 200).iloc[-1]
    r = rsi(close).iloc[-1]
    hist = macd_hist(close)
    ret_1m = last / close.iloc[-22] - 1
    ret_3m = last / close.iloc[-64] - 1
    vol_ratio = df["Volume"].tail(20).mean() / df["Volume"].tail(60).mean()
    bb = bollinger(close).iloc[-1]
    st = stochastic(df).iloc[-1]
    adx_v = adx(df).iloc[-1]

    checks = {
        "harga > SMA20": (last > s20, 10),
        "harga > SMA50": (last > s50, 15),
        "SMA50 > SMA200": (s50 > s200, 15),
        "RSI 45-70": (45 <= r <= 70, 15),
        "MACD histogram > 0": (hist.iloc[-1] > 0, 15),
        "MACD histogram naik": (hist.iloc[-1] > hist.iloc[-2], 5),
        "return 1 bulan > 0": (ret_1m > 0, 5),
        "return 3 bulan > 0": (ret_3m > 0, 10),
        "volume 20h > rata-rata 60h": (vol_ratio > 1, 10),
    }
    score = sum(points for passed, points in checks.values() if passed)
    details = {
        "close": float(last), "sma20": float(s20), "sma50": float(s50), "sma200": float(s200),
        "rsi": round(float(r), 1), "ret_1m": round(float(ret_1m), 4),
        "ret_3m": round(float(ret_3m), 4), "vol_ratio": round(float(vol_ratio), 2),
        "passed": [name for name, (passed, _) in checks.items() if passed],
        # Confirmation signals read by the analyst team; not part of the 0-100 score during the trial.
        "signals": _signals(float(bb["pct_b"]), float(st["k"]), float(st["d"]), float(adx_v)),
    }
    return score, details


def fundamental_score(info: dict) -> tuple[int, dict]:
    """Score from Yahoo's ``info``; a missing field scores nothing and is listed."""
    roe, pe, pb = info.get("returnOnEquity"), info.get("trailingPE"), info.get("priceToBook")
    margin, growth, de = info.get("profitMargins"), info.get("revenueGrowth"), info.get("debtToEquity")
    is_bank = info.get("sector") == "Financial Services"
    score, missing = 0, []

    def tier(value, name, rules):
        nonlocal score
        if value is None:
            missing.append(name)
            return
        for test, points in rules:
            if test(value):
                score += points
                return

    tier(roe, "ROE", [(lambda v: v > 0.15, 25), (lambda v: v > 0.10, 15)])
    tier(pe, "PER", [(lambda v: 0 < v < 15, 20), (lambda v: 0 < v < 25, 10)])
    tier(pb, "PBV", [(lambda v: 0 < v < 3, 15), (lambda v: 0 < v < 5, 8)])
    tier(margin, "margin laba", [(lambda v: v > 0.10, 15), (lambda v: v > 0.05, 8)])
    tier(growth, "pertumbuhan pendapatan", [(lambda v: v > 0, 10)])
    if is_bank:
        score += 10  # debt/equity says little about a bank's balance sheet
    else:
        tier(de, "DER", [(lambda v: v < 100, 15), (lambda v: v < 200, 8)])

    details = {
        "roe": roe, "per": pe, "pbv": pb, "profit_margin": margin,
        "revenue_growth": growth, "der": None if is_bank else de,
        "sector": info.get("sector"), "name": info.get("longName"), "missing": missing,
    }
    return score, details


def screen(universe: list[str]) -> dict:
    symbols = [f"{code}.JK" for code in universe]
    prices = yf.download(symbols, period="1y", interval="1d", group_by="ticker",
                         auto_adjust=True, progress=False, threads=True)
    rows, skipped = [], []
    for code, symbol in zip(universe, symbols, strict=True):
        df = prices[symbol].dropna(subset=["Close"]) if symbol in prices.columns.get_level_values(0) else None
        if df is None or len(df) < 200:
            skipped.append({"code": code, "reason": "data harga kurang dari 200 hari"})
            continue
        daily_value = (df["Close"] * df["Volume"]).tail(20).mean()
        tech, tech_d = technical_score(df)
        try:
            info = yf.Ticker(symbol).info
        except Exception as exc:  # one ticker's fundamentals failing should not stop the screen
            logger.warning("info for %s failed: %s", symbol, exc)
            info = {}
        fund, fund_d = fundamental_score(info)
        last_move = df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1
        flags = []
        if abs(last_move) >= EXTREME_MOVE:
            flags.append(f"pergerakan harian ekstrem {last_move:+.1%}: cek berita/aksi korporasi")
        if daily_value < config.MIN_DAILY_VALUE:
            flags.append("nilai transaksi harian < Rp 10 miliar")
        if fund_d["missing"]:
            flags.append("data fundamental tidak lengkap: " + ", ".join(fund_d["missing"]))
        rows.append({
            "code": code,
            "name": fund_d["name"],
            "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "score": round(TECH_WEIGHT * tech + (1 - TECH_WEIGHT) * fund, 1),
            "technical_score": tech,
            "fundamental_score": fund,
            "last_move": round(float(last_move), 4),
            "avg_daily_value": float(daily_value),
            "technical": tech_d,
            "fundamental": fund_d,
            "atr": float(atr(df).iloc[-1]),
            "plan": plan_trade(float(df["Close"].iloc[-1]), float(atr(df).iloc[-1])),
            "flags": flags,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    # Candidates must be liquid, in an uptrend, and free of an unexplained shock move.
    eligible = [r for r in rows
                if r["avg_daily_value"] >= config.MIN_DAILY_VALUE
                and r["technical"]["close"] > r["technical"]["sma50"]
                and abs(r["last_move"]) < EXTREME_MOVE
                and r["plan"]["lots"] > 0]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "universe": "LQ45",
        "settings": {
            "equity": config.EQUITY, "risk_per_trade": config.RISK_PER_TRADE,
            "monthly_risk_cap": config.MONTHLY_RISK_CAP, "tech_weight": TECH_WEIGHT,
        },
        "candidates": [r["code"] for r in eligible[:config.TOP_N]],
        "rows": rows,
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the result as JSON here")
    args = parser.parse_args()
    result = screen(config.LQ45)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=float))
    print(f"{'Kode':6} {'Skor':>5} {'Tek':>4} {'Fund':>4}  Catatan")
    for r in result["rows"]:
        print(f"{r['code']:6} {r['score']:5.1f} {r['technical_score']:4} {r['fundamental_score']:4}  "
              + "; ".join(r["flags"]))
    print("Kandidat:", ", ".join(result["candidates"]) or "-")
    if result["skipped"]:
        print("Dilewati:", result["skipped"])


if __name__ == "__main__":
    main()
