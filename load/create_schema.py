"""
Creates the DuckDB schema for the finance ETL project.

Run this once to (re)initialize the database file. Safe to re-run —
uses CREATE TABLE IF NOT EXISTS, so it won't wipe existing data.

Usage:
    python load/create_schema.py
"""

import duckdb
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")

DDL = """
-- =========================================================
-- DIMENSION TABLES
-- =========================================================

CREATE TABLE IF NOT EXISTS dim_company (
    company_id   INTEGER PRIMARY KEY,
    ticker       VARCHAR UNIQUE NOT NULL,
    company_name VARCHAR,
    sector       VARCHAR,
    industry     VARCHAR,
    cik          VARCHAR,          -- SEC's company identifier (10-digit, zero-padded)
    added_date   DATE DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_id             DATE PRIMARY KEY,
    year                INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    day                 INTEGER NOT NULL,
    day_of_week         INTEGER NOT NULL,   -- 0=Monday .. 6=Sunday
    is_trading_day      BOOLEAN NOT NULL,
    fiscal_quarter_label VARCHAR NOT NULL   -- e.g. '2024Q1'
);

CREATE TABLE IF NOT EXISTS dim_macro_series (
    series_id   VARCHAR PRIMARY KEY,   -- FRED series code, e.g. 'FEDFUNDS', 'CPIAUCSL'
    series_name VARCHAR,
    frequency   VARCHAR,               -- 'daily' | 'monthly' | 'quarterly'
    units       VARCHAR
);

-- =========================================================
-- FACT TABLES
-- =========================================================

CREATE TABLE IF NOT EXISTS fact_daily_price (
    company_id      INTEGER NOT NULL REFERENCES dim_company(company_id),
    date_id         DATE    NOT NULL REFERENCES dim_date(date_id),
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    adj_close       DOUBLE,
    volume          BIGINT,
    source_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (company_id, date_id)
);

CREATE TABLE IF NOT EXISTS fact_filing (
    filing_id         VARCHAR PRIMARY KEY,   -- use SEC accession_number as the ID
    company_id        INTEGER NOT NULL REFERENCES dim_company(company_id),
    filing_date       DATE    NOT NULL REFERENCES dim_date(date_id),
    form_type         VARCHAR,               -- '10-K', '10-Q', '8-K'
    fiscal_period_end DATE,
    accession_number  VARCHAR,
    source_url        VARCHAR,
    source_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_fundamentals (
    company_id        INTEGER NOT NULL REFERENCES dim_company(company_id),
    fiscal_period_end DATE    NOT NULL,
    revenue           DOUBLE,
    net_income        DOUBLE,
    total_debt        DOUBLE,
    total_equity      DOUBLE,
    eps               DOUBLE,
    source_filing_id  VARCHAR REFERENCES fact_filing(filing_id),
    source_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (company_id, fiscal_period_end)
);

CREATE TABLE IF NOT EXISTS fact_macro_observation (
    series_id       VARCHAR NOT NULL REFERENCES dim_macro_series(series_id),
    date_id         DATE    NOT NULL REFERENCES dim_date(date_id),
    value           DOUBLE,
    source_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (series_id, date_id)
);

-- =========================================================
-- INDEXES (DuckDB creates PK indexes automatically;
-- these help the common analytical join/filter patterns)
-- =========================================================

CREATE INDEX IF NOT EXISTS idx_price_date       ON fact_daily_price(date_id);
CREATE INDEX IF NOT EXISTS idx_filing_company    ON fact_filing(company_id);
CREATE INDEX IF NOT EXISTS idx_macro_series_date ON fact_macro_observation(series_id, date_id);
"""


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH)
    con.execute(DDL)
    con.close()
    print(f"Schema created/verified at: {DB_PATH}")


if __name__ == "__main__":
    main()
