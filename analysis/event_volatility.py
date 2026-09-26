"""
Analysis 2: Volatility clustering around earnings filings vs. macro
(Fed rate change) events.

Question: does volatility spike more around company-specific events
(earnings filings) or macro events (Fed rate decisions)?

Method (a standard "event study" design):
  1. Define earnings events as 10-Q/10-K filing dates per company.
     CAVEAT: this is a proxy. The actual earnings announcement/call
     typically happens 1-4 weeks BEFORE the formal filing — so this
     measures volatility around the filing, not the announcement.
  2. Define macro events as days where the Fed target rate (DFEDTARU,
     daily upper bound) changed. This is the rate's effective date,
     which is typically the trading day after the FOMC announcement, so
     the +/-1 day window covers the announcement day too.
  3. For each event, take a +/-1 TRADING DAY window (not calendar day —
     using row-number offsets per ticker avoids weekend contamination
     that a naive date+/-1 would introduce).
  4. Compare mean absolute daily return inside these windows to each
     ticker's baseline (all non-event trading days). A ratio > 1 means
     volatility clusters around that event type.

Usage:
    python analysis/event_volatility.py
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")


def run_event_study(con, event_query: str, label: str, window: int = 1):
    """event_query must return (company_id, event_date) pairs."""

    sql = f"""
    WITH trading AS (
        SELECT company_id, date_id, daily_return,
               ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY date_id) AS rn
        FROM analytics_daily
        WHERE daily_return IS NOT NULL
    ),
    events AS (
        {event_query}
    ),
    event_anchor AS (
        -- map each event to the nearest trading day on/before it, per company
        SELECT e.company_id, e.event_date, t.rn AS anchor_rn
        FROM events e
        ASOF JOIN trading t
            ON e.company_id = t.company_id AND t.date_id <= e.event_date
    ),
    event_window AS (
        -- the anchor trading day +/- `window` trading days
        SELECT DISTINCT t.company_id, t.date_id, t.daily_return
        FROM event_anchor ea
        JOIN trading t
            ON t.company_id = ea.company_id
            AND t.rn BETWEEN ea.anchor_rn - {window} AND ea.anchor_rn + {window}
    ),
    event_stats AS (
        SELECT AVG(ABS(daily_return)) AS avg_abs_return, COUNT(*) AS n
        FROM event_window
    ),
    baseline_stats AS (
        SELECT AVG(ABS(t.daily_return)) AS avg_abs_return, COUNT(*) AS n
        FROM trading t
        WHERE NOT EXISTS (
            SELECT 1 FROM event_window ew
            WHERE ew.company_id = t.company_id AND ew.date_id = t.date_id
        )
    )
    SELECT
        (SELECT avg_abs_return FROM event_stats)    AS event_avg_abs_return,
        (SELECT n FROM event_stats)                  AS event_n_days,
        (SELECT avg_abs_return FROM baseline_stats)  AS baseline_avg_abs_return,
        (SELECT n FROM baseline_stats)                AS baseline_n_days
    """

    row = con.execute(sql).fetchone()
    event_avg, event_n, baseline_avg, baseline_n = row
    ratio = event_avg / baseline_avg if baseline_avg else None

    print(f"\n{label}")
    print(f"  Event-window avg |daily return|:    {event_avg:.5f}  (n={event_n:,} company-days)")
    print(f"  Baseline avg |daily return|:         {baseline_avg:.5f}  (n={baseline_n:,} company-days)")
    print(f"  Ratio (event / baseline):            {ratio:.2f}x")
    return ratio


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=1,
                         help="trading days before/after each event to include (default 1)")
    args = parser.parse_args()

    con = duckdb.connect(DB_PATH, read_only=True)

    earnings_query = """
        SELECT DISTINCT company_id, filing_date AS event_date
        FROM fact_filing
        WHERE form_type IN ('10-Q', '10-K')
    """

    macro_query = """
        WITH fedtarget AS (
            SELECT date_id, value,
                   LAG(value) OVER (ORDER BY date_id) AS prev_value
            FROM fact_macro_observation
            WHERE series_id = 'DFEDTARU'
        ),
        rate_change_dates AS (
            -- DFEDTARU is already daily, so every change IS the real
            -- effective date of an actual Fed decision — unlike FEDFUNDS
            -- (a monthly average), this needs no anchoring approximation
            -- and no noise-filtering threshold.
            SELECT date_id AS event_date
            FROM fedtarget
            WHERE prev_value IS NOT NULL AND value != prev_value
        )
        SELECT c.company_id, r.event_date
        FROM rate_change_dates r
        CROSS JOIN dim_company c
    """

    print("=" * 70)
    print(f"EVENT STUDY: volatility clustering (event window vs. baseline), +/-{args.window} trading days")
    print("=" * 70)

    earnings_ratio = run_event_study(
        con, earnings_query,
        f"EARNINGS FILINGS (10-Q/10-K filing date +/-{args.window} trading days)",
        window=args.window,
    )
    macro_ratio = run_event_study(
        con, macro_query,
        f"FED RATE CHANGES (real DFEDTARU decision date +/-{args.window} trading days, all tickers)",
        window=args.window,
    )

    con.close()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Earnings filing volatility ratio: {earnings_ratio:.2f}x baseline")
    print(f"Macro rate-change volatility ratio: {macro_ratio:.2f}x baseline")
    if earnings_ratio and macro_ratio:
        bigger = "earnings filings" if earnings_ratio > macro_ratio else "macro rate changes"
        print(f"-> Volatility clusters more strongly around: {bigger}")

    print("""
Caveats (state these explicitly in any write-up):
  - Earnings events use FILING dates, not announcement/call dates. The
    actual earnings release typically precedes the formal 10-Q/10-K
    filing by 1-4 weeks, so this likely UNDERSTATES true earnings-day
    volatility clustering — the real spike may fall outside this window.
  - Macro events use DFEDTARU (the actual daily target rate), so each
    event date is the real effective date of an actual Fed decision, not
    an approximation. This replaced an earlier, flawed version that used
    FEDFUNDS's monthly average anchored to the 1st of the month — most
    "changes" in that series were sub-0.2-point drift from averaging,
    not real decisions, which made a near-1.0x null result close to
    guaranteed regardless of the true effect.
  - +/-1 trading day is a narrow window; a wider window (+/-3, +/-5)
    would be a reasonable robustness check.
""")


if __name__ == "__main__":
    main()
