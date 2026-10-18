"""Daily paper-trading run, for the IDX after its close and for crypto after the UTC day.

``python -m idx_bot.daily run [--market idx|crypto]`` settles that market's
ledger, screens its universe and sizes each candidate within the 2%/6% rules,
writing ``runs/daily-<date>.json`` and the dashboard's CSVs. The analyst team
then reviews the candidates, and ``python -m idx_bot.daily buy CODE --reason ...
[--market ...]`` queues an approved one for the next open.
"""

import argparse
import csv
import json
from pathlib import Path

import yfinance as yf

from idx_bot import config, crypto, paper
from idx_bot.risk import plan_trade
from idx_bot.screener import screen

HERE = Path(__file__).parent
RUNS = HERE / "runs"
DASHBOARD = HERE / "paper" / "dashboard"
CRYPTO_HOME = HERE / "crypto_paper"


def _paths(market: config.Market) -> tuple[Path, Path, Path]:
    """Runs directory, dashboard directory and ledger file of ``market``."""
    if market.name == "idx":
        return RUNS, DASHBOARD, paper.LEDGER
    return CRYPTO_HOME / "runs", CRYPTO_HOME / "dashboard", CRYPTO_HOME / "ledger.json"


def _idx_bars(codes: list[str]) -> dict:
    if not codes:
        return {}
    symbols = [f"{c}.JK" for c in codes]
    data = yf.download(symbols, period="3mo", interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False)
    return {c: data[s].dropna(subset=["Close"]) for c, s in zip(codes, symbols, strict=True)}


