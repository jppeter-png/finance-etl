"""Tests for the XBRL parsing in extract/filings.py, using hand-built
"company facts" payloads shaped like SEC's API response."""

from filings import _extract_concept, _is_clean_period, extract_fundamentals


def _fact(end, val, filed, accn, start=None, form="10-Q"):
    e = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form}
    if start:
        e["start"] = start
    return e


def _facts(concepts):
    """concepts: {tag: {unit: [facts]}}"""
    return {"facts": {"us-gaap": {tag: {"units": units} for tag, units in concepts.items()}}}


# --- _is_clean_period (Bug #2: quarterly vs. year-to-date collisions) -------

def test_quarter_and_full_year_windows_pass():
    assert _is_clean_period({"start": "2023-01-01", "end": "2023-03-31"})
    assert _is_clean_period({"start": "2023-01-01", "end": "2023-12-31"})


def test_six_and_nine_month_ytd_windows_are_rejected():
    assert not _is_clean_period({"start": "2023-01-01", "end": "2023-06-30"})
    assert not _is_clean_period({"start": "2023-01-01", "end": "2023-09-30"})


def test_instant_facts_always_pass():
    assert _is_clean_period({"end": "2023-09-30"})


# --- _extract_concept -------------------------------------------------------

def test_keeps_first_filed_value_not_later_comparative():
    # Bug #5: the same period is repeated as a comparative in next year's
    # filing; the original release is what the market saw
    facts = _facts({"Revenues": {"USD": [
        _fact("2023-03-31", 100, "2024-05-01", "later", start="2023-01-01"),
        _fact("2023-03-31", 100, "2023-05-01", "original", start="2023-01-01"),
    ]}})
    out = _extract_concept(facts, ["Revenues"], "2021-01-01")
    assert out["2023-03-31"] == (100, "original", "2023-05-01")


def test_drops_periods_first_released_before_window():
    facts = _facts({"Revenues": {"USD": [
        _fact("2020-09-30", 90, "2020-11-01", "original", start="2020-07-01"),
        _fact("2020-09-30", 90, "2021-11-01", "comparative", start="2020-07-01"),
    ]}})
    assert _extract_concept(facts, ["Revenues"], "2021-01-01") == {}


def test_rejects_ytd_duplicate_for_same_period_end():
    facts = _facts({"Revenues": {"USD": [
        _fact("2023-09-30", 300, "2023-11-01", "a", start="2023-01-01"),  # 9-month YTD
        _fact("2023-09-30", 100, "2023-11-01", "a", start="2023-07-01"),  # quarter
    ]}})
    assert _extract_concept(facts, ["Revenues"], "2021-01-01")["2023-09-30"][0] == 100


def test_merges_across_tags_with_priority():
    # Bug #1: companies switch tags mid-history; merge rather than stop at
    # the first tag, and let the higher-priority tag win on overlap
    facts = _facts({
        "Revenues": {"USD": [
            _fact("2023-03-31", 100, "2023-05-01", "a", start="2023-01-01"),
        ]},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
            _fact("2023-03-31", 999, "2023-05-01", "a", start="2023-01-01"),
            _fact("2023-06-30", 110, "2023-08-01", "b", start="2023-04-01"),
        ]},
    })
    out = _extract_concept(
        facts, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], "2021-01-01"
    )
    assert out["2023-03-31"][0] == 100
    assert out["2023-06-30"][0] == 110


def test_reads_per_share_unit():
    # Bug #6: EPS is reported in USD/shares, not USD
    facts = _facts({"EarningsPerShareDiluted": {"USD/shares": [
        _fact("2023-03-31", 1.52, "2023-05-01", "a", start="2023-01-01"),
    ]}})
    assert _extract_concept(facts, ["EarningsPerShareDiluted"], "2021-01-01")["2023-03-31"][0] == 1.52


def test_ignores_untracked_forms():
    facts = _facts({"Revenues": {"USD": [
        _fact("2023-03-31", 100, "2023-05-01", "a", start="2023-01-01", form="S-1"),
    ]}})
    assert _extract_concept(facts, ["Revenues"], "2021-01-01") == {}


# --- extract_fundamentals ---------------------------------------------------

def test_row_links_to_latest_first_release_among_concepts():
    # revenue first public in the original 10-Q, equity only in a later
    # filing: the row must not be treated as public before both were
    facts = _facts({
        "Revenues": {"USD": [
            _fact("2023-03-31", 100, "2023-05-01", "original", start="2023-01-01"),
        ]},
        "StockholdersEquity": {"USD": [
            _fact("2023-03-31", 40, "2023-08-01", "later"),
        ]},
    })
    (row,) = extract_fundamentals(facts, "2021-01-01")
    period, revenue, net_income, debt, equity, eps, accn = row
    assert (period, revenue, equity, accn) == ("2023-03-31", 100, 40, "later")
    assert net_income is None and debt is None and eps is None
