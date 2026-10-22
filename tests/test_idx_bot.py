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


@pytest.mark.unit
def test_rupiah_crypto_book_converts_prices_but_not_volume():
    from idx_bot import crypto
    df = _bars([[1.0, 2.0, 0.5, 1.5]])
    df["Volume"] = 1e7
    out = crypto.in_currency({"X": df}, 18_000)["X"]
    assert out["Close"].iloc[0] == 27_000 and out["Low"].iloc[0] == 9_000
    assert out["Volume"].iloc[0] == 1e7  # liquidity stays judged in USD
    assert df["Close"].iloc[0] == 1.5  # the input is left alone
    plan = plan_trade(entry=1_470_000_000, atr_value=38_000_000, market=config.INDODAX)
    assert plan["risk"] <= config.INDODAX.equity * config.RISK_PER_TRADE  # Rp 200 rb
    assert plan["units"] == pytest.approx(0.00263157, abs=1e-8)


_INDODAX_PAIRS = [
    {"id": "btcidr", "symbol": "BTCIDR", "base_currency": "idr", "traded_currency": "btc", "quantity_increment": 1e-8,
     "price_precision": 1000, "trade_min_base_currency": 10000, "trade_fee_percent_taker": 0.2,
     "is_market_suspended": 0, "is_maintenance": 0},
    {"id": "aptidr", "symbol": "APTIDR", "old_ticker_id": "apt_old_idr", "base_currency": "idr",
     "traded_currency": "apt", "quantity_increment": 1e-8,
     "price_precision": 1, "trade_min_base_currency": 10000, "trade_fee_percent_taker": 0.2,
     "is_market_suspended": 1, "is_maintenance": 0},
    {"id": "btcusdt", "symbol": "BTCUSDT", "base_currency": "usdt", "traded_currency": "btc", "quantity_increment": 1e-8,
     "price_precision": 1, "trade_min_base_currency": 5, "trade_fee_percent_taker": 0.2},
]


@pytest.mark.unit
def test_indodax_rules_and_candles(monkeypatch):
    from idx_bot import indodax_feed

    def fake_get(path, **params):
        if path == "/api/pairs":
            return _INDODAX_PAIRS
        assert params["symbol"] == "BTCIDR" and params["tf"] == "1D"
        return [{"Time": 1760659200, "Open": 1_450_000_000, "High": 1_480_000_000, "Low": 1_440_000_000,
                 "Close": 1_470_000_000, "Volume": "8.5"}]
    monkeypatch.setattr(indodax_feed, "_get", fake_get)
    rules = indodax_feed.trading_rules()
    assert set(rules) == {"BTC", "APT"}  # the USDT pair is left out
    assert rules["BTC"] == {"step": 1e-8, "tick": 1000.0, "min_notional": 10000.0, "taker_fee": 0.002,
                            "suspended": False, "pair_id": "btcidr", "chart_symbols": ["BTCIDR"]}
    assert rules["APT"]["suspended"] is True and rules["APT"]["chart_symbols"] == ["APTIDR", "APTOLDIDR"]
    df = indodax_feed.klines("BTC", days=1)
    assert df["Close"].iloc[0] == 1_470_000_000
    assert df["Volume"].iloc[0] == pytest.approx(8.5 * 1_470_000_000)  # turnover in rupiah
    assert df.index[0].strftime("%Y-%m-%d") == "2025-10-17"


@pytest.mark.unit
def test_rupiah_fetch_falls_back_to_converted_usd(monkeypatch):
    from idx_bot import crypto, indodax_feed

    def down(code, days):
        raise ConnectionError("indodax down")
    monkeypatch.setattr(indodax_feed, "klines", down)
    usd = _bars([[10.0, 11.0, 9.0, 10.5]], start="2026-10-01")
    usd["Volume"] = 1e6
    monkeypatch.setattr(crypto, "fetch", lambda codes, period: ({"BTC": usd}, "Binance"))
    monkeypatch.setattr(crypto, "usd_idr", lambda: 18_000)
    prices, source = crypto.fetch_rupiah(["BTC"])
    assert source == "Binance x kurs USD/IDR"
    assert prices["BTC"]["Close"].iloc[0] == 189_000 and prices["BTC"]["Volume"].iloc[0] == 18e9


