"""
HISTORICAL NOTE: this diagnostic was written to explain why net_margin
showed a strong positive quintile spread (+6.44%) but a near-zero pooled
Pearson correlation (-0.005). That disagreement turned out to be an
artifact of a bug in quintile_spread() (max - min instead of top - bottom
quintile), since fixed. With that fix and point-in-time (first-release)
fundamentals, the spread is -1.00% and not statistically significant. The outlier checks below are still
valid on their own.

Original question: why does net_margin show a strong positive quintile
spread but a near-zero pooled Pearson correlation?

Checks two leading hypotheses:
  1. OUTLIERS: Pearson correlation is sensitive to extreme values.
     net_margin = net_income / revenue can blow up to absurd magnitudes
     when revenue is very small, while the quintile-spread method only
     uses rank order and is robust to this.
  2. TIME-VARYING RELATIONSHIP: the effect might be real within most
     quarters but flip sign in others, canceling out in a pooled
     (all-quarters-mixed) correlation while still showing up as a
     positive average when each quarter is tested separately (which is
     what the quintile method does).

Usage:
    python analysis/net_margin_diagnostic.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from fundamentals_factors import load_panel, add_factors, DB_PATH  # noqa: E402

import duckdb
import numpy as np
import pandas as pd


def main():
    con = duckdb.connect(DB_PATH, read_only=True)
    panel = load_panel(con)
    con.close()
    panel = add_factors(panel)

    nm = panel.dropna(subset=["net_margin", "forward_return"]).copy()

    print("=" * 70)
    print("CHECK 1: distribution of net_margin (looking for outliers)")
    print("=" * 70)
    print(nm["net_margin"].describe(percentiles=[0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]))
    extreme = nm[(nm["net_margin"] < -1) | (nm["net_margin"] > 1)]
    print(f"\nRows with net_margin outside [-1, 1] (i.e. |margin| > 100%): "
          f"{len(extreme)} of {len(nm)} ({len(extreme)/len(nm)*100:.1f}%)")
    if len(extreme) > 0:
        print(extreme[["ticker", "fiscal_period_end", "revenue", "net_income", "net_margin", "forward_return"]]
              .sort_values("net_margin").head(10).to_string(index=False))
        print("...")
        print(extreme[["ticker", "fiscal_period_end", "revenue", "net_income", "net_margin", "forward_return"]]
              .sort_values("net_margin").tail(10).to_string(index=False))

    print("\n" + "=" * 70)
    print("CHECK 1b: correlation after clipping outliers (winsorizing at 1st/99th pct)")
    print("=" * 70)
    lo, hi = nm["net_margin"].quantile([0.01, 0.99])
    nm["net_margin_clipped"] = nm["net_margin"].clip(lo, hi)
    corr_raw = nm["net_margin"].corr(nm["forward_return"])
    corr_clipped = nm["net_margin_clipped"].corr(nm["forward_return"])
    print(f"  Pearson correlation, raw net_margin:      {corr_raw:+.4f}")
    print(f"  Pearson correlation, clipped net_margin:   {corr_clipped:+.4f}")

    print("\n" + "=" * 70)
    print("CHECK 2: does the quarterly spread flip sign over time?")
    print("=" * 70)
    rows = []
    for period, group in nm.groupby("quarter_cohort"):
        if len(group) < 20:
            continue
        try:
            group = group.copy()
            group["quintile"] = pd.qcut(group["net_margin"], 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        if group["quintile"].nunique() < 5:
            continue
        by_q = group.groupby("quintile")["forward_return"].mean()
        spread = by_q.max() - by_q.min()
        rows.append({"quarter": str(period), "n": len(group), "spread": spread})

    spread_df = pd.DataFrame(rows).sort_values("quarter")
    print(spread_df.to_string(index=False))
    n_positive = (spread_df["spread"] > 0).sum()
    n_negative = (spread_df["spread"] < 0).sum()
    print(f"\nPositive-spread quarters: {n_positive} / {len(spread_df)}")
    print(f"Negative-spread quarters: {n_negative} / {len(spread_df)}")
    print(f"Std dev of quarterly spread: {spread_df['spread'].std():.4f} "
          f"(vs. mean {spread_df['spread'].mean():.4f})")

    print("\n" + "=" * 70)
    print("CHECK 3: pooled correlation after removing the quarter (market-wide) effect")
    print("=" * 70)
    print("If pooled correlation was near zero because between-quarter market swings")
    print("swamp the within-quarter margin effect, then correlating each company's")
    print("return RELATIVE TO ITS QUARTER'S AVERAGE (i.e. demeaned) should recover")
    print("a positive relationship close to what the quintile spread already found.\n")
    nm["forward_return_demeaned"] = (
        nm.groupby("quarter_cohort")["forward_return"].transform(lambda x: x - x.mean())
    )
    corr_demeaned = nm["net_margin"].corr(nm["forward_return_demeaned"])
    print(f"  Pearson correlation, raw net_margin vs. RAW forward_return:      {corr_raw:+.4f}")
    print(f"  Pearson correlation, raw net_margin vs. DEMEANED forward_return: {corr_demeaned:+.4f}")


if __name__ == "__main__":
    main()
