"""Paper-trading ledger: orders fill at the next day's open, stops and targets on daily bars.

The ledger is a JSON file committed to the repo, since the session that runs it
does not keep files between days. Fills are conservative: a gap past a stop
fills at the open, and a day that touches both stop and target counts the stop.
"""

import json
from pathlib import Path

import pandas as pd

from idx_bot import config

LEDGER = Path(__file__).parent / "paper" / "ledger.json"


def new_ledger() -> dict:
    return {"cash": config.EQUITY, "pending": [], "open": [], "closed": []}


def load(path: Path | None = None) -> dict:
    path = path or LEDGER
    return json.loads(path.read_text()) if path.exists() else new_ledger()


def save(ledger: dict, path: Path | None = None) -> None:
    path = path or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))


def open_risk(ledger: dict) -> float:
    """What the open positions and pending orders lose if every stop is hit."""
    positions = ledger["open"] + ledger["pending"]
    return sum(max(p["entry"] - p["stop"], 0) * p["lots"] * config.LOT_SIZE for p in positions)


def month_loss(ledger: dict, month: str) -> float:
    """Net realised loss (positive number) of trades closed in ``month`` (YYYY-MM)."""
    pnl = sum(t["pnl"] for t in ledger["closed"] if t["exit_date"].startswith(month))
    return max(-pnl, 0.0)


def add_order(ledger: dict, code: str, plan: dict, signal_date: str, reason: str) -> None:
    """Queue a buy at the next open; ``plan`` comes from risk.plan_trade."""
    ledger["pending"].append({
        "code": code, "lots": plan["lots"], "entry": plan["entry"], "stop": plan["stop"],
        "target": plan["target"], "signal_date": signal_date, "reason": reason,
    })


def _close(ledger: dict, pos: dict, price: float, date: str, why: str) -> None:
    shares = pos["lots"] * config.LOT_SIZE
    proceeds = shares * price * (1 - config.SELL_FEE)
    ledger["cash"] += proceeds
    ledger["closed"].append({**pos, "exit_price": price, "exit_date": date, "exit_reason": why,
                             "pnl": round(proceeds - pos["cost"], 2)})


def settle(ledger: dict, bars: dict[str, pd.DataFrame]) -> list[str]:
    """Fill pending orders and check stops/targets against each code's daily bars.

    ``bars[code]`` is a daily OHLC frame indexed by date. Returns what happened,
    one line per event, for the daily report.
    """
    events = []
    still_pending = []
    for order in ledger["pending"]:
        df = bars.get(order["code"])
        after = df[df.index.strftime("%Y-%m-%d") > order["signal_date"]] if df is not None else None
        if after is None or after.empty:
            still_pending.append(order)
            continue
        day, bar = after.index[0].strftime("%Y-%m-%d"), after.iloc[0]
        fill = float(bar["Open"])
        cost = order["lots"] * config.LOT_SIZE * fill * (1 + config.BUY_FEE)
        if bar["Open"] <= order["stop"] or cost > ledger["cash"]:
            events.append(f"{order['code']}: order batal (buka {fill:,.0f} di bawah stop atau kas kurang)")
            continue
        ledger["cash"] -= cost
        # Keep the planned stop; the position's risk is now measured from the real fill.
        ledger["open"].append({**order, "entry": fill, "entry_date": day, "cost": round(cost, 2)})
        events.append(f"{order['code']}: beli {order['lots']} lot di {fill:,.0f} ({day})")
    ledger["pending"] = still_pending

    still_open = []
    for pos in ledger["open"]:
        df = bars.get(pos["code"])
        done = False
        if df is not None:
            for ts, bar in df[df.index.strftime("%Y-%m-%d") >= pos["entry_date"]].iterrows():
                day = ts.strftime("%Y-%m-%d")
                if bar["Low"] <= pos["stop"]:
                    price = min(float(bar["Open"]), pos["stop"]) if day > pos["entry_date"] else pos["stop"]
                    _close(ledger, pos, price, day, "stop-loss")
                    done = True
                elif bar["High"] >= pos["target"]:
                    price = max(float(bar["Open"]), pos["target"]) if day > pos["entry_date"] else pos["target"]
                    _close(ledger, pos, price, day, "target")
                    done = True
                if done:
                    t = ledger["closed"][-1]
                    events.append(f"{pos['code']}: jual di {t['exit_price']:,.0f} ({t['exit_reason']}), "
                                  f"laba/rugi Rp {t['pnl']:,.0f}")
                    break
        if not done:
            still_open.append(pos)
    ledger["open"] = still_open
    return events


def equity(ledger: dict, last_close: dict[str, float]) -> float:
    held = sum(p["lots"] * config.LOT_SIZE * last_close.get(p["code"], p["entry"]) for p in ledger["open"])
    return ledger["cash"] + held
