"""
Analysis 3: Which fundamentals actually correlated with forward returns?

Question: did revenue/margin/leverage/EPS levels predict subsequent stock
performance over this period?

Method: cross-sectional quintile spread (standard factor-investing test)
  1. For each fundamentals report, compute three factors:
       - net_margin      = net_income / revenue
       - debt_to_equity  = total_debt / total_equity
       - eps             (raw)
  2. Compute the forward return: the stock's actual return over the ~63
     trading days (roughly one quarter) AFTER the fundamentals' real
     public effective_date — using the same trading-day-offset technique
     as the event study, so this never looks at data before it existed.
  3. Within each fiscal quarter (cross-sectionally, so all companies are
     compared against the SAME market conditions), rank companies into
     quintiles by each factor and compare the average forward return of
     the top quintile vs. the bottom quintile. This "spread" is the
     standard long-short factor test — more robust than a raw pooled
     correlation, since it controls for market-wide moves that would
     otherwise swamp the signal.

IMPORTANT LIMITATIONS (read before trusting the numbers):
  - SURVIVORSHIP BIAS: the ticker universe is the CURRENT S&P 500.
    Companies removed from the index during 2021-2026 (bankruptcy,
    underperformance, acquisition) are almost entirely absent. This
    biases results toward companies that turned out to survive/succeed —
    a classic and well-known backtesting flaw, not fixed here.
  - Only 3 factors tested, no interaction effects, no risk adjustment
    (e.g. no comparison to a market benchmark return).
  - ~5 years / ~20 quarterly cohorts is a modest sample for this kind of
    analysis; treat results as exploratory, not a validated strategy.

Usage:
    python analysis/fundamentals_factors.py
"""

import os
import sys

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
FORWARD_WINDOW_TRADING_DAYS = 63  # ~1 quarter
# Only rank companies on fundamentals released within this many days of the
# period end. A handful of periods (~9%) are only fully public much later,
# because one of their values first appeared as a comparative in a later
# filing; their effective_date is correct (no look-ahead) but the data is stale.
MAX_REPORTING_LAG_DAYS = 120


def load_panel(con) -> pd.DataFrame:
    sql = f"""
    WITH trading AS (
        SELECT company_id, date_id, COALESCE(adj_close, close) AS px,
               ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY date_id) AS rn
        FROM analytics_daily
        WHERE COALESCE(adj_close, close) IS NOT NULL
    ),
    anchors AS (
        SELECT f.company_id, f.fiscal_period_end, f.effective_date,
               f.revenue, f.net_income, f.total_debt, f.total_equity, f.eps,
               t.rn AS anchor_rn, t.px AS anchor_close
        FROM fundamentals_dated f
        ASOF JOIN trading t
            ON f.company_id = t.company_id AND t.date_id <= f.effective_date
    ),
    forward AS (
        SELECT a.*, t2.px AS forward_close
        FROM anchors a
        JOIN trading t2
            ON t2.company_id = a.company_id
            AND t2.rn = a.anchor_rn + {FORWARD_WINDOW_TRADING_DAYS}
    )
    SELECT
        f.company_id, c.ticker, c.sector,
        f.fiscal_period_end, f.effective_date,
        f.revenue, f.net_income, f.total_debt, f.total_equity, f.eps,
        f.anchor_close, f.forward_close,
        (f.forward_close / f.anchor_close - 1) AS forward_return
    FROM forward f
    JOIN dim_company c ON f.company_id = c.company_id
    """
    return con.execute(sql).df()


