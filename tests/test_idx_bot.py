"""IDX screener: indicators, Elder 2%/6% sizing, and scoring on synthetic data."""

import numpy as np
import pandas as pd
import pytest

from idx_bot import config
from idx_bot.indicators import atr, rsi
from idx_bot.risk import plan_trade, round_down_to_tick, tick_size
from idx_bot.screener import fundamental_score, technical_score


def _frame(closes):
    close = pd.Series(closes, dtype=float, index=pd.bdate_range("2025-01-01", periods=len(closes)))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1_000_000.0})


@pytest.mark.unit
def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1, 60, dtype=float))).iloc[-1] == pytest.approx(100)
    assert rsi(pd.Series(np.arange(60, 1, -1, dtype=float))).iloc[-1] == pytest.approx(0)


@pytest.mark.unit
def test_atr_of_constant_range():
    df = _frame([1000.0] * 50)
    assert atr(df).iloc[-1] == pytest.approx(20.0)  # high-low = 1010 - 990


@pytest.mark.unit
def test_idx_tick_sizes():
    assert [tick_size(p) for p in (150, 300, 1500, 3000, 6000)] == [1, 2, 5, 10, 25]
    assert round_down_to_tick(5180) == 5175
    assert round_down_to_tick(1503) == 1500


@pytest.mark.unit
def test_plan_trade_risks_at_most_two_percent():
    plan = plan_trade(entry=1000, atr_value=25)  # stop 950, 50 per share at risk
    assert plan["stop"] == 950
    assert plan["target"] == 1100
    assert plan["lots"] == 40  # 200,000 / (50 * 100)
    assert plan["risk"] <= config.EQUITY * config.RISK_PER_TRADE


@pytest.mark.unit
def test_plan_trade_capped_by_cash():
    plan = plan_trade(entry=1000, atr_value=1, cash=2_000_000)
    assert plan["position_value"] <= 2_000_000
    assert plan["lots"] == 19  # 20 lots would exceed cash once the buy fee is added


@pytest.mark.unit
def test_plan_trade_respects_six_percent_rule():
    # 300k open risk + 250k lost this month leaves 50k of the 600k monthly cap.
    plan = plan_trade(entry=1000, atr_value=25, open_risk=300_000, month_loss=250_000)
    assert plan["risk"] <= 50_000
    assert plan_trade(entry=1000, atr_value=25, month_loss=600_000)["lots"] == 0


@pytest.mark.unit
def test_uptrend_outscores_downtrend():
    up, _ = technical_score(_frame(np.linspace(1000, 2000, 260) + np.sin(np.arange(260)) * 15))
    down, details = technical_score(_frame(np.linspace(2000, 1000, 260)))
    assert up > down
    # A steady decline flattens the MACD histogram towards zero; nothing else passes.
    assert details["passed"] == ["MACD histogram naik"]


@pytest.mark.unit
def test_fundamental_missing_fields_listed_and_bank_der_skipped():
    score, details = fundamental_score({"sector": "Financial Services", "returnOnEquity": 0.2})
    assert score == 35  # 25 for ROE + 10 neutral for a bank's leverage
    assert "DER" not in details["missing"]
    assert "PER" in details["missing"]


