"""Checks between the daily runs, on 15-minute bars, for IDX shares and Indodax crypto.

``python -m idx_bot.intraday check --market idx`` runs three times a trading day
(about 09:45, 14:05 and 15:15 WIB); ``--market crypto`` every four hours. A check
closes a position the moment a bar touches its stop or target, and fills an order
the analyst team approved at the last daily run at the price of the moment instead
of only at the next open. Yahoo's IDX quotes are about ten minutes late, so an IDX
fill is the last 15-minute bar's close; Indodax's bars are live.

The daily run calls ``close_day`` before it settles on daily bars: that sweeps the
rest of the day bar by bar and marks the day as covered, so a low reached before an
intraday fill never triggers that position's stop. Days are WIB days for IDX and UTC
days for crypto, as the daily bars are.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from idx_bot import config, indodax_feed, paper
from idx_bot.risk import plan_trade

WIB = ZoneInfo("Asia/Jakarta")
MAX_CHASE = 0.03  # an approved order fills only within 3% above its planned entry
BAR = pd.Timedelta(minutes=15)
ACTIONS = ("beli", "jual", "batal")  # event kinds worth a phone notification


def market_day(market: config.Market, now: datetime) -> str:
    """The day a market's daily bar would carry for ``now``."""
    return now.astimezone(WIB if market.name == "idx" else UTC).strftime("%Y-%m-%d")


def today_bars(codes: list[str], day: str) -> dict[str, pd.DataFrame]:
    """IDX 15-minute bars of ``day`` (YYYY-MM-DD, WIB) per code; codes without any are left out."""
    if not codes:
        return {}
    symbols = [c if c.startswith("^") else f"{c}.JK" for c in codes]  # ^JKSE is the index itself
    data = yf.download(symbols, period="5d", interval="15m", group_by="ticker",
                       auto_adjust=False, progress=False)
    out = {}
    for code, symbol in zip(codes, symbols, strict=True):
        if data.empty or symbol not in data.columns.get_level_values(0):
            continue
        df = data[symbol].dropna(subset=["Close"])
        df.index = df.index.tz_convert(WIB)
        df = df[df.index.strftime("%Y-%m-%d") == day]
        if not df.empty:
            out[code] = df
    return out


def crypto_bars(codes: list[str], day: str, rules: dict | None = None) -> dict[str, pd.DataFrame]:
    """Indodax 15-minute bars of UTC ``day`` per coin; coins without any are left out."""
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    out = {}
    for code in codes:
        symbols = (rules or {}).get(code, {}).get("chart_symbols")
        df = indodax_feed.klines_15m(code, start, start + 86_400 - 1, symbols)
        df = df[df.index.strftime("%Y-%m-%d") == day] if not df.empty else df
        if not df.empty:
            out[code] = df
    return out


