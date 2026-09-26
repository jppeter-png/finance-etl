"""
Analysis 1: Rate sensitivity by sector.

Question: does sector performance actually correlate with Fed rate changes
the way conventional wisdom predicts (growth/tech hurt more by hikes,
financials helped, defensives insulated)?

Method:
  1. Aggregate analytics_daily to one row per ticker per month
     (month-end adjusted close, month-end Fed target rate from DFEDTARU).
  2. Compute monthly return per ticker and monthly change (delta) in the
     Fed target rate. Most months have a delta of zero; the correlation
     is driven by the months in which the Fed actually moved.
  3. Correlate each ticker's monthly return against the monthly rate
     delta — a negative correlation means the stock tends to fall when
     rates rise (conventional "rate-sensitive growth" behavior); positive
     means it tends to rise with rates (conventional "benefits from
     higher rates" behavior, e.g. financials).
  4. Average ticker-level correlations up to the sector level.

Note: sector is read directly from DuckDB, which returns a missing text
value as an empty string (''), not NaN — unlike a CSV round-trip, where
pandas' reader converts blank fields to NaN automatically. A plain
dropna(subset=["sector"]) or groupby("sector") would silently include an
unlabeled '' group (e.g. tickers removed from the S&P 500 since being
loaded, whose sector was never backfilled) rather than excluding it. Both
NaN and blank/whitespace-only sector values are filtered out explicitly
below.

Caveats printed with the output:
  - ~5 years of monthly data, with only a minority of months containing
    an actual rate change — treat this as directional, not statistically
    robust.
  - Correlation, not causation — rates move alongside many other things.

Usage:
    python analysis/rate_sensitivity.py
"""

import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")


def main():
    con = duckdb.connect(DB_PATH, read_only=True)

    monthly = con.execute("""
        SELECT
            ticker,
            sector,
            date_trunc('month', date_id) AS month,
            last(COALESCE(adj_close, close) ORDER BY date_id) AS month_end_close,
            last(fed_target_rate ORDER BY date_id) AS month_end_rate
        FROM analytics_daily
        GROUP BY ticker, sector, date_trunc('month', date_id)
        ORDER BY ticker, month
    """).df()

    con.close()

    monthly["monthly_return"] = monthly.groupby("ticker")["month_end_close"].pct_change()
    monthly["rate_delta"] = monthly.groupby("ticker")["month_end_rate"].diff()

    clean = monthly.dropna(subset=["monthly_return", "rate_delta"])

    # per-ticker correlation between monthly return and rate change
    ticker_corr = (
        clean.groupby(["ticker", "sector"])
        .apply(lambda g: g["monthly_return"].corr(g["rate_delta"]), include_groups=False)
        .reset_index(name="correlation_with_rate_delta")
    )

    # exclude both NaN and blank/whitespace-only sectors (see note above) —
    # otherwise tickers with a missing sector silently form their own
    # unlabeled group instead of being dropped from the sector rollup
    valid_sector = ticker_corr["sector"].notna() & (ticker_corr["sector"].str.strip() != "")
    excluded = ticker_corr[~valid_sector]
    if len(excluded) > 0:
        print(f"Note: excluding {len(excluded)} ticker(s) with missing sector from "
              f"sector rollup: {sorted(excluded['ticker'].tolist())}\n")

    sector_summary = (
        ticker_corr[valid_sector]
        .groupby("sector")["correlation_with_rate_delta"]
        .agg(["mean", "count"])
        .rename(columns={"mean": "avg_correlation", "count": "n_tickers"})
        .sort_values("avg_correlation")
        .reset_index()
    )

    print("Per-ticker correlation (monthly return vs. monthly Fed target rate change):\n")
    print(ticker_corr.sort_values("correlation_with_rate_delta").to_string(index=False))

    print("\n\nSector-level average:\n")
    print(sector_summary.to_string(index=False))

    print("""
Reading this:
  - Negative correlation = stock tends to fall when rates rise (the
    conventional expectation for growth/tech).
  - Positive correlation = stock tends to rise when rates rise (the
    conventional expectation for financials).

Notes:
  - Rate source is fed_target_rate (DFEDTARU, the actual daily target
    rate), not FEDFUNDS's monthly average — avoids treating routine
    month-to-month averaging drift as a real rate move.
  - Returns use adj_close (dividend- and split-adjusted), not raw close.
  - This is correlation, not a controlled experiment — many other things
    moved alongside rates over this period (inflation, an AI-driven tech
    rally, etc.) and aren't isolated out here.
""")

    out_path = os.path.join(os.path.dirname(__file__), "rate_sensitivity_results.csv")
    ticker_corr.to_csv(out_path, index=False)
    print(f"Per-ticker results saved to {out_path}")


if __name__ == "__main__":
    main()
