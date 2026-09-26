"""
Runs the full ETL pipeline end-to-end, in order:

  1. create_schema.py     — create/verify DuckDB tables
  2. populate_dim_date.py — populate the date dimension
  3. extract/prices.py    — yfinance daily OHLCV
  4. extract/filings.py   — SEC EDGAR filings + fundamentals
  5. extract/macro.py     — FRED macro indicators
  6. transform/build_views.py — build the joined analytical views

Each step is a separate script (not a shared library) so any of them can
still be run individually during development — this wrapper just chains
them in the right order for a clean end-to-end run.

If a step fails, the pipeline stops there (later steps depend on earlier
ones having run) and prints which step failed and its exit code, rather
than continuing on to steps that would just fail anyway on missing data.

Usage:
    python run_pipeline.py --start 2021-01-01 --end 2026-12-31
"""

import argparse
import subprocess
import sys
import time

ROOT = None  # set in main()


def run_step(description: str, args: list) -> bool:
    print(f"\n{'=' * 60}")
    print(f"STEP: {description}")
    print(f"{'=' * 60}")
    start = time.time()
    result = subprocess.run([sys.executable] + args)
    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"\n FAILED: {description} (exit code {result.returncode}, {elapsed:.1f}s)")
        print("Stopping pipeline — later steps depend on this one.")
        return False
    print(f"\n OK: {description} ({elapsed:.1f}s)")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01", help="pipeline-wide start date")
    parser.add_argument("--end", default="2026-12-31", help="pipeline-wide end date")
    parser.add_argument(
        "--skip", nargs="*", default=[],
        choices=["schema", "dates", "prices", "filings", "macro", "views"],
        help="step names to skip, e.g. --skip schema dates",
    )
    args = parser.parse_args()

    pipeline_start = time.time()

    steps = [
        ("schema", "Create/verify DuckDB schema", ["load/create_schema.py"]),
        ("dates", "Populate date dimension",
         ["load/populate_dim_date.py", "--start", args.start, "--end", args.end]),
        ("prices", "Extract prices (yfinance)",
         ["extract/prices.py", "--start", args.start, "--end", args.end]),
        ("filings", "Extract SEC filings + fundamentals",
         ["extract/filings.py", "--start", args.start]),
        ("macro", "Extract FRED macro indicators",
         ["extract/macro.py", "--start", args.start, "--end", args.end]),
        ("views", "Build joined analytical views",
         ["transform/build_views.py"]),
    ]

    for key, description, step_args in steps:
        if key in args.skip:
            print(f"\nSkipping: {description} (--skip {key})")
            continue
        if not run_step(description, step_args):
            sys.exit(1)

    total_elapsed = time.time() - pipeline_start
    print(f"\n{'=' * 60}")
    print(f"Pipeline complete in {total_elapsed / 60:.1f} minutes.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