def run(market: config.Market = config.IDX, out_dir: Path | None = None) -> dict:
    runs, dashboard, ledger_path = _paths(market)
    out_dir = out_dir or dashboard
    runs.mkdir(parents=True, exist_ok=True)
    ledger = paper.load(ledger_path, market)
    held = sorted({p["code"] for p in ledger["open"] + ledger["pending"]})
    bars = _idx_bars(held) if market.name == "idx" else crypto.fetch(held, period="3mo")[0]
    events = paper.settle(ledger, bars, market)

    result = screen(config.LQ45) if market.name == "idx" else crypto.screen()
    date = max(r["last_date"] for r in result["rows"])
    (runs / f"screener-{date}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=float))
    rows = {r["code"]: r for r in result["rows"]}
    last_close = {code: r["technical"]["close"] for code, r in rows.items()}
    for code, df in bars.items():  # a held coin can drop out of the top list
        last_close.setdefault(code, float(df["Close"].iloc[-1]))

    month_loss = paper.month_loss(ledger, date[:7])
    open_risk = paper.open_risk(ledger)
    brake = month_loss + open_risk >= market.equity * config.MONTHLY_RISK_CAP
    held_now = {p["code"] for p in ledger["open"] + ledger["pending"]}
    candidates = []
    for code in result["candidates"]:
        if code in held_now:
            continue
        r = rows[code]
        plan = plan_trade(r["technical"]["close"], r["atr"], cash=ledger["cash"],
                          open_risk=open_risk, month_loss=month_loss, market=market, rules=r.get("rules"))
        if plan["lots"] > 0:
            candidates.append({"code": code, "name": r["name"], "score": r["score"], "plan": plan})

    report = {
        "market": market.name, "currency": market.currency, "price_source": result.get("price_source", "Yahoo"),
        "date": date, "events": events, "brake": brake,
        "month_loss": month_loss, "open_risk": open_risk,
        "cash": ledger["cash"], "equity": paper.equity(ledger, last_close),
        "candidates": candidates,
        "open": ledger["open"], "pending": ledger["pending"],
    }
    paper.save(ledger, ledger_path)
    (runs / f"daily-{date}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=float))
    _write_dashboard_csvs(out_dir, result, ledger, report, last_close, market)
    return report


def buy(code: str, reason: str, market: config.Market = config.IDX) -> dict:
    """Queue ``code`` from the latest daily report for the next open."""
    runs, dashboard, ledger_path = _paths(market)
    report = json.loads(sorted(runs.glob("daily-*.json"))[-1].read_text())
    match = next((c for c in report["candidates"] if c["code"] == code), None)
    if match is None:
        raise SystemExit(f"{code} bukan kandidat di laporan {report['date']}")
    if report["brake"]:
        raise SystemExit("Rem 6% aktif: tidak ada pembelian baru bulan ini")
    ledger = paper.load(ledger_path, market)
    paper.add_order(ledger, code, match["plan"], report["date"], reason, market)
    if paper.open_risk(ledger) + report["month_loss"] > market.equity * config.MONTHLY_RISK_CAP:
        raise SystemExit("Order ini akan melewati batas 6%: dibatalkan")
    paper.save(ledger, ledger_path)
    screened = json.loads((runs / f"screener-{report['date']}.json").read_text())
    _write_positions(dashboard, ledger, {r["code"]: r["technical"]["close"] for r in screened["rows"]}, market)
    return match


def _write_dashboard_csvs(out_dir: Path, result: dict, ledger: dict, report: dict, last_close: dict,
                          market: config.Market) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cand = set(result["candidates"])
    with open(out_dir / "screener.csv", "w", newline="") as f:
        w = csv.writer(f)
        if market.name == "idx":
            w.writerow(["kode", "nama", "skor", "skor_teknikal", "skor_fundamental", "harga", "rsi", "return_1b",
                        "gerak_16okt", "nilai_transaksi_miliar", "per", "roe", "stop", "target", "lot",
                        "risiko_rp", "kandidat", "catatan"])
        else:
            w.writerow(["kode", "nama", "skor", "skor_teknikal", "skor_pasar", "harga", "rsi", "return_1b",
                        "gerak_harian", "volume_juta_usd", "peringkat_mcap", "kuat_vs_btc_30h", "stop", "target",
                        "jumlah", "risiko_usdt", "kandidat", "catatan"])
        for r in result["rows"]:
            t, p = r["technical"], r["plan"]
            if market.name == "idx":
                fu = r["fundamental"]
                w.writerow([r["code"], r["name"], r["score"], r["technical_score"], r["fundamental_score"],
                            t["close"], t["rsi"], t["ret_1m"], r["last_move"], round(r["avg_daily_value"] / 1e9, 1),
                            "" if fu["per"] is None else round(fu["per"], 1),
                            "" if fu["roe"] is None else round(fu["roe"], 4),
                            p["stop"], p["target"], p["lots"], round(p["risk"]),
                            "ya" if r["code"] in cand else "tidak", "; ".join(r["flags"])])
            else:
                m = r["market"]
                w.writerow([r["code"], r["name"], r["score"], r["technical_score"], r["market_score"],
                            t["close"], t["rsi"], t["ret_1m"], r["last_move"], round(m["volume_usd"] / 1e6, 1),
                            m["rank"], m["rs_btc_30d"], p["stop"], p["target"], p["units"], round(p["risk"], 2),
                            "ya" if r["code"] in cand else "tidak", "; ".join(r["flags"])])
    _write_positions(out_dir, ledger, last_close, market)
    _write_benchmark(out_dir, market)
    history = out_dir / "ekuitas.csv"
    rows = [["tanggal", "ekuitas", "kas"]]
    if history.exists():
        with open(history, newline="") as f:
            rows = list(csv.reader(f))
    rows = [r for r in rows if r[0] != report["date"]]
    digits = 0 if market.name == "idx" else 2
    rows.append([report["date"], round(report["equity"], digits), round(report["cash"], digits)])
    with open(history, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def _write_benchmark(out_dir: Path, market: config.Market) -> None:
    """Six months of daily closes of the market's yardstick: IHSG for IDX, Bitcoin for crypto."""
    if market.name == "idx":
        name, df = "ihsg-6bulan.csv", yf.download("^JKSE", period="6mo", progress=False, auto_adjust=True)
    else:
        name, df = "btc-6bulan.csv", crypto.complete_days(
            yf.download("BTC-USD", period="6mo", progress=False, auto_adjust=True))
    close = df["Close"].squeeze().dropna()
    with open(out_dir / name, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tanggal", "penutupan"])
        for ts, v in close.items():
            w.writerow([ts.strftime("%Y-%m-%d"), round(float(v), 2)])


def _write_positions(out_dir: Path, ledger: dict, last_close: dict, market: config.Market) -> None:
    """One row per pending order, open position and closed trade. ``lot`` is lots on
    IDX and coins for crypto."""
    out_dir.mkdir(parents=True, exist_ok=True)
    digits = 0 if market.name == "idx" else 2
    size = (lambda p: p["lots"]) if market.name == "idx" else (lambda p: p["units"])
    with open(out_dir / "posisi.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kode", "status", "lot", "harga_masuk", "stop", "target", "harga_terakhir",
                    "laba_rugi_rp", "tanggal", "alasan"])
        for p in ledger["pending"]:
            w.writerow([p["code"], "menunggu", size(p), p["entry"], p["stop"], p["target"],
                        last_close.get(p["code"], ""), 0, p["signal_date"], p["reason"]])
        for p in ledger["open"]:
            last = last_close.get(p["code"], p["entry"])
            pnl = p["units"] * last * (1 - market.sell_fee) - p["cost"]
            w.writerow([p["code"], "terbuka", size(p), p["entry"], p["stop"], p["target"], last,
                        round(pnl, digits), p["entry_date"], p["reason"]])
        for t in ledger["closed"]:
            w.writerow([t["code"], f"ditutup ({t['exit_reason']})", size(t), t["entry"], t["stop"], t["target"],
                        t["exit_price"], t["pnl"], t["exit_date"], t["reason"]])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--market", choices=["idx", "crypto"], default="idx")
    r.add_argument("--out", type=Path)
    b = sub.add_parser("buy")
    b.add_argument("code")
    b.add_argument("--reason", required=True)
    b.add_argument("--market", choices=["idx", "crypto"], default="idx")
    args = parser.parse_args()
    market = config.IDX if args.market == "idx" else config.CRYPTO
    if args.cmd == "run":
        print(json.dumps(run(market, args.out), indent=2, ensure_ascii=False, default=float))
    else:
        print(json.dumps(buy(args.code, args.reason, market), indent=2, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
