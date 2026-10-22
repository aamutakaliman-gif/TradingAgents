"""Checks during IDX trading hours, on 15-minute bars.

``python -m idx_bot.intraday check`` runs three times a trading day (about 09:45,
14:05 and 15:15 WIB). It closes a position the moment a bar touches its stop or
target, and fills an order the analyst team approved the evening before at the
price of the moment instead of only at the open. Yahoo's IDX quotes are about ten
minutes late, so a fill is the last 15-minute bar's close, not a live quote.

The evening run calls ``close_day`` before it settles on daily bars: that sweeps
the rest of the day bar by bar and marks the day as covered, so a low reached
before an intraday fill never triggers that position's stop.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from idx_bot import config, paper
from idx_bot.risk import plan_trade

WIB = ZoneInfo("Asia/Jakarta")
MAX_CHASE = 0.03  # an approved order fills only within 3% above its planned entry
RUNS = Path(__file__).parent / "runs"
DASHBOARD = Path(__file__).parent / "paper" / "dashboard"
ACTIONS = ("beli", "jual", "batal")  # event kinds worth a phone notification


def today_bars(codes: list[str], day: str) -> dict[str, pd.DataFrame]:
    """15-minute bars of ``day`` (YYYY-MM-DD, WIB) per code; codes without any are left out."""
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


def _event(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


def sweep(ledger: dict, bars: dict[str, pd.DataFrame], market: config.Market = config.IDX) -> list[dict]:
    """Stops and targets on the bars each position has not seen yet, in time order.

    As on daily bars, a bar touching both counts the stop and a bar opening past a
    level fills at its open. A position that has not been swept before starts after
    the last day the evening run settled.
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
                    pos["seen_until"] = ts.isoformat()
                    continue
                paper._close(ledger, pos, price, ts.strftime("%Y-%m-%d"), why, market)
                pnl = ledger["closed"][-1]["pnl"]
                events.append(_event("jual", f"{pos['code']}: jual di {paper._price(price)} ({why}, "
                                             f"{ts:%H:%M} WIB), laba/rugi Rp {pnl:,.0f}"))
                closed = True
                break
        if not closed:
            still_open.append(pos)
    ledger["open"] = still_open
    return events


def fill_orders(ledger: dict, bars: dict[str, pd.DataFrame], day: str,
                market: config.Market = config.IDX) -> list[dict]:
    """Fill approved orders from an earlier evening at the last bar's close.

    The stop keeps its planned distance below the fill and the size is worked out
    again at that price under the 2% and 6% rules. An order whose price ran more
    than ``MAX_CHASE`` above plan waits for the next check; one already below its
    stop is cancelled.
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
                          month_loss=paper.month_loss(ledger, day[:7]), market=market)
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
        events.append(_event("beli", f"{order['code']}: beli {plan['lots']} lot di {paper._price(price)} "
                                     f"({ts:%H:%M} WIB), stop {paper._price(plan['stop'])}, "
                                     f"target {paper._price(plan['target'])}"))
    ledger["pending"] = still_pending
    return events


def close_day(ledger: dict, bars: dict[str, pd.DataFrame], day: str,
              market: config.Market = config.IDX) -> list[dict]:
    """The evening's last pass: sweep the rest of ``day``, mark the swept positions so
    the daily settle skips that day, and cancel orders the checks could not fill."""
    events = sweep(ledger, bars, market)
    for pos in ledger["open"]:
        if pos["code"] in bars:
            pos["swept_day"] = day
    for order in [o for o in ledger["pending"] if o.get("intraday_checked")]:
        ledger["pending"].remove(order)
        events.append(_event("batal", f"{order['code']}: order batal, tidak terisi selama jam bursa"))
    return events


def _ihsg_prev_close(day: str) -> float | None:
    """IHSG's last close before ``day``, from the benchmark CSV the evening run writes."""
    path = DASHBOARD / "ihsg-6bulan.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    before = df[df["tanggal"] < day]
    return None if before.empty else float(before["penutupan"].iloc[-1])


def check(now: datetime | None = None) -> dict:
    """One check during trading hours; writes runs/intraday-<date>-<HHMM>.json and the
    dashboard's posisi.csv, and returns the report."""
    from idx_bot import daily  # daily imports this module for close_day

    now = now or datetime.now(WIB)
    day = now.strftime("%Y-%m-%d")
    ledger = paper.load()
    screen = json.loads(sorted(RUNS.glob("screener-*.json"))[-1].read_text())
    prev_close = {r["code"]: r["technical"]["close"] for r in screen["rows"]}
    held = {p["code"] for p in ledger["open"] + ledger["pending"]}
    bars = today_bars(sorted(set(prev_close) | held | {"^JKSE"}), day)
    report = {"date": day, "time": now.strftime("%H:%M"), "source": "Yahoo, tertunda sekitar 10 menit"}
    ihsg = bars.pop("^JKSE", None)
    if not bars:
        report.update(market_open=False, events=[])
        return report

    events = sweep(ledger, bars) + fill_orders(ledger, bars, day)
    last = {c: float(df["Close"].iloc[-1]) for c, df in bars.items()}
    moves = sorted(((c, last[c] / prev_close[c] - 1) for c in last if c in prev_close), key=lambda m: m[1])
    report.update(
        market_open=True,
        data_until=max(df.index[-1] for df in bars.values()).strftime("%H:%M"),
        ihsg=None if ihsg is None else {"last": round(float(ihsg["Close"].iloc[-1]), 2),
                                         "prev_close": _ihsg_prev_close(day)},
        events=events,
        notify=any(e["kind"] in ACTIONS for e in events),
        top_gainers=[(c, round(m, 4)) for c, m in moves[::-1][:5]],
        top_losers=[(c, round(m, 4)) for c, m in moves[:5]],
        positions=[{"code": p["code"], "entry": p["entry"], "last": last.get(p["code"]), "stop": p["stop"],
                    "target": p["target"], "pnl": round(p["units"] * last.get(p["code"], p["entry"])
                                                        * (1 - config.IDX.sell_fee) - p["cost"])}
                   for p in ledger["open"]],
        pending=[{"code": o["code"], "entry": o["entry"], "last": last.get(o["code"])} for o in ledger["pending"]],
        cash=ledger["cash"],
        equity=paper.equity(ledger, {**prev_close, **last}),
    )
    paper.save(ledger)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"intraday-{day}-{now:%H%M}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=float))
    daily._write_positions(DASHBOARD, ledger, {**prev_close, **last}, config.IDX)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["check"])
    parser.parse_args()
    print(json.dumps(check(), indent=2, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