def _bars(rows, start="2026-10-19"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def _ledger_with_order():
    from idx_bot import paper
    ledger = paper.new_ledger()
    plan = {"lots": 10, "entry": 1000, "stop": 950, "target": 1100}
    paper.add_order(ledger, "TEST", plan, "2026-10-16", "uji")
    return paper, ledger


@pytest.mark.unit
def test_paper_fills_at_next_open_and_hits_target():
    paper, ledger = _ledger_with_order()
    bars = {"TEST": _bars([[1010, 1030, 1000, 1020], [1020, 1120, 1015, 1110]])}
    events = paper.settle(ledger, bars)
    assert len(events) == 2 and not ledger["open"]
    trade = ledger["closed"][0]
    assert trade["entry"] == 1010 and trade["exit_price"] == 1100 and trade["exit_reason"] == "target"
    cost = 1000 * 1010 * (1 + config.BUY_FEE)
    assert trade["pnl"] == pytest.approx(1000 * 1100 * (1 - config.SELL_FEE) - cost, abs=0.01)
    assert ledger["cash"] == pytest.approx(config.EQUITY + trade["pnl"])


@pytest.mark.unit
def test_paper_gap_below_stop_fills_at_open_and_stop_wins_ties():
    paper, ledger = _ledger_with_order()
    paper.settle(ledger, {"TEST": _bars([[1010, 1020, 1000, 1010], [900, 920, 880, 900]])})
    assert ledger["closed"][0]["exit_price"] == 900  # gapped through the 950 stop
    paper2, ledger2 = _ledger_with_order()
    paper2.settle(ledger2, {"TEST": _bars([[1010, 1150, 940, 1000]])})
    assert ledger2["closed"][0]["exit_reason"] == "stop-loss"


@pytest.mark.unit
def test_paper_order_waits_without_new_bar_and_cancels_below_stop():
    paper, ledger = _ledger_with_order()
    paper.settle(ledger, {"TEST": _bars([[1000, 1000, 1000, 1000]], start="2026-10-16")})
    assert len(ledger["pending"]) == 1  # no bar after the signal day yet
    paper.settle(ledger, {"TEST": _bars([[940, 960, 930, 950]])})
    assert not ledger["pending"] and not ledger["open"] and ledger["cash"] == config.EQUITY


@pytest.mark.unit
def test_paper_risk_and_month_loss():
    paper, ledger = _ledger_with_order()
    assert paper.open_risk(ledger) == 50 * 10 * 100
    ledger["closed"] = [{"exit_date": "2026-10-20", "pnl": -120_000}, {"exit_date": "2026-09-30", "pnl": -50_000},
                        {"exit_date": "2026-10-21", "pnl": 20_000}]
    assert paper.month_loss(ledger, "2026-10") == 100_000


@pytest.mark.unit
def test_buy_queues_candidate_and_refuses_others(tmp_path, monkeypatch):
    import json

    from idx_bot import daily, paper
    monkeypatch.setattr(paper, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(daily, "RUNS", tmp_path)
    monkeypatch.setattr(daily, "DASHBOARD", tmp_path / "dash")
    plan = {"lots": 10, "entry": 1000, "stop": 950, "target": 1100}
    report = {"date": "2026-10-19", "brake": False, "month_loss": 0,
              "candidates": [{"code": "TEST", "name": "Uji", "score": 70, "plan": plan}]}
    (tmp_path / "daily-2026-10-19.json").write_text(json.dumps(report))
    (tmp_path / "screener-2026-10-19.json").write_text(
        json.dumps({"rows": [{"code": "TEST", "technical": {"close": 1000}}]}))

    daily.buy("TEST", "tren naik")
    assert paper.load()["pending"][0]["code"] == "TEST"
    assert "TEST,menunggu,10" in (tmp_path / "dash" / "posisi.csv").read_text()
    with pytest.raises(SystemExit):
        daily.buy("OTHER", "bukan kandidat")
    report["brake"] = True
    (tmp_path / "daily-2026-10-19.json").write_text(json.dumps(report))
    with pytest.raises(SystemExit):
        daily.buy("TEST", "rem aktif")


@pytest.mark.unit
def test_crypto_ticks_and_sizing():
    assert tick_size(81_500, config.CRYPTO) == pytest.approx(1.0)
    assert tick_size(0.3512, config.CRYPTO) == pytest.approx(0.00001)
    plan = plan_trade(entry=80_000, atr_value=2_000, market=config.CRYPTO)  # stop 76,000
    assert plan["stop"] == 76_000 and plan["target"] == 88_000
    budget = config.CRYPTO.equity * config.RISK_PER_TRADE
    assert plan["risk"] <= budget
    assert plan["risk"] > budget * 0.99  # coins are divisible, so nearly all of the 2% is used
    assert plan["units"] == pytest.approx(0.002777, abs=1e-6)


@pytest.mark.unit
def test_crypto_drops_the_utc_day_in_progress():
    from datetime import UTC, datetime

    from idx_bot.crypto import complete_days
    df = _bars([[1, 1, 1, 1]] * 3, start="2026-10-16")
    df.index = pd.date_range("2026-10-16", periods=3, freq="D")
    kept = complete_days(df, now=datetime(2026, 10, 18, 0, 15, tzinfo=UTC))
    assert list(kept.index.strftime("%Y-%m-%d")) == ["2026-10-16", "2026-10-17"]


@pytest.mark.unit
def test_crypto_market_score():
    from idx_bot.crypto import market_score
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    btc = pd.DataFrame({"Close": np.linspace(100, 110, 120), "Volume": 5e9}, index=idx)
    strong = pd.DataFrame({"Close": np.linspace(100, 150, 120), "Volume": 3e8}, index=idx)
    score, d = market_score("SOL", strong, btc, rank=5)
    assert score == 20 + 25 + 15 + 15 and d["rs_btc_30d"] > 0
    assert market_score("BTC", btc, btc, rank=1)[0] == 30 + 20 + 30


@pytest.mark.unit
def test_crypto_paper_trade_uses_units_and_binance_fees():
    from idx_bot import paper
    ledger = paper.new_ledger(config.CRYPTO)
    plan = plan_trade(entry=80_000, atr_value=2_000, market=config.CRYPTO)
    paper.add_order(ledger, "BTC", plan, "2026-10-17", "uji", config.CRYPTO)
    bars = {"BTC": _bars([[80_000, 81_000, 79_000, 80_500], [80_500, 89_000, 80_000, 88_500]], start="2026-10-18")}
    events = paper.settle(ledger, bars, config.CRYPTO)
    trade = ledger["closed"][0]
    assert "unit" in events[0] and trade["exit_price"] == 88_000
    expected = plan["units"] * (88_000 * (1 - 0.001) - 80_000 * (1 + 0.001))
    assert trade["pnl"] == pytest.approx(expected, abs=0.01)


class _FakeBinance:
    """Stands in for binance.client.Client with canned public-endpoint answers."""

    def get_klines(self, symbol, interval, limit):
        day = 86_400_000
        start = 1_760_659_200_000  # 2025-10-17 00:00 UTC
        return [[start + i * day, "100", "110", "90", str(100 + i), "5", start + (i + 1) * day - 1, "5000",
                 10, "2", "200", "0"] for i in range(3)]

    def get_symbol_info(self, symbol):
        if symbol != "BTCUSDT":
            return None
        return {"filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        ]}


@pytest.mark.unit
def test_binance_klines_and_trading_rules():
    from idx_bot import binance_feed
    df = binance_feed.klines(_FakeBinance(), "BTC", days=3)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df["Close"].tolist() == [100, 101, 102] and df["Volume"].iloc[0] == 5000  # USDT turnover
    assert df.index[0].strftime("%Y-%m-%d") == "2025-10-17"
    assert binance_feed.trading_rules(_FakeBinance(), "BTC") == {"step": 0.00001, "tick": 0.01, "min_notional": 5.0}
    assert binance_feed.trading_rules(_FakeBinance(), "XYZ") is None


@pytest.mark.unit
def test_plan_trade_follows_binance_rules():
    rules = {"step": 0.00001, "tick": 0.01, "min_notional": 5.0}
    plan = plan_trade(entry=81_382.15, atr_value=2_133.57, market=config.CRYPTO, rules=rules)
    assert plan["stop"] == pytest.approx(77_115.01)  # floored to the 0.01 tick
    assert plan["units"] == pytest.approx(0.0026)  # floored to the 0.00001 step
    assert round(plan["units"] / rules["step"], 6) == int(round(plan["units"] / rules["step"]))
    assert plan["risk"] <= config.CRYPTO.equity * config.RISK_PER_TRADE
    tiny = plan_trade(entry=81_382.15, atr_value=2_133.57, market=config.CRYPTO, cash=4.0, rules=rules)
    assert tiny["units"] == 0  # under Binance's 5 USDT minimum order


@pytest.mark.unit
def test_crypto_fetch_falls_back_to_yahoo(monkeypatch):
    from idx_bot import binance_feed, crypto

    def unreachable():
        raise ConnectionError("403 from proxy")
    monkeypatch.setattr(binance_feed, "client", unreachable)
    monkeypatch.setattr(crypto, "_yahoo", lambda codes, period: dict.fromkeys(codes, "yahoo"))
    assert crypto.fetch(["BTC"]) == ({"BTC": "yahoo"}, "Yahoo")
    monkeypatch.setattr(binance_feed, "client", lambda: _FakeBinance())
    prices, source = crypto.fetch(["BTC"], period="3mo")
    assert source == "Binance" and len(prices["BTC"]) == 3  # all three days are long past


class _FakeTestnet(_FakeBinance):
    """Records the orders a testnet run sends."""

    def __init__(self, fail_on=None):
        self.calls, self.status, self.fail_on = [], {}, fail_on

    def get_symbol_info(self, symbol):
        return super().get_symbol_info("BTCUSDT")

    def order_market_buy(self, **p):
        if p["symbol"] == self.fail_on:
            raise RuntimeError("Filter failure: NOTIONAL")
        self.calls.append(("buy", p))
        qty = float(p["quantity"])
        return {"orderId": 1, "executedQty": p["quantity"], "cummulativeQuoteQty": str(qty * 60_000)}

    def create_oco_order(self, **p):
        self.calls.append(("oco", p))
        return {"orders": [{"orderId": 11}, {"orderId": 12}]}

    def get_order(self, symbol, orderId):
        return {"status": self.status.get(orderId, "NEW"), "type": "LIMIT_MAKER" if orderId == 11 else "STOP_LOSS_LIMIT"}

    def cancel_order(self, **p):
        self.calls.append(("cancel", p))

    def order_market_sell(self, **p):
        self.calls.append(("sell", p))


def _open_btc(units=0.0026123):
    return {"code": "BTC", "units": units, "entry": 80_000, "stop": 76_000, "target": 88_000}


@pytest.mark.unit
def test_testnet_needs_keys_and_only_builds_a_testnet_client(monkeypatch):
    from idx_bot import binance_testnet
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    assert binance_testnet.connect() is None
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    api = binance_testnet.connect()
    assert api.testnet is True and "testnet.binance.vision" in api._create_api_uri("order")


@pytest.mark.unit
def test_testnet_bracket_keeps_paper_distances_around_its_own_fill():
    from idx_bot import binance_testnet
    api = _FakeTestnet()
    rec = binance_testnet.open_bracket(api, _open_btc())
    buy, oco = api.calls[0][1], api.calls[1][1]
    assert buy["quantity"] == "0.00261"  # floored to the 0.00001 step
    assert rec["fill"] == pytest.approx(60_000)
    # paper stop is 5% and target 10% from entry; the bracket keeps those around the 60,000 fill
    assert oco["belowStopPrice"] == "57000" and oco["abovePrice"] == "66000"
    assert oco["belowPrice"] == "56715" and oco["belowType"] == "STOP_LOSS_LIMIT"
    assert oco["aboveType"] == "LIMIT_MAKER" and oco["side"] == "SELL" and oco["quantity"] == "0.00261"


@pytest.mark.unit
def test_testnet_sync_follows_the_paper_ledger():
    from idx_bot import binance_testnet
    api = _FakeTestnet()
    ledger = {"open": [_open_btc()], "closed": []}
    events = binance_testnet.sync(api, ledger)
    assert ledger["open"][0]["testnet"]["status"] == "open" and "OCO" in events[0]
    assert binance_testnet.sync(api, ledger) == []  # nothing new on a quiet day

    ledger["closed"].append(ledger["open"].pop())  # the paper stop was hit
    events = binance_testnet.sync(api, ledger)
    assert [c[0] for c in api.calls[-2:]] == ["cancel", "sell"]
    assert ledger["closed"][0]["testnet"]["status"] == "ditutup mengikuti paper"

    api2 = _FakeTestnet()
    ledger2 = {"open": [_open_btc()], "closed": []}
    binance_testnet.sync(api2, ledger2)
    api2.status[11] = "FILLED"  # testnet reached its own target first
    assert "LIMIT_MAKER" in binance_testnet.sync(api2, ledger2)[0]


@pytest.mark.unit
def test_testnet_failure_on_one_coin_is_reported_not_raised():
    from idx_bot import binance_testnet
    api = _FakeTestnet(fail_on="BTCUSDT")
    ledger = {"open": [_open_btc()], "closed": []}
    events = binance_testnet.sync(api, ledger)
    assert "GAGAL" in events[0] and ledger["open"][0]["testnet"]["status"] == "gagal"
