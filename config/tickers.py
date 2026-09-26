"""
Central list of tickers this pipeline tracks.

Fetches the current S&P 500 constituent list (ticker, company name, sector)
from the public-domain datasets/s-and-p-500-companies GitHub repo, which
sources it from Wikipedia and updates it periodically. Falls back to a
local cache if the fetch fails (e.g. no network), so the pipeline can
still run offline once it's been fetched at least once.

To track a custom/smaller list instead (e.g. for fast local testing),
set MANUAL_TICKERS below to a non-empty list — it takes priority over the
S&P 500 fetch.
"""

import csv
import io
import os

import requests

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sp500_constituents_cache.csv")
SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# Set this to a non-empty list to override the S&P 500 fetch with a fixed,
# small set of tickers — useful for fast local development/testing.
MANUAL_TICKERS = []


def _fetch_and_cache() -> str:
    resp = requests.get(SOURCE_URL, timeout=30)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        f.write(resp.text)
    return resp.text


def _load_from_cache() -> str:
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _get_sp500_tickers() -> list:
    try:
        csv_text = _fetch_and_cache()
        source = "live fetch"
    except requests.exceptions.RequestException as e:
        print(f"  WARNING: could not fetch S&P 500 list ({e}); trying local cache...")
        try:
            csv_text = _load_from_cache()
            source = "local cache"
        except FileNotFoundError:
            raise RuntimeError(
                "No network access and no local cache found at "
                f"{CACHE_PATH}. Run once with network access to populate the cache."
            )

    reader = csv.DictReader(io.StringIO(csv_text))
    tickers = []
    for row in reader:
        symbol = row.get("Symbol", "").strip()
        name = row.get("Security", "").strip()
        sector = row.get("GICS Sector", "").strip()
        industry = row.get("GICS Sub-Industry", "").strip()
        if not symbol:
            continue
        # yfinance/SEC both use '-' where this list sometimes uses '.'
        # for share classes (e.g. BRK.B -> BRK-B)
        symbol = symbol.replace(".", "-")
        tickers.append((symbol, name, sector, industry))

    print(f"  Loaded {len(tickers)} tickers from {source}.")
    return tickers


TICKERS = MANUAL_TICKERS or _get_sp500_tickers()

