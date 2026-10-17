"""Daily paper-trading run, after the IDX close.

``python -m idx_bot.daily run`` settles the ledger, screens the LQ45 and sizes
each candidate within the 2%/6% rules, writing ``runs/daily-<date>.json`` and
the dashboard's CSVs. The analyst team then reviews the candidates, and
``python -m idx_bot.daily buy CODE --reason ...`` queues an approved one for
the next open.
"""

import argparse
import csv
import json
from pathlib import Path

import yfinance as yf

from idx_bot import config, paper
from idx_bot.risk import plan_trade
from idx_bot.screener import screen

RUNS = Path(__file__).parent / "runs"
DASHBOARD = Path(__file__).parent / "paper" / "dashboard"


def _bars(codes: list[str]) -> dict:
    if not codes:
        return {}
    symbols = [f"{c}.JK" for c in codes]
    data = yf.download(symbols, period="3mo", interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False)
    return {c: data[s].dropna(subset=["Close"]) for c, s in zip(codes, symbols, strict=True)}


def run(out_dir: Path) -> dict:
    ledger = paper.load()
    held = sorted({p["code"] for p in ledger["open"] + ledger["pending"]})
    events = paper.settle(ledger, _bars(held))

    result = screen(config.LQ45)
    date = max(r["last_date"] for r in result["rows"])
    (RUNS / f"screener-{date}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=float))
    rows = {r["code"]: r for r in result["rows"]}
    last_close = {code: r["technical"]["close"] for code, r in rows.items()}

    month_loss = paper.month_loss(ledger, date[:7])
    open_risk = paper.open_risk(ledger)
    brake = month_loss + open_risk >= config.EQUITY * config.MONTHLY_RISK_CAP
    held_now = {p["code"] for p in ledger["open"] + ledger["pending"]}
    candidates = []
    for code in result["candidates"]:
        if code in held_now:
            continue
        r = rows[code]
        plan = plan_trade(r["technical"]["close"], r["atr"], cash=ledger["cash"],
                          open_risk=open_risk, month_loss=month_loss)
        if plan["lots"] > 0:
            candidates.append({"code": code, "name": r["name"], "score": r["score"], "plan": plan})

    report = {
        "date": date, "events": events, "brake": brake,
        "month_loss": month_loss, "open_risk": open_risk,
        "cash": ledger["cash"], "equity": paper.equity(ledger, last_close),
        "candidates": candidates,
        "open": ledger["open"], "pending": ledger["pending"],
    }
    paper.save(ledger)
    (RUNS / f"daily-{date}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=float))
    _write_dashboard_csvs(out_dir, result, ledger, report, last_close)
    return report


def buy(code: str, reason: str) -> dict:
    """Queue ``code`` from the latest daily report for the next open."""
    report = json.loads(sorted(RUNS.glob("daily-*.json"))[-1].read_text())
    match = next((c for c in report["candidates"] if c["code"] == code), None)
    if match is None:
        raise SystemExit(f"{code} bukan kandidat di laporan {report['date']}")
    if report["brake"]:
        raise SystemExit("Rem 6% aktif: tidak ada pembelian baru bulan ini")
    ledger = paper.load()
    paper.add_order(ledger, code, match["plan"], report["date"], reason)
    if paper.open_risk(ledger) + report["month_loss"] > config.EQUITY * config.MONTHLY_RISK_CAP:
        raise SystemExit("Order ini akan melewati batas 6%: dibatalkan")
    paper.save(ledger)
    screened = json.loads((RUNS / f"screener-{report['date']}.json").read_text())
    _write_positions(DASHBOARD, ledger, {r["code"]: r["technical"]["close"] for r in screened["rows"]})
    return match


def _write_dashboard_csvs(out_dir: Path, result: dict, ledger: dict, report: dict, last_close: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cand = set(result["candidates"])
    with open(out_dir / "screener.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kode", "nama", "skor", "skor_teknikal", "skor_fundamental", "harga", "rsi", "return_1b",
                    "gerak_16okt", "nilai_transaksi_miliar", "per", "roe", "stop", "target", "lot",
                    "risiko_rp", "kandidat", "catatan"])
        for r in result["rows"]:
            t, fu, p = r["technical"], r["fundamental"], r["plan"]
            w.writerow([r["code"], r["name"], r["score"], r["technical_score"], r["fundamental_score"],
                        t["close"], t["rsi"], t["ret_1m"], r["last_move"], round(r["avg_daily_value"] / 1e9, 1),
                        "" if fu["per"] is None else round(fu["per"], 1),
                        "" if fu["roe"] is None else round(fu["roe"], 4),
                        p["stop"], p["target"], p["lots"], round(p["risk_idr"]),
                        "ya" if r["code"] in cand else "tidak", "; ".join(r["flags"])])
    _write_positions(out_dir, ledger, last_close)
    history = out_dir / "ekuitas.csv"
    rows = [["tanggal", "ekuitas", "kas"]]
    if history.exists():
        with open(history, newline="") as f:
            rows = list(csv.reader(f))
    rows = [r for r in rows if r[0] != report["date"]]
    rows.append([report["date"], round(report["equity"]), round(report["cash"])])
    with open(history, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def _write_positions(out_dir: Path, ledger: dict, last_close: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "posisi.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kode", "status", "lot", "harga_masuk", "stop", "target", "harga_terakhir",
                    "laba_rugi_rp", "tanggal", "alasan"])
        for p in ledger["pending"]:
            w.writerow([p["code"], "menunggu", p["lots"], p["entry"], p["stop"], p["target"],
                        last_close.get(p["code"], ""), 0, p["signal_date"], p["reason"]])
        for p in ledger["open"]:
            last = last_close.get(p["code"], p["entry"])
            pnl = p["lots"] * config.LOT_SIZE * last * (1 - config.SELL_FEE) - p["cost"]
            w.writerow([p["code"], "terbuka", p["lots"], p["entry"], p["stop"], p["target"], last,
                        round(pnl), p["entry_date"], p["reason"]])
        for t in ledger["closed"]:
            w.writerow([t["code"], f"ditutup ({t['exit_reason']})", t["lots"], t["entry"], t["stop"], t["target"],
                        t["exit_price"], t["pnl"], t["exit_date"], t["reason"]])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, default=DASHBOARD)
    b = sub.add_parser("buy")
    b.add_argument("code")
    b.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.cmd == "run":
        print(json.dumps(run(args.out), indent=2, ensure_ascii=False, default=float))
    else:
        print(json.dumps(buy(args.code, args.reason), indent=2, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
