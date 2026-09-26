"""
Populates dim_date for a given date range. Run this after create_schema.py
and before loading any fact tables, since they all have FK references to dim_date.

Usage:
    python load/populate_dim_date.py --start 2021-01-01 --end 2026-12-31
"""

import argparse
import os
from datetime import date, timedelta

import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")


def daterange(start: date, end: date):
    days = (end - start).days
    for i in range(days + 1):
        yield start + timedelta(days=i)


def build_rows(start: date, end: date):
    rows = []
    for d in daterange(start, end):
        quarter = (d.month - 1) // 3 + 1
        rows.append((
            d,
            d.year,
            quarter,
            d.month,
            d.day,
            d.weekday(),                 # 0=Monday .. 6=Sunday
            d.weekday() < 5,              # naive trading-day flag; holidays not excluded
            f"{d.year}Q{quarter}",
        ))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-12-31")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    rows = build_rows(start, end)

    con = duckdb.connect(DB_PATH)
    con.executemany(
        """
        INSERT INTO dim_date
            (date_id, year, quarter, month, day, day_of_week, is_trading_day, fiscal_quarter_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (date_id) DO NOTHING
        """,
        rows,
    )
    count = con.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]
    con.close()
    print(f"Inserted date range {start} to {end}. dim_date now has {count} rows.")


if __name__ == "__main__":
    main()
