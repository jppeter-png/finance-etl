"""
Extracts daily OHLCV price data from yfinance and loads it into DuckDB.

Populates:
  - dim_company (upsert, one row per ticker)
  - fact_daily_price (5 years of daily bars per ticker)

yfinance wraps unofficial Yahoo endpoints, so it can throttle or return
partial/empty data under load. This script retries with backoff and
skips (rather than crashes on) a ticker that fails after retries, so one
bad ticker doesn't take down the whole run.

Usage:
    python extract/prices.py --start 2021-01-01 --end 2026-12-31
"""

import argparse
import os
import sys
import time

import duckdb
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.tickers import TICKERS

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def upsert_companies(con):
    """Ensure every ticker in config/tickers.py has a row in dim_company.
    Returns a dict of ticker -> company_id."""
    existing = con.execute("SELECT ticker, company_id, sector FROM dim_company").fetchall()
    existing_map = {t: cid for t, cid, _sector in existing}
    existing_sector = {t: sector for t, _cid, sector in existing}

    next_id_row = con.execute("SELECT COALESCE(MAX(company_id), 0) FROM dim_company").fetchone()
    next_id = next_id_row[0] + 1

    backfilled = 0
    for ticker, name, sector, industry in TICKERS:
        if ticker in existing_map:
            # backfill rows that exist but are missing sector/name data
            # (e.g. from an earlier bug, or a manual placeholder entry)
            current_sector = existing_sector.get(ticker)
            if not current_sector and sector:
                con.execute(
                    "UPDATE dim_company SET company_name = ?, sector = ?, industry = ? WHERE ticker = ?",
                    [name, sector, industry, ticker],
                )
                backfilled += 1
            continue
        con.execute(
            """
            INSERT INTO dim_company (company_id, ticker, company_name, sector, industry)
            VALUES (?, ?, ?, ?, ?)
            """,
            [next_id, ticker, name, sector, industry],
        )
        existing_map[ticker] = next_id
        next_id += 1

    if backfilled:
        print(f"  Backfilled sector/name for {backfilled} existing companies.")

    return existing_map


def fetch_prices_with_retry(ticker: str, start: str, end: str) -> pd.DataFrame:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
            if df is None or df.empty:
                raise ValueError("empty dataframe returned")
            return df
        except Exception as e:
            print(f"  [{ticker}] attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    print(f"  [{ticker}] giving up after {MAX_RETRIES} attempts — skipping this ticker")
    return pd.DataFrame()


def load_prices(con, company_id: int, ticker: str, df: pd.DataFrame):
    if df.empty:
        return 0

    rows = []
    for idx, row in df.iterrows():
        date_id = idx.date()
        rows.append((
            company_id,
            date_id,
            float(row["Open"]) if pd.notna(row["Open"]) else None,
            float(row["High"]) if pd.notna(row["High"]) else None,
            float(row["Low"]) if pd.notna(row["Low"]) else None,
            float(row["Close"]) if pd.notna(row["Close"]) else None,
            float(row["Adj Close"]) if "Adj Close" in row and pd.notna(row["Adj Close"]) else None,
            int(row["Volume"]) if pd.notna(row["Volume"]) else None,
        ))

    con.executemany(
        """
        INSERT OR REPLACE INTO fact_daily_price
            (company_id, date_id, open, high, low, close, adj_close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-12-31")
    args = parser.parse_args()

    con = duckdb.connect(DB_PATH)
    company_map = upsert_companies(con)

    total_rows = 0
    failed_tickers = []

    for ticker, name, *_ in TICKERS:
        print(f"Fetching {ticker} ({name})...")
        df = fetch_prices_with_retry(ticker, args.start, args.end)
        if df.empty:
            failed_tickers.append(ticker)
            continue
        n = load_prices(con, company_map[ticker], ticker, df)
        total_rows += n
        print(f"  loaded {n} rows for {ticker}")

    con.close()

    print(f"\nDone. Loaded {total_rows} price rows across {len(TICKERS) - len(failed_tickers)} tickers.")
    if failed_tickers:
        print(f"Failed/skipped tickers: {failed_tickers} — re-run this script to retry just these.")


if __name__ == "__main__":
    main()
