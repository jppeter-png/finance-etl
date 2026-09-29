"""
Exports small, pre-computed summary CSVs for the Tableau dashboard.
These are the results of analyses already validated in Python — Tableau's
job here is visualization and interactivity, not recomputing the stats.

Outputs (to analysis/):
    event_volatility_results.csv
    net_margin_quintile_spread_by_quarter.csv
    factor_spread_summary.csv

Usage:
    python analysis/export_for_tableau.py
"""

import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from make_charts import _event_ratio  # noqa: E402
from fundamentals_factors import load_panel, add_factors, quintile_spread, spread_tstat  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
OUT_DIR = os.path.dirname(__file__)


def export_event_volatility(con):
    earnings_q = """
        SELECT DISTINCT company_id, filing_date AS event_date
        FROM fact_filing WHERE form_type IN ('10-Q', '10-K')
    """
    macro_q = """
        WITH fedtarget AS (
            SELECT date_id, value, LAG(value) OVER (ORDER BY date_id) AS prev_value
            FROM fact_macro_observation WHERE series_id = 'DFEDTARU'
        ),
        rate_change_dates AS (
            SELECT date_id AS event_date FROM fedtarget
            WHERE prev_value IS NOT NULL AND value != prev_value
        )
        SELECT c.company_id, r.event_date FROM rate_change_dates r CROSS JOIN dim_company c
    """

    rows = []
    for label, query in [("Earnings filings", earnings_q), ("Fed rate changes", macro_q)]:
        for window in (1, 5):
            ratio = _event_ratio(con, query, window)
            rows.append({"event_type": label, "window_days": window, "volatility_ratio": ratio})

    df = pd.DataFrame(rows)
    out = os.path.join(OUT_DIR, "event_volatility_results.csv")
    df.to_csv(out, index=False)
    print(f"Saved {out}")
    print(df.to_string(index=False))


def export_fundamentals(con):
    panel = load_panel(con)
    panel = add_factors(panel)

    net_margin_spreads = quintile_spread(panel, "net_margin")
    out1 = os.path.join(OUT_DIR, "net_margin_quintile_spread_by_quarter.csv")
    net_margin_spreads.to_csv(out1, index=False)
    print(f"\nSaved {out1}")

    summary_rows = []
    for factor in ["net_margin", "debt_to_equity", "eps"]:
        spreads = quintile_spread(panel, factor)
        if not spreads.empty:
            summary_rows.append({
                "factor": factor,
                "avg_quintile_spread": spreads["spread"].mean(),
                "pct_quarters_positive": (spreads["spread"] > 0).mean(),
                "n_quarters": len(spreads),
                "t_stat": spread_tstat(spreads),
            })
    summary_df = pd.DataFrame(summary_rows)
    out2 = os.path.join(OUT_DIR, "factor_spread_summary.csv")
    summary_df.to_csv(out2, index=False)
    print(f"Saved {out2}")
    print(summary_df.to_string(index=False))


def main():
    con = duckdb.connect(DB_PATH, read_only=True)
    print("Exporting event volatility results...")
    export_event_volatility(con)
    print("\nExporting fundamentals factor results...")
    export_fundamentals(con)
    con.close()


if __name__ == "__main__":
    main()
