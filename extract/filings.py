"""
Extracts SEC EDGAR data for the tracked tickers and loads it into DuckDB.

Populates:
  - dim_company.cik   (looked up from SEC's ticker->CIK mapping file)
  - fact_filing        (10-K/10-Q/8-K metadata, from the submissions API)
  - fact_fundamentals  (revenue, net income, equity, EPS, etc., from the
                         XBRL "company facts" API — pre-parsed, structured data)

SEC requires a descriptive User-Agent on every request (name + contact email),
or it will block you. Set SEC_USER_AGENT in your .env file, e.g.:
    SEC_USER_AGENT="Jane Doe jane@example.com"

Notes on the messiness this script has to handle (this is the real ETL work):
  - Companies don't all use the same XBRL tag for the same concept. Revenue
    might be tagged "Revenues" for one company and
    "RevenueFromContractWithCustomerExcludingAssessedTax" for another. We try
    a list of candidate tags per concept, in priority order.
  - "Total debt" has no single clean XBRL tag across all companies; this
    script uses total liabilities as a documented proxy (see TOTAL_DEBT_TAGS).
  - The same fiscal period appears multiple times in the raw data: every
    10-K/10-Q repeats prior-period figures as comparatives, and some
    periods are later restated. We keep the FIRST filed value — what the
    market actually saw when the number became public — so each row's
    effective date is its original release, not a comparative a year later.

Usage:
    python extract/filings.py --start 2021-01-01
"""

import argparse
import os
import sys
import time

import duckdb
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
USER_AGENT = os.getenv("SEC_USER_AGENT")

HEADERS = {"User-Agent": USER_AGENT or ""}
SLEEP_BETWEEN_REQUESTS = 0.3  # be polite; SEC's fair-access policy asks for restraint

FORM_TYPES_TRACKED = ("10-K", "10-Q", "8-K")

# Candidate XBRL tags per concept, in priority order — first one found wins.
REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
# No single clean "total debt" tag exists across companies; total liabilities
# is used here as a documented proxy rather than true interest-bearing debt.
TOTAL_DEBT_TAGS = ["Liabilities"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]


def get_cik_map() -> dict:
    """SEC publishes a single JSON file mapping ticker -> CIK for all companies."""
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    # file is keyed by row number, each value has 'ticker' and 'cik_str'
    return {row["ticker"]: str(row["cik_str"]).zfill(10) for row in data.values()}


def update_company_ciks(con, cik_map: dict, tickers: list) -> dict:
    """Set dim_company.cik for each tracked ticker. Returns ticker -> company_id."""
    rows = con.execute("SELECT company_id, ticker FROM dim_company").fetchall()
    ticker_to_id = {ticker: cid for cid, ticker in rows}

    for ticker, *_ in tickers:
        cik = cik_map.get(ticker)
        if not cik:
            print(f"  WARNING: no CIK found for {ticker} — skipping SEC data for this ticker")
            continue
        con.execute("UPDATE dim_company SET cik = ? WHERE ticker = ?", [cik, ticker])

    return ticker_to_id


def fetch_submissions(cik: str) -> dict:
    resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json", headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def load_filings(con, company_id: int, submissions: dict, start_date: str) -> int:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    rows = []
    for form, filed, accn, doc in zip(forms, dates, accns, docs):
        if form not in FORM_TYPES_TRACKED:
            continue
        if filed < start_date:
            continue
        source_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(company_id)}/{accn.replace('-', '')}/{doc}"
        )
        rows.append((accn, company_id, filed, form, None, accn, source_url))

    if not rows:
        return 0

    con.executemany(
        """
        INSERT INTO fact_filing
            (filing_id, company_id, filing_date, form_type, fiscal_period_end, accession_number, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (filing_id) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def fetch_company_facts(cik: str) -> dict:
    resp = requests.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def _is_clean_period(entry: dict, tolerance_days: int = 15) -> bool:
    """For duration-type XBRL facts (revenue, net income, EPS), a single
    filing can tag both the standalone quarter AND the year-to-date
    cumulative figure under the same concept, with the same 'end' date but
    different 'start' dates (e.g. a Q3 10-Q reporting both the 3-month
    quarter and the 9-month YTD sum). Keying only on 'end' date risks
    silently picking up the inflated YTD figure instead of the single
    period. This keeps only windows that are roughly one quarter (~91
    days) or one full year (~365 days), rejecting 6-month/9-month YTD
    windows. Instant-type facts (no 'start', e.g. balance-sheet items like
    equity or total debt) always pass through unfiltered — there's no
    duration ambiguity for a point-in-time value.
    """
    start = entry.get("start")
    end = entry.get("end")
    if not start:
        return True  # instant concept — nothing to filter
    try:
        from datetime import date
        delta_days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return False
    is_quarter = abs(delta_days - 91) <= tolerance_days
    is_year = abs(delta_days - 365) <= tolerance_days
    return is_quarter or is_year


def _extract_concept(facts: dict, candidate_tags: list, start_date: str) -> dict:
    """Returns {fiscal_period_end: (value, accession_number, filed_date)}.

    Merges data across ALL candidate tags rather than stopping at the first
    tag with any data — companies often switch which XBRL tag they report a
    concept under partway through their history (e.g. many switched revenue
    tags around 2018-2019 when ASC 606 took effect), so a single company can
    have some periods only under one tag and other periods only under
    another. Stopping at the first match silently drops the periods that
    only exist under a later candidate.

    Tags are tried in priority order: if the same period appears under more
    than one tag, the higher-priority tag's value wins. Within a single tag,
    if the same period appears more than once, the FIRST-filed value is
    kept (point-in-time: the original release, not a later comparative or
    restatement). An earlier version kept the most recently filed value,
    which linked most periods to the following year's filing (median lag
    ~400 days after period end) and made every factor ~1 year stale.

    Periods first filed before start_date are dropped rather than being
    attributed to a later comparative filing inside the window.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    merged = {}
    for tag in candidate_tags:
        units = gaap.get(tag, {}).get("units", {})
        # per-share concepts (EPS) are reported in "USD/shares", not "USD"
        entries = units.get("USD", []) + units.get("USD/shares", [])
        tag_periods = {}
        for e in entries:
            end = e.get("end")
            filed = e.get("filed")
            form = e.get("form")
            if not end or not filed or form not in FORM_TYPES_TRACKED:
                continue
            if not _is_clean_period(e):
                continue
            if end not in tag_periods or filed < tag_periods[end][2]:
                tag_periods[end] = (e.get("val"), e.get("accn"), filed)
        for end, value in tag_periods.items():
            if value[2] < start_date:
                continue  # first released before the window — no in-window release date
            merged.setdefault(end, value)  # only fill periods not already set by a higher-priority tag
    return merged


