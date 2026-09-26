"""
Builds SQL views that join all three fact tables into an analysis-ready
shape. These are DuckDB VIEWs (not materialized tables).

Views created:

  macro_daily
      One row per calendar day in dim_date, with each macro series
      forward-filled to that day via DuckDB's ASOF JOIN. Includes both
      fed_funds_rate (FEDFUNDS, monthly average — kept for reference) and
      fed_target_rate (DFEDTARU, daily) — analyses should prefer
      fed_target_rate for anything event-based, since FEDFUNDS's monthly
      averaging creates spurious month-to-month "changes" even when no
      actual Fed decision occurred that month.

  fundamentals_dated
      fact_fundamentals with an added effective_date (real filing date,
      or a +45 day estimate when the filing link was dropped — see
      extract/filings.py), flagged via effective_date_is_estimated.

  analytics_daily
      One row per company per trading day. daily_return is computed from
      COALESCE(adj_close, close) — NOT raw close — so it reflects total
      return (price change + dividends), not price-only return. Raw
      close/adj_close are both still exposed as separate columns for
      anyone who specifically wants price-only figures. Fundamentals are
      joined via ASOF JOIN on effective_date <= trading date to avoid
      look-ahead bias.

Usage:
    python transform/build_views.py
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")

DDL = """
-- =========================================================
-- macro_daily: forward-fill each macro series to every calendar day
-- =========================================================
CREATE OR REPLACE VIEW macro_daily AS
SELECT
    d.date_id,
    fed.value    AS fed_funds_rate,
    fedtarget.value AS fed_target_rate,
    dgs10.value  AS treasury_10y,
    cpi.value    AS cpi,
    unrate.value AS unemployment_rate,
    gdp.value    AS gdp
FROM dim_date d
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'FEDFUNDS'
) fed ON fed.date_id <= d.date_id
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'DFEDTARU'
) fedtarget ON fedtarget.date_id <= d.date_id
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'DGS10'
) dgs10 ON dgs10.date_id <= d.date_id
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'CPIAUCSL'
) cpi ON cpi.date_id <= d.date_id
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'UNRATE'
) unrate ON unrate.date_id <= d.date_id
ASOF LEFT JOIN (
    SELECT date_id, value FROM fact_macro_observation WHERE series_id = 'GDP'
) gdp ON gdp.date_id <= d.date_id;

-- =========================================================
-- fundamentals_dated: attach the real-world "public as of" date
-- =========================================================
CREATE OR REPLACE VIEW fundamentals_dated AS
SELECT
    f.company_id,
    f.fiscal_period_end,
    f.revenue,
    f.net_income,
    f.total_debt,
    f.total_equity,
    f.eps,
    f.source_filing_id,
    COALESCE(fl.filing_date, f.fiscal_period_end + INTERVAL 45 DAY) AS effective_date,
    (fl.filing_date IS NULL) AS effective_date_is_estimated
FROM fact_fundamentals f
LEFT JOIN fact_filing fl ON f.source_filing_id = fl.filing_id;

-- =========================================================
-- analytics_daily: the main joined table for analysis
-- =========================================================
CREATE OR REPLACE VIEW analytics_daily AS
SELECT
    c.company_id,
    c.ticker,
    c.company_name,
    c.sector,
    c.industry,
    p.date_id,
    dd.year,
    dd.quarter,
    dd.fiscal_quarter_label,
    p.open,
    p.high,
    p.low,
    p.close,
    p.adj_close,
    p.volume,
    (
        COALESCE(p.adj_close, p.close)
        / LAG(COALESCE(p.adj_close, p.close)) OVER (PARTITION BY p.company_id ORDER BY p.date_id)
        - 1
    ) AS daily_return,
    m.fed_funds_rate,
    m.fed_target_rate,
    m.treasury_10y,
    m.cpi,
    m.unemployment_rate,
    m.gdp,
    fd.revenue,
    fd.net_income,
    fd.total_debt,
    fd.total_equity,
    fd.eps,
    fd.fiscal_period_end   AS fundamentals_period_end,
    fd.effective_date      AS fundamentals_effective_date,
    fd.effective_date_is_estimated
FROM fact_daily_price p
JOIN dim_company c ON p.company_id = c.company_id
JOIN dim_date dd ON p.date_id = dd.date_id
LEFT JOIN macro_daily m ON p.date_id = m.date_id
ASOF LEFT JOIN fundamentals_dated fd
    ON p.company_id = fd.company_id
    AND fd.effective_date <= p.date_id;
"""


def main():
    con = duckdb.connect(DB_PATH)
    con.execute(DDL)

    row = con.execute("SELECT COUNT(*) FROM analytics_daily").fetchone()
    sample = con.execute("""
        SELECT ticker, date_id, close, adj_close, daily_return, fed_target_rate
        FROM analytics_daily
        WHERE ticker = 'AAPL'
        ORDER BY date_id DESC
        LIMIT 3
    """).fetchall()

    con.close()

    print(f"Views created. analytics_daily has {row[0]} rows.")
    print("\nSample (AAPL, most recent 3 trading days):")
    for r in sample:
        print(f"  {r}")


if __name__ == "__main__":
    main()