@pytest.mark.unit
def test_indodax_costs_and_rules_in_sizing():
    rules = {"step": 1e-8, "tick": 1000.0, "min_notional": 10000.0}
    plan = plan_trade(entry=1_470_574_000, atr_value=38_000_000, market=config.INDODAX, rules=rules)
    assert plan["stop"] % 1000 == 0 and plan["target"] % 1000 == 0  # Rp 1.000 price steps for BTC
    assert plan["risk"] <= 200_000
    assert config.INDODAX.buy_fee == pytest.approx(0.002 * 1.11)
    assert config.INDODAX.sell_fee == pytest.approx(0.002 * 1.11 + 0.0021)


@pytest.mark.unit
def test_local_premium_flags_thin_indodax_books():
    from idx_bot import crypto
    world = {"BCH": _bars([[276, 280, 270, 276.8]], start="2026-10-16"),
             "BTC": _bars([[82_000, 82_500, 81_000, 82_116]], start="2026-10-16")}
    local = {"BCH": _bars([[7_700_000] * 4], start="2026-10-16"),
             "BTC": _bars([[1_478_000_000] * 4], start="2026-10-16")}
    prem = crypto.local_premium(local, world, 18_000)
    assert prem["BCH"] == pytest.approx(7_700_000 / (276.8 * 18_000) - 1, abs=1e-4)  # about +55%
    assert abs(prem["BTC"]) < config.MAX_LOCAL_PREMIUM < prem["BCH"]


@pytest.mark.unit
def test_indodax_chart_falls_back_to_old_symbol(monkeypatch):
    from idx_bot import indodax_feed
    seen = []

    def fake_get(path, **params):
        seen.append(params["symbol"])
        return [] if params["symbol"] == "XLMIDR" else [
            {"Time": 1760659200, "Open": 3400, "High": 3427, "Low": 3280, "Close": 3287, "Volume": "10"}]
    monkeypatch.setattr(indodax_feed, "_get", fake_get)
    df = indodax_feed.klines("XLM", 5, ["XLMIDR", "STRIDR"])
    assert seen == ["XLMIDR", "STRIDR"] and df["Close"].iloc[0] == 3287
    monkeypatch.setattr(indodax_feed, "_get", lambda path, **p: [])
    with pytest.raises(LookupError):
        indodax_feed.klines("APT", 5)


@pytest.mark.unit
def test_buy_cost_walks_the_asks():
    from idx_bot.indodax_feed import buy_cost
    book = {"buy": [["99", "10"]], "sell": [["101", "1"], ["102", "1"], ["110", "5"]]}
    # Rp 101 buys one coin at the best ask: 1% over the 100 mid
    assert buy_cost(book, 101) == pytest.approx(0.01)
    # Rp 203 takes both first levels: average 101.5
    assert buy_cost(book, 203) == pytest.approx(203 / 2 / 100 - 1)
    assert buy_cost(book, 10_000) is None  # more than the whole book
    assert buy_cost({"buy": [], "sell": [["1", "1"]]}, 1) is None


@pytest.mark.unit
def test_new_indicators_shapes_and_ranges():
    import numpy as np

    from idx_bot.indicators import adx, bollinger, stochastic
    idx = pd.date_range("2025-01-01", periods=120)
    up = pd.Series(np.linspace(100, 160, 120), index=idx)
    df = pd.DataFrame({"Open": up, "High": up * 1.02, "Low": up * 0.98, "Close": up, "Volume": 1e6})
    bb = bollinger(df["Close"]).iloc[-1]
    assert bb["lower"] < bb["mid"] < bb["upper"] and 0 <= bb["pct_b"] <= 1.2
    st = stochastic(df).iloc[-1]
    assert 0 <= st["k"] <= 100 and st["k"] > 50  # rising series sits high in its range
    assert adx(df).iloc[-1] > 25  # a clean trend reads as strong


@pytest.mark.unit
def test_confirmation_signals_read_in_plain_language():
    from idx_bot.screener import _signals
    strong = _signals(pct_b=1.1, stoch_k=85, stoch_d=80, adx_v=40)
    assert "tren kuat (ADX > 25)" in strong["notes"]
    assert any("Bollinger atas" in n for n in strong["notes"])
    assert any("jenuh beli (>80)" in n for n in strong["notes"])
    weak = _signals(pct_b=0.5, stoch_k=50, stoch_d=50, adx_v=15)
    assert weak["notes"] == ["tren lemah/sideways (ADX < 25)"]
    blank = _signals(pct_b=float("nan"), stoch_k=float("nan"), stoch_d=float("nan"), adx_v=float("nan"))
    assert blank["notes"] == [] and blank["adx"] is None


