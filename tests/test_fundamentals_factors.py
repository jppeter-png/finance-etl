"""Tests for the quintile-spread factor test in analysis/fundamentals_factors.py,
using tiny synthetic panels where the right answer is known."""

import numpy as np
import pandas as pd
import pytest

from fundamentals_factors import add_factors, quintile_spread, spread_tstat


def _cohort(factor_values, returns, quarter="2023Q1"):
    return pd.DataFrame({
        "quarter_cohort": pd.Period(quarter, freq="Q"),
        "factor": factor_values,
        "forward_return": returns,
    })


def test_spread_is_negative_when_low_factor_stocks_outperform():
    # regression test for the original bug: spread was max(quintile mean) -
    # min(quintile mean), which can never be negative
    factor = np.arange(50, dtype=float)
    returns = -factor / 100  # higher factor -> lower return
    out = quintile_spread(_cohort(factor, returns), "factor")
    assert len(out) == 1
    assert out["spread"].iloc[0] < 0


def test_spread_is_top_quintile_minus_bottom_quintile():
    factor = np.arange(50, dtype=float)  # 10 companies per quintile
    returns = np.repeat([0.05, 0.0, 0.0, 0.0, 0.02], 10)
    out = quintile_spread(_cohort(factor, returns), "factor")
    # top (0.02) - bottom (0.05), not max - min (0.05 - 0.0)
    assert out["spread"].iloc[0] == pytest.approx(-0.03)


def test_spread_skips_quarters_with_too_few_companies():
    factor = np.arange(10, dtype=float)
    out = quintile_spread(_cohort(factor, factor / 100), "factor")
    assert out.empty


def test_spread_is_computed_per_quarter():
    factor = np.arange(50, dtype=float)
    panel = pd.concat([
        _cohort(factor, factor / 100, "2023Q1"),   # high factor wins
        _cohort(factor, -factor / 100, "2023Q2"),  # low factor wins
    ])
    out = quintile_spread(panel, "factor").set_index("quarter")["spread"]
    assert out["2023Q1"] > 0
    assert out["2023Q2"] < 0


def test_tstat_sign_and_zero_variance():
    assert spread_tstat(pd.DataFrame({"spread": [0.01, 0.02, 0.03]})) > 0
    assert spread_tstat(pd.DataFrame({"spread": [-0.01, -0.02, -0.03]})) < 0
    assert np.isnan(spread_tstat(pd.DataFrame({"spread": [0.01, 0.01]})))


def _raw_row(period_end, effective, **overrides):
    row = {
        "fiscal_period_end": pd.Timestamp(period_end),
        "effective_date": pd.Timestamp(effective),
        "revenue": 100.0, "net_income": 10.0,
        "total_debt": 50.0, "total_equity": 25.0, "eps": 1.0,
    }
    row.update(overrides)
    return row


def test_add_factors_computes_ratios():
    out = add_factors(pd.DataFrame([_raw_row("2023-03-31", "2023-05-01")]))
    assert out["net_margin"].iloc[0] == pytest.approx(0.10)
    assert out["debt_to_equity"].iloc[0] == pytest.approx(2.0)


def test_add_factors_blanks_debt_to_equity_for_negative_equity():
    out = add_factors(pd.DataFrame([_raw_row("2023-03-31", "2023-05-01", total_equity=-5.0)]))
    assert np.isnan(out["debt_to_equity"].iloc[0])


def test_add_factors_drops_stale_fundamentals():
    out = add_factors(pd.DataFrame([
        _raw_row("2023-03-31", "2023-05-01"),  # 31-day lag: kept
        _raw_row("2022-03-31", "2023-05-01"),  # released a year later: dropped
    ]))
    assert list(out["fiscal_period_end"]) == [pd.Timestamp("2023-03-31")]