def extract_fundamentals(facts: dict, start_date: str) -> list:
    revenue = _extract_concept(facts, REVENUE_TAGS, start_date)
    net_income = _extract_concept(facts, NET_INCOME_TAGS, start_date)
    equity = _extract_concept(facts, EQUITY_TAGS, start_date)
    debt = _extract_concept(facts, TOTAL_DEBT_TAGS, start_date)
    eps = _extract_concept(facts, EPS_TAGS, start_date)

    all_periods = set(revenue) | set(net_income) | set(equity) | set(debt) | set(eps)

    rows = []
    for period in all_periods:
        # link to the LATEST of the concepts' first-release filings, so the
        # row's effective date is when every value in it was public (no
        # look-ahead if one concept first appeared in a later filing)
        releases = [c[period] for c in (revenue, net_income, equity, debt, eps) if period in c]
        accn = max(releases, key=lambda r: r[2])[1]
        rows.append((
            period,
            revenue.get(period, (None,))[0],
            net_income.get(period, (None,))[0],
            debt.get(period, (None,))[0],
            equity.get(period, (None,))[0],
            eps.get(period, (None,))[0],
            accn,
        ))
    return rows


def load_fundamentals(con, company_id: int, rows: list) -> int:
    # replace this company's rows wholesale, so periods no longer extracted
    # (e.g. first filed before the window) don't linger from earlier runs
    con.execute("DELETE FROM fact_fundamentals WHERE company_id = ?", [company_id])
    if not rows:
        return 0

    # fundamentals can reference filings older than what SEC's "recent filings"
    # endpoint returns (that endpoint is capped; company facts has full history).
    # Rather than fail the whole batch on one missing FK, drop the link for any
    # accession number we don't actually have a fact_filing row for.
    known_filing_ids = {
        r[0] for r in con.execute("SELECT filing_id FROM fact_filing").fetchall()
    }

    to_insert = []
    dropped_links = 0
    for row in rows:
        *fields, source_filing_id = row
        if source_filing_id is not None and source_filing_id not in known_filing_ids:
            dropped_links += 1
            source_filing_id = None
        to_insert.append((company_id, *fields, source_filing_id))

    if dropped_links:
        print(f"  note: {dropped_links} fundamentals rows reference filings outside "
              f"the recent-filings window — source_filing_id set to NULL for those")

    con.executemany(
        """
        INSERT OR REPLACE INTO fact_fundamentals
            (company_id, fiscal_period_end, revenue, net_income, total_debt, total_equity, eps, source_filing_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        to_insert,
    )
    return len(to_insert)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01", help="earliest filing date to pull")
    args = parser.parse_args()

    if not USER_AGENT:
        print("ERROR: SEC_USER_AGENT not set in .env. SEC will block requests without it.")
        sys.exit(1)

    # imported here, not at module level, so importing this module (e.g. from
    # tests) doesn't trigger a live fetch of the S&P 500 list
    from config.tickers import TICKERS

    con = duckdb.connect(DB_PATH)

    print("Fetching SEC ticker -> CIK mapping...")
    cik_map = get_cik_map()
    ticker_to_id = update_company_ciks(con, cik_map, TICKERS)

    total_filings = 0
    total_fundamentals = 0
    failed = []

    for ticker, name, *_ in TICKERS:
        cik = cik_map.get(ticker)
        company_id = ticker_to_id.get(ticker)
        if not cik or not company_id:
            failed.append(ticker)
            continue

        print(f"Fetching SEC data for {ticker} ({name})...")
        try:
            submissions = fetch_submissions(cik)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            n_filings = load_filings(con, company_id, submissions, args.start)
            total_filings += n_filings

            facts = fetch_company_facts(cik)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            fundamentals_rows = extract_fundamentals(facts, args.start)
            n_fund = load_fundamentals(con, company_id, fundamentals_rows)
            total_fundamentals += n_fund

            print(f"  {n_filings} filings, {n_fund} fundamentals periods")
        except requests.exceptions.RequestException as e:
            print(f"  ERROR fetching {ticker}: {e} — skipping")
            failed.append(ticker)

    con.close()

    print(f"\nDone. {total_filings} filings and {total_fundamentals} fundamentals rows loaded.")
    if failed:
        print(f"Failed/skipped tickers: {failed}")


if __name__ == "__main__":
    main()
