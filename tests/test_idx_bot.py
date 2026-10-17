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
    assert plan["risk_idr"] <= config.EQUITY * config.RISK_PER_TRADE


@pytest.mark.unit
def test_plan_trade_capped_by_cash():
    plan = plan_trade(entry=1000, atr_value=1, cash=2_000_000)
    assert plan["position_value"] <= 2_000_000
    assert plan["lots"] == 19  # 20 lots would exceed cash once the buy fee is added


@pytest.mark.unit
def test_plan_trade_respects_six_percent_rule():
    # 300k open risk + 250k lost this month leaves 50k of the 600k monthly cap.
    plan = plan_trade(entry=1000, atr_value=25, open_risk=300_000, month_loss=250_000)
    assert plan["risk_idr"] <= 50_000
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