def add_factors(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["revenue"] = df["revenue"].astype(float)
    df["total_equity"] = df["total_equity"].astype(float)
    df["net_margin"] = df["net_income"] / df["revenue"].replace(0, np.nan)
    df["debt_to_equity"] = df["total_debt"] / df["total_equity"].replace(0, np.nan)
    # drop economically nonsensical values (e.g. negative equity making D/E meaningless)
    df.loc[df["total_equity"] <= 0, "debt_to_equity"] = np.nan
    df["quarter_cohort"] = pd.PeriodIndex(df["fiscal_period_end"], freq="Q")
    lag_days = (pd.to_datetime(df["effective_date"]) - pd.to_datetime(df["fiscal_period_end"])).dt.days
    df = df[lag_days <= MAX_REPORTING_LAG_DAYS]
    return df


def quintile_spread(df: pd.DataFrame, factor: str) -> pd.DataFrame:
    """For each quarter cohort, rank companies into quintiles by `factor`
    and return the TOP-quintile-by-factor-value minus BOTTOM-quintile-by-
    -factor-value average forward return.

    IMPORTANT: this must be by_q.loc[4] - by_q.loc[0], NOT by_q.max() -
    by_q.min(). pd.qcut(..., labels=False) assigns label 0 to the lowest
    factor values and label 4 to the highest (ascending bins), so loc[4]
    is genuinely "high factor value" and loc[0] is genuinely "low factor
    value" — and the difference can be negative if low-factor companies
    outperformed. max()-min() instead just takes whichever two quintiles
    happen to have the highest/lowest MEAN RETURN, regardless of which
    quintile that actually is — that number is non-negative for any
    input data whatsoever and says nothing about the factor's direction.
    (An earlier version of this function had exactly this bug.)
    """
    results = []
    for period, group in df.dropna(subset=[factor, "forward_return"]).groupby("quarter_cohort"):
        if len(group) < 20:  # need enough companies for a meaningful quintile split
            continue
        try:
            group = group.copy()
            group["quintile"] = pd.qcut(group[factor], 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        if group["quintile"].nunique() < 5:
            continue
        by_q = group.groupby("quintile")["forward_return"].mean()
        spread = by_q.loc[4] - by_q.loc[0]  # top quintile minus bottom quintile, by factor rank
        results.append({"quarter": str(period), "n_companies": len(group), "spread": spread})
    return pd.DataFrame(results)


def spread_tstat(spreads: pd.DataFrame) -> float:
    """t-statistic of the mean quarterly spread (mean / standard error across
    quarters). Each quarter is one observation, so cross-sectional
    correlation between stocks in the same quarter doesn't inflate it."""
    s = spreads["spread"]
    if len(s) < 2 or s.std() == 0:
        return float("nan")
    return s.mean() / (s.std() / np.sqrt(len(s)))


def main():
    con = duckdb.connect(DB_PATH, read_only=True)
    panel = load_panel(con)
    con.close()

    panel = add_factors(panel)
    print(f"Loaded {len(panel)} fundamentals-with-forward-return rows across "
          f"{panel['company_id'].nunique()} companies and "
          f"{panel['quarter_cohort'].nunique()} fiscal quarters.\n")

    for factor in ["net_margin", "debt_to_equity", "eps"]:
        print("=" * 70)
        print(f"FACTOR: {factor}")
        print("=" * 70)
        spreads = quintile_spread(panel, factor)
        if spreads.empty:
            print("  Not enough data per quarter to compute quintile spreads.\n")
            continue
        avg_spread = spreads["spread"].mean()
        print(f"  Avg quarterly top-vs-bottom quintile spread: {avg_spread:+.4f} "
              f"({avg_spread*100:+.2f}%)")
        print(f"  Computed across {len(spreads)} quarters "
              f"(avg {spreads['n_companies'].mean():.0f} companies/quarter), "
              f"positive in {int((spreads['spread'] > 0).sum())}")
        print(f"  t-stat of mean spread: {spread_tstat(spreads):+.2f}  (|t| < 2 = not significant)")
        pooled_data = panel[[factor, "forward_return"]].dropna().astype(float)
        pooled_corr = pooled_data[factor].corr(pooled_data["forward_return"]) if len(pooled_data) > 1 else float("nan")
        print(f"  (for reference) pooled Pearson correlation: {pooled_corr:+.4f}\n")

    print("""
Reading this:
  - Positive spread = the highest-factor-value quintile outperformed the
    lowest-factor-value quintile over the following quarter, on average.
  - Negative spread = the opposite (high factor value underperformed).

LIMITATIONS — repeat these in any write-up:
  - Survivorship bias: current S&P 500 membership only. Companies that
    failed or were removed during this period are largely absent, which
    should make ALL of these numbers look more favorable than a true
    point-in-time backtest would.
  - No risk adjustment, no transaction costs, no benchmark comparison —
    this identifies correlation/pattern, not a tradeable strategy.
""")


if __name__ == "__main__":
    main()