@pytest.mark.unit
def test_obv_follows_price_direction():
    from idx_bot.indicators import obv
    idx = pd.date_range("2025-01-01", periods=5)
    up = pd.DataFrame({"Close": [10, 11, 12, 13, 14.0], "Volume": [100.0] * 5}, index=idx)
    down = pd.DataFrame({"Close": [14, 13, 12, 11, 10.0], "Volume": [100.0] * 5}, index=idx)
    assert obv(up).iloc[-1] == pytest.approx(400)   # four up days add volume
    assert obv(down).iloc[-1] == pytest.approx(-400)  # four down days subtract it


@pytest.mark.unit
def test_chaikin_money_flow_sign_matches_close_within_range():
    from idx_bot.indicators import chaikin_money_flow
    idx = pd.date_range("2025-01-01", periods=25)
    # Close pinned at the high of each bar: strong accumulation, CMF near +1.
    hi = pd.DataFrame({"High": 101.0, "Low": 99.0, "Close": 101.0, "Volume": 1e6}, index=idx)
    lo = pd.DataFrame({"High": 101.0, "Low": 99.0, "Close": 99.0, "Volume": 1e6}, index=idx)
    assert chaikin_money_flow(hi).iloc[-1] == pytest.approx(1.0)
    assert chaikin_money_flow(lo).iloc[-1] == pytest.approx(-1.0)


@pytest.mark.unit
def test_accumulation_and_distribution_notes():
    from idx_bot.screener import _signals
    accum = _signals(pct_b=0.5, stoch_k=50, stoch_d=50, adx_v=30, cmf=0.2, obv_up=True)
    assert any("akumulasi" in n for n in accum["notes"]) and accum["cmf"] == 0.2 and accum["obv_naik"] is True
    distr = _signals(pct_b=0.5, stoch_k=50, stoch_d=50, adx_v=30, cmf=-0.2, obv_up=False)
    assert any("distribusi" in n for n in distr["notes"])
    # Mixed or flat flow earns neither note.
    mixed = _signals(pct_b=0.5, stoch_k=50, stoch_d=50, adx_v=30, cmf=0.2, obv_up=False)
    assert not any("akumulasi" in n or "distribusi" in n for n in mixed["notes"])


def _ohlc(closes, spread=0.01):
    close = pd.Series(closes, dtype=float, index=pd.bdate_range("2025-01-01", periods=len(closes)))
    return pd.DataFrame({"Open": close, "High": close * (1 + spread), "Low": close * (1 - spread),
                         "Close": close, "Volume": 1e6})


@pytest.mark.unit
def test_chart_patterns_breakout_breakdown_and_levels():
    from idx_bot.screener import chart_patterns
    flat = [100.0] * 40
    up = chart_patterns(_ohlc(flat + [110.0]))
    assert "breakout: tembus titik tertinggi 20 hari" in up["notes"]
    assert up["resistance"] == pytest.approx(101.0) and up["support"] == pytest.approx(99.0)
    down = chart_patterns(_ohlc(flat + [90.0]))
    assert "breakdown: tembus titik terendah 20 hari" in down["notes"]
    near = chart_patterns(_ohlc(flat + [100.5]))
    assert near["notes"] == ["dekat resistance 20 hari"]


@pytest.mark.unit
def test_chart_patterns_fresh_golden_cross_only():
    from idx_bot.indicators import sma
    from idx_bot.screener import chart_patterns
    closes = list(np.linspace(2000, 1000, 250)) + list(np.linspace(1000, 2500, 120))
    df = _ohlc(closes)
    above = sma(df["Close"], 50) > sma(df["Close"], 200)
    cross = next(i for i in range(201, len(df)) if above.iloc[i] and not above.iloc[i - 1])
    fresh = chart_patterns(df.iloc[:cross + 3])
    assert any("golden cross" in n for n in fresh["notes"])
    old = chart_patterns(df.iloc[:cross + 30])  # a month later the cross is no longer news
    assert not any("golden cross" in n for n in old["notes"])


@pytest.mark.unit
def test_recent_bars_and_history_csv(tmp_path):
    import csv as _csv

    from idx_bot import daily
    from idx_bot.screener import recent_bars
    bars = recent_bars(_ohlc(np.linspace(100, 200, 100)))
    assert len(bars) == 63 and bars[-1]["close"] == pytest.approx(200)
    daily._write_history(tmp_path, {"BBCA": bars})
    with open(tmp_path / "harga-3bulan.csv", newline="") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 63 and rows[0]["kode"] == "BBCA" and float(rows[-1]["close"]) == pytest.approx(200)