def _event(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


def _size(market: config.Market, plan: dict) -> str:
    return f"{plan['lots']} lot" if market.name == "idx" else f"{plan['units']:g} unit"


def sweep(ledger: dict, bars: dict[str, pd.DataFrame], market: config.Market = config.IDX,
          now: datetime | None = None) -> list[dict]:
    """Stops and targets on the bars each position has not seen yet, in time order.

    As on daily bars, a bar touching both counts the stop and a bar opening past a
    level fills at its open. A position that has not been swept before starts after
    the last day the daily run settled. With ``now``, a bar that may still be forming
    is checked but not marked as seen, so the next check looks at it again.
    """
    events, still_open = [], []
    for pos in ledger["open"]:
        df = bars.get(pos["code"])
        closed = False
        if df is not None:
            seen = pos.get("seen_until")
            new = (df[df.index > pd.Timestamp(seen)] if seen
                   else df[df.index.strftime("%Y-%m-%d") > ledger.get("settled_through", "")])
            for ts, bar in new.iterrows():
                if bar["Low"] <= pos["stop"]:
                    price, why = min(float(bar["Open"]), pos["stop"]), "stop-loss"
                elif bar["High"] >= pos["target"]:
                    price, why = max(float(bar["Open"]), pos["target"]), "target"
                else:
                    if now is None or ts + BAR <= pd.Timestamp(now):
                        pos["seen_until"] = ts.isoformat()
                    continue
                paper._close(ledger, pos, price, ts.strftime("%Y-%m-%d"), why, market)
                pnl = ledger["closed"][-1]["pnl"]
                events.append(_event("jual", f"{pos['code']}: jual di {paper._price(price)} ({why}, "
                                             f"{ts.astimezone(WIB):%H:%M} WIB), laba/rugi Rp {pnl:,.0f}"))
                closed = True
                break
        if not closed:
            still_open.append(pos)
    ledger["open"] = still_open
    return events


def fill_orders(ledger: dict, bars: dict[str, pd.DataFrame], day: str,
                market: config.Market = config.IDX, rules: dict | None = None) -> list[dict]:
    """Fill approved orders from an earlier daily run at the last bar's close.

    The stop keeps its planned distance below the fill and the size is worked out
    again at that price under the 2% and 6% rules (and the exchange's own steps,
    for crypto). An order whose price ran more than ``MAX_CHASE`` above plan waits
    for the next check; one already below its stop is cancelled.
    """
    events, still_pending = [], []
    for order in ledger["pending"]:
        df = bars.get(order["code"])
        if order["signal_date"] >= day or df is None:
            still_pending.append(order)
            continue
        order["intraday_checked"] = True
        ts, price = df.index[-1], float(df["Close"].iloc[-1])
        if price <= order["stop"]:
            events.append(_event("batal", f"{order['code']}: order batal, harga {paper._price(price)} "
                                          f"sudah di bawah stop {paper._price(order['stop'])}"))
            continue
        if price > order["entry"] * (1 + MAX_CHASE):
            events.append(_event("tunggu", f"{order['code']}: belum dibeli, harga {paper._price(price)} lebih "
                                           f"dari {MAX_CHASE:.0%} di atas rencana {paper._price(order['entry'])}"))
            still_pending.append(order)
            continue
        others = {**ledger, "pending": [o for o in ledger["pending"] if o is not order]}
        plan = plan_trade(price, (order["entry"] - order["stop"]) / config.ATR_STOP_MULTIPLE,
                          cash=ledger["cash"], open_risk=paper.open_risk(others),
                          month_loss=paper.month_loss(ledger, day[:7]), market=market,
                          rules=(rules or {}).get(order["code"]))
        cost = plan["units"] * price * (1 + market.buy_fee)
        if plan["lots"] <= 0 or cost > ledger["cash"]:
            events.append(_event("batal", f"{order['code']}: order batal, tidak muat batas risiko atau kas "
                                          f"di harga {paper._price(price)}"))
            continue
        ledger["cash"] -= cost
        ledger["open"].append({**order, "lots": plan["lots"], "units": plan["units"], "entry": price,
                               "stop": plan["stop"], "target": plan["target"], "planned_entry": order["entry"],
                               "entry_date": day, "entry_time": ts.isoformat(), "seen_until": ts.isoformat(),
                               "cost": round(cost, 2)})
        events.append(_event("beli", f"{order['code']}: beli {_size(market, plan)} di {paper._price(price)} "
                                     f"({ts.astimezone(WIB):%H:%M} WIB), stop {paper._price(plan['stop'])}, "
                                     f"target {paper._price(plan['target'])}"))
    ledger["pending"] = still_pending
    return events


def close_day(ledger: dict, bars: dict[str, pd.DataFrame], day: str,
              market: config.Market = config.IDX) -> list[dict]:
    """The daily run's first pass: sweep the rest of ``day``, mark the swept positions
    so the daily settle skips that day, and cancel orders the checks could not fill."""
    events = sweep(ledger, bars, market)
    for pos in ledger["open"]:
        if pos["code"] in bars:
            pos["swept_day"] = day
    for order in [o for o in ledger["pending"] if o.get("intraday_checked")]:
        ledger["pending"].remove(order)
        events.append(_event("batal", f"{order['code']}: order batal, tidak terisi di antara dua run harian"))
    return events


def last_settled_day(market: config.Market, now: datetime) -> str:
    """The day the daily run is about to settle: today on IDX (it runs after the
    close), yesterday's UTC day for crypto (it runs after 00:00 UTC)."""
    return market_day(market, now if market.name == "idx" else now - timedelta(days=1))


def _ihsg_prev_close(dashboard, day: str) -> float | None:
    """IHSG's last close before ``day``, from the benchmark CSV the evening run writes."""
    path = dashboard / "ihsg-6bulan.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    before = df[df["tanggal"] < day]
    return None if before.empty else float(before["penutupan"].iloc[-1])


def check(market: config.Market = config.IDX, now: datetime | None = None) -> dict:
    """One check between daily runs; writes runs/intraday-<date>-<HHMM>.json and the
    dashboard's posisi.csv, and returns the report."""
    from idx_bot import daily  # daily imports this module for close_day

    now = now or datetime.now(UTC)
    day = market_day(market, now)
    runs, dashboard, ledger_path = daily._paths(market)
    ledger = paper.load(ledger_path, market)
    screen = json.loads(sorted(runs.glob("screener-*.json"))[-1].read_text())
    prev_close = {r["code"]: r["technical"]["close"] for r in screen["rows"]}
    codes = sorted(set(prev_close) | {p["code"] for p in ledger["open"] + ledger["pending"]})
    rules = None
    if market.name == "idx":
        bars = today_bars(codes + ["^JKSE"], day)
        index = bars.pop("^JKSE", None)
    else:
        try:
            rules = indodax_feed.trading_rules()
        except Exception:  # chart codes fall back to <COIN>IDR
            rules = None
        bars = crypto_bars(codes, day, rules)
        index = None
    wib = now.astimezone(WIB)
    report = {"market": market.name, "date": day, "time_wib": wib.strftime("%H:%M"),
              "source": "Yahoo, tertunda sekitar 10 menit" if market.name == "idx" else "Indodax, langsung"}
    if not bars:
        report.update(market_open=False, events=[], notify=False)
        return report

    events = sweep(ledger, bars, market, now) + fill_orders(ledger, bars, day, market, rules)
    last = {c: float(df["Close"].iloc[-1]) for c, df in bars.items()}
    moves = sorted(((c, last[c] / prev_close[c] - 1) for c in last if c in prev_close), key=lambda m: m[1])
    report.update(
        market_open=True,
        data_until_wib=(max(df.index[-1] for df in bars.values()).astimezone(WIB)).strftime("%H:%M"),
        events=events,
        notify=any(e["kind"] in ACTIONS for e in events),
        top_gainers=[(c, round(m, 4)) for c, m in moves[::-1][:5]],
        top_losers=[(c, round(m, 4)) for c, m in moves[:5]],
        positions=[{"code": p["code"], "entry": p["entry"], "last": last.get(p["code"]), "stop": p["stop"],
                    "target": p["target"], "pnl": round(p["units"] * last.get(p["code"], p["entry"])
                                                        * (1 - market.sell_fee) - p["cost"])}
                   for p in ledger["open"]],
        pending=[{"code": o["code"], "entry": o["entry"], "last": last.get(o["code"])} for o in ledger["pending"]],
        cash=ledger["cash"],
        equity=paper.equity(ledger, {**prev_close, **last}),
    )
    if index is not None:
        report["ihsg"] = {"last": round(float(index["Close"].iloc[-1]), 2),
                          "prev_close": _ihsg_prev_close(dashboard, day)}
    if "BTC" in last and "BTC" in prev_close:
        report["btc"] = {"last": last["BTC"], "vs_last_daily_close": round(last["BTC"] / prev_close["BTC"] - 1, 4)}
    paper.save(ledger, ledger_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"intraday-{day}-{wib:%H%M}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=float))
    daily._write_positions(dashboard, ledger, {**prev_close, **last}, market)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["check"])
    parser.add_argument("--market", choices=["idx", "crypto"], default="idx")
    args = parser.parse_args()
    market = config.IDX if args.market == "idx" else config.INDODAX
    print(json.dumps(check(market), indent=2, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
