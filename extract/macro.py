"""
Extracts macroeconomic indicator data from FRED (Federal Reserve Economic Data)
and loads it into DuckDB.

Populates:
  - dim_macro_series  (metadata about each series being tracked)
  - fact_macro_observation

Series tracked:
  - FEDFUNDS  Effective Federal Funds Rate        (monthly average)
  - DFEDTARU  Federal Funds Target Rate - Upper Limit (daily)
  - DGS10     10-Year Treasury Constant Maturity   (daily)
  - CPIAUCSL  Consumer Price Index (all urban)     (monthly)
  - UNRATE    Unemployment Rate                    (monthly)
  - GDP       Gross Domestic Product               (quarterly)

DFEDTARU matters specifically for event-study analysis: FEDFUNDS is a
monthly AVERAGE, so nearly every month shows some tiny change even when
no FOMC decision occurred that month — that's noise, not a rate-change
event. DFEDTARU is the actual daily target rate; it only changes on the
real effective date of an actual Fed decision, giving genuine event dates
directly with no averaging artifacts and no need to anchor to an
arbitrary "1st of the month" date.

Usage:
    python extract/macro.py --start 2021-01-01 --end 2026-12-31
"""

import argparse
import os
import sys

import duckdb
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
API_KEY = os.getenv("FRED_API_KEY")

if not API_KEY:
    print("ERROR: FRED_API_KEY not set in .env")
    sys.exit(1)

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES = [
    # series_id, series_name, frequency, units
    ("FEDFUNDS", "Effective Federal Funds Rate", "monthly", "Percent"),
    ("DFEDTARU", "Federal Funds Target Rate - Upper Limit", "daily", "Percent"),
    ("DGS10", "10-Year Treasury Constant Maturity Rate", "daily", "Percent"),
    ("CPIAUCSL", "Consumer Price Index for All Urban Consumers", "monthly", "Index 1982-1984=100"),
    ("UNRATE", "Unemployment Rate", "monthly", "Percent"),
    ("GDP", "Gross Domestic Product", "quarterly", "Billions of Dollars"),
]


def upsert_series_metadata(con):
    con.executemany(
        """
        INSERT OR REPLACE INTO dim_macro_series (series_id, series_name, frequency, units)
        VALUES (?, ?, ?, ?)
        """,
        SERIES,
    )


def fetch_series(series_id: str, start: str, end: str) -> list:
    resp = requests.get(
        BASE_URL,
        params={
            "series_id": series_id,
            "api_key": API_KEY,
            "file_type": "json",
            "observation_start": start,
            "observation_end": end,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("observations", [])


def load_observations(con, series_id: str, observations: list) -> int:
    rows = []
    for obs in observations:
        if obs.get("value") in (None, ".", ""):
            continue
        rows.append((series_id, obs["date"], float(obs["value"])))

    if not rows:
        return 0

    con.executemany(
        """
        INSERT OR REPLACE INTO fact_macro_observation (series_id, date_id, value)
        VALUES (?, ?, ?)
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
    upsert_series_metadata(con)

    total = 0
    for series_id, name, *_ in SERIES:
        print(f"Fetching {series_id} ({name})...")
        observations = fetch_series(series_id, args.start, args.end)
        n = load_observations(con, series_id, observations)
        total += n
        print(f"  loaded {n} observations")

    con.close()
    print(f"\nDone. Loaded {total} macro observations across {len(SERIES)} series.")


if __name__ == "__main__":
    main()