@pytest.mark.unit
def test_signal_levels_blank_when_missing():
    from idx_bot import daily
    assert daily._signal_levels({"signals": {"cmf": 0.2, "support": 90.0, "resistance": 110.0}}) == [0.2, 90.0, 110.0]
    assert daily._signal_levels({}) == ["", "", ""]


@pytest.mark.unit
def test_watchlist_reads_codes_per_market(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(config, "WATCHLIST_FILE", path)
    assert config.watchlist("idx") == []  # no file yet
    path.write_text('{"idx": ["bbca", "GOTO"], "crypto": ["BTC"]}')
    assert config.watchlist("idx") == ["BBCA", "GOTO"]
    assert config.watchlist("crypto") == ["BTC"]


@pytest.mark.unit
def test_shipped_watchlist_file_is_valid():
    import json
    data = json.loads(config.WATCHLIST_FILE.read_text())
    assert set(data) == {"idx", "crypto"} and all(isinstance(v, list) for v in data.values())


def _bars15(day, rows):
    """15-minute WIB bars from 09:00: rows of (open, high, low, close)."""
    idx = pd.date_range(f"{day} 09:00", periods=len(rows), freq="15min", tz="Asia/Jakarta")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"]).assign(Volume=1e5)


def _intraday_ledger():
    return {"cash": 10_000_000.0, "pending": [], "closed": [], "settled_through": "2026-10-21",
            "open": [{"code": "PTBA", "lots": 5, "units": 500, "entry": 3050.0, "stop": 2710, "target": 3730,
                      "signal_date": "2026-10-20", "entry_date": "2026-10-21", "cost": 1_528_000.0,
                      "reason": "uji"}]}


@pytest.mark.unit
def test_intraday_sweep_hits_stop_in_time_order_and_remembers_bars():
    from idx_bot import intraday
    ledger = _intraday_ledger()
    calm = _bars15("2026-10-22", [(3100, 3150, 3050, 3120), (3120, 3160, 3100, 3140)])
    assert intraday.sweep(ledger, {"PTBA": calm}) == []
    assert ledger["open"][0]["seen_until"].startswith("2026-10-22T09:15")
    # The same bars again change nothing; a later bar through the stop closes at the stop.
    later = _bars15("2026-10-22", [(3100, 3150, 3050, 3120), (3120, 3160, 3100, 3140), (2900, 2950, 2700, 2720)])
    ev = intraday.sweep(ledger, {"PTBA": later})
    assert ledger["open"] == [] and ledger["closed"][0]["exit_price"] == 2710
    assert ev[0]["kind"] == "jual" and "stop-loss" in ev[0]["text"] and "09:30" in ev[0]["text"]


@pytest.mark.unit
def test_intraday_sweep_gap_below_stop_fills_at_open_and_target_works():
    from idx_bot import intraday
    gap = _intraday_ledger()
    intraday.sweep(gap, {"PTBA": _bars15("2026-10-22", [(2600, 2650, 2550, 2600)])})
    assert gap["closed"][0]["exit_price"] == 2600
    win = _intraday_ledger()
    intraday.sweep(win, {"PTBA": _bars15("2026-10-22", [(3600, 3750, 3590, 3700)])})
    assert win["closed"][0]["exit_reason"] == "target" and win["closed"][0]["exit_price"] == 3730


@pytest.mark.unit
def test_intraday_fill_resizes_at_price_and_waits_when_price_ran():
    from idx_bot import intraday
    order = {"code": "ADRO", "lots": 10, "units": 1000, "entry": 2000.0, "stop": 1800, "target": 2400,
             "signal_date": "2026-10-21", "reason": "uji"}
    ledger = {"cash": 10_000_000.0, "pending": [dict(order)], "open": [], "closed": []}
    ev = intraday.fill_orders(ledger, {"ADRO": _bars15("2026-10-22", [(2000, 2030, 1990, 2020)])}, "2026-10-22")
    pos = ledger["open"][0]
    assert ev[0]["kind"] == "beli" and pos["entry"] == 2020 and pos["entry"] - pos["stop"] == 200
    assert (pos["entry"] - pos["stop"]) * pos["units"] <= 200_000 and pos["planned_entry"] == 2000
    assert pos["entry_time"] == pos["seen_until"] and ledger["pending"] == []
    ran = {"cash": 10_000_000.0, "pending": [dict(order)], "open": [], "closed": []}
    ev = intraday.fill_orders(ran, {"ADRO": _bars15("2026-10-22", [(2100, 2120, 2090, 2110)])}, "2026-10-22")
    assert ev[0]["kind"] == "tunggu" and ran["open"] == [] and ran["pending"][0]["intraday_checked"]
    # Orders queued this evening wait for tomorrow; one below its stop is cancelled.
    today = {"cash": 1e7, "pending": [{**order, "signal_date": "2026-10-22"}], "open": [], "closed": []}
    assert intraday.fill_orders(today, {"ADRO": _bars15("2026-10-22", [(2000,) * 4])}, "2026-10-22") == []
    low = {"cash": 1e7, "pending": [dict(order)], "open": [], "closed": []}
    ev = intraday.fill_orders(low, {"ADRO": _bars15("2026-10-22", [(1790, 1800, 1780, 1790)])}, "2026-10-22")
    assert ev[0]["kind"] == "batal" and low["pending"] == [] and low["open"] == []


@pytest.mark.unit
def test_daily_settle_skips_a_morning_low_before_an_intraday_fill():
    from idx_bot import intraday, paper
    order = {"code": "ADRO", "lots": 10, "units": 1000, "entry": 2000.0, "stop": 1800, "target": 2400,
             "signal_date": "2026-10-21", "reason": "uji"}
    ledger = {"cash": 1e7, "pending": [order], "open": [], "closed": []}
    # Filled at 13:45 after a morning dip to 1,750 that came before the fill.
    bars = _bars15("2026-10-22", [(1900, 1910, 1750, 1800)] + [(2000, 2030, 1990, 2020)] * 20)
    intraday.fill_orders(ledger, {"ADRO": bars.iloc[:19]}, "2026-10-22")
    intraday.close_day(ledger, {"ADRO": bars}, "2026-10-22")
    day = pd.DataFrame({"Open": [1900.0], "High": [2030.0], "Low": [1750.0], "Close": [2020.0]},
                       index=pd.to_datetime(["2026-10-22"]))
    assert paper.settle(ledger, {"ADRO": day}) == [] and len(ledger["open"]) == 1
    assert ledger["open"][0]["swept_day"] == "2026-10-22"


@pytest.mark.unit
def test_close_day_cancels_orders_the_checks_could_not_fill():
    from idx_bot import intraday
    ledger = {"cash": 1e7, "open": [], "closed": [], "pending": [
        {"code": "A", "signal_date": "2026-10-21", "intraday_checked": True},
        {"code": "B", "signal_date": "2026-10-22"}]}
    ev = intraday.close_day(ledger, {}, "2026-10-22")
    assert [o["code"] for o in ledger["pending"]] == ["B"] and ev[0]["kind"] == "batal"


@pytest.mark.unit
def test_sweep_does_not_mark_a_bar_still_forming():
    from datetime import datetime as dt

    from idx_bot import intraday
    ledger = _intraday_ledger()
    bars = _bars15("2026-10-22", [(3100, 3150, 3050, 3120), (3120, 3160, 3100, 3140)])
    now = dt.fromisoformat("2026-10-22T09:20:00+07:00")  # the 09:15 bar has five minutes to go
    intraday.sweep(ledger, {"PTBA": bars}, now=now)
    assert ledger["open"][0]["seen_until"].startswith("2026-10-22T09:00")


@pytest.mark.unit
def test_crypto_days_are_utc_and_bars_use_chart_codes(monkeypatch):
    from datetime import UTC, datetime as dt

    from idx_bot import indodax_feed, intraday
    now = dt(2026, 10, 22, 0, 30, tzinfo=UTC)  # 07:30 WIB
    assert intraday.market_day(config.INDODAX, now) == "2026-10-22"
    assert intraday.market_day(config.IDX, now) == "2026-10-22"
    assert intraday.last_settled_day(config.INDODAX, now) == "2026-10-21"
    calls = []

    def fake(code, start, end, symbols=None):
        calls.append((code, start, end, symbols))
        idx = pd.date_range("2026-10-21 23:45", periods=3, freq="15min", tz="UTC")
        return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)
    monkeypatch.setattr(indodax_feed, "klines_15m", fake)
    out = intraday.crypto_bars(["XLM"], "2026-10-22", {"XLM": {"chart_symbols": ["XLMIDR", "STRIDR"]}})
    assert calls[0][3] == ["XLMIDR", "STRIDR"] and calls[0][2] - calls[0][1] == 86_399
    assert len(out["XLM"]) == 2  # the 23:45 bar of the 21st is left out
