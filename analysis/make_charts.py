"""
Generates static PNG charts summarizing the three analyses, for embedding
in FINDINGS.md / README.md on GitHub (GitHub renders images from a repo
path directly, no JS needed — simplest, most portable option).

Requires matplotlib (not part of the core pipeline dependencies):
    python3 -m pip install matplotlib

Outputs to charts/:
    01_rate_sensitivity_by_sector.png
    02_event_volatility_comparison.png
    03_net_margin_quintile_spread_by_quarter.png
    04_factor_spread_summary.png

Usage:
    python analysis/make_charts.py
"""

import os
import sys

import duckdb
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fundamentals_factors import load_panel, add_factors, quintile_spread  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
CHARTS_DIR = os.path.join(os.path.dirname(__file__), "..", "charts")

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "font.size": 11,
})

POSITIVE_COLOR = "#2a9d8f"
NEGATIVE_COLOR = "#e76f51"


def chart_rate_sensitivity(con):
    monthly = con.execute("""
        SELECT ticker, sector, date_trunc('month', date_id) AS month,
               last(COALESCE(adj_close, close) ORDER BY date_id) AS month_end_close,
               last(fed_target_rate ORDER BY date_id) AS month_end_rate
        FROM analytics_daily
        GROUP BY ticker, sector, date_trunc('month', date_id)
        ORDER BY ticker, month
    """).df()

    monthly["monthly_return"] = monthly.groupby("ticker")["month_end_close"].pct_change()
    monthly["rate_delta"] = monthly.groupby("ticker")["month_end_rate"].diff()
    clean = monthly.dropna(subset=["monthly_return", "rate_delta"])

    ticker_corr = (
        clean.groupby(["ticker", "sector"])
        .apply(lambda g: g["monthly_return"].corr(g["rate_delta"]), include_groups=False)
        .reset_index(name="corr")
    )
    # dropna() alone only catches NaN — a blank/empty-string sector (e.g. the
    # orphaned tickers no longer in the current S&P 500 list, whose sector
    # never got backfilled) survives dropna and forms its own unlabeled group
    valid_sector = ticker_corr["sector"].notna() & (ticker_corr["sector"].str.strip() != "")
    sector_summary = (
        ticker_corr[valid_sector]
        .groupby("sector")["corr"].mean()
        .sort_values()
    )

    fig, ax = plt.subplots(figsize=(9, 6))
    colors = [NEGATIVE_COLOR if v < 0 else POSITIVE_COLOR for v in sector_summary.values]
    ax.barh(sector_summary.index, sector_summary.values, color=colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Avg correlation: monthly return vs. monthly Fed target rate change")
    ax.set_title("Rate Sensitivity by Sector (503 tickers, 2021–2026)")
    fig.tight_layout()
    out = os.path.join(CHARTS_DIR, "01_rate_sensitivity_by_sector.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved {out}")


def _event_ratio(con, event_query: str, window: int) -> float:
    sql = f"""
    WITH trading AS (
        SELECT company_id, date_id, daily_return,
               ROW_NUMBER() OVER (PARTITION BY company_id ORDER BY date_id) AS rn
        FROM analytics_daily WHERE daily_return IS NOT NULL
    ),
    events AS ({event_query}),
    event_anchor AS (
        SELECT e.company_id, e.event_date, t.rn AS anchor_rn
        FROM events e
        ASOF JOIN trading t ON e.company_id = t.company_id AND t.date_id <= e.event_date
    ),
    event_window AS (
        SELECT DISTINCT t.company_id, t.date_id, t.daily_return
        FROM event_anchor ea
        JOIN trading t ON t.company_id = ea.company_id
            AND t.rn BETWEEN ea.anchor_rn - {window} AND ea.anchor_rn + {window}
    )
    SELECT
        (SELECT AVG(ABS(daily_return)) FROM event_window) AS event_avg,
        (SELECT AVG(ABS(t.daily_return)) FROM trading t
         WHERE NOT EXISTS (SELECT 1 FROM event_window ew
                            WHERE ew.company_id = t.company_id AND ew.date_id = t.date_id)) AS baseline_avg
    """
    event_avg, baseline_avg = con.execute(sql).fetchone()
    return event_avg / baseline_avg


def chart_event_volatility(con):
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

    data = {
        "Earnings filings": [_event_ratio(con, earnings_q, 1), _event_ratio(con, earnings_q, 5)],
        "Fed rate changes": [_event_ratio(con, macro_q, 1), _event_ratio(con, macro_q, 5)],
    }

    fig, ax = plt.subplots(figsize=(8, 5.5))
    x = range(2)
    width = 0.35
    ax.bar([i - width/2 for i in x], data["Earnings filings"], width,
           label="Earnings filings", color=POSITIVE_COLOR)
    ax.bar([i + width/2 for i in x], data["Fed rate changes"], width,
           label="Fed rate changes", color="#457b9d")
    ax.axhline(1.0, color="black", linewidth=0.8, linestyle="--", label="Baseline (1.0x)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(["±1 trading day", "±5 trading days"])
    ax.set_ylabel("Volatility ratio (event window / baseline)")
    ax.set_title("Volatility Clustering: Earnings Filings vs. Fed Rate Changes")
    ax.legend()
    fig.tight_layout()
    out = os.path.join(CHARTS_DIR, "02_event_volatility_comparison.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved {out}")


def chart_net_margin_by_quarter(panel: pd.DataFrame):
    spreads = quintile_spread(panel, "net_margin").sort_values("quarter")

    fig, ax = plt.subplots(figsize=(12, 5.5))
    colors = [NEGATIVE_COLOR if v < 0 else POSITIVE_COLOR for v in spreads["spread"]]
    ax.bar(spreads["quarter"], spreads["spread"], color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.axhline(spreads["spread"].mean(), color="gray", linewidth=1, linestyle="--",
               label=f"Average ({spreads['spread'].mean():.1%})")
    ax.set_ylabel("Top-vs-bottom quintile spread (forward return)")
    n_pos = int((spreads["spread"] > 0).sum())
    ax.set_title(f"Net Margin Factor: Top-minus-Bottom Quintile Spread "
                 f"(positive in {n_pos}/{len(spreads)} quarters)")
    ax.legend()
    plt.xticks(rotation=90)
    fig.tight_layout()
    out = os.path.join(CHARTS_DIR, "03_net_margin_quintile_spread_by_quarter.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved {out}")


def chart_factor_summary(panel: pd.DataFrame):
    factors = {}
    for factor in ["net_margin", "debt_to_equity", "eps"]:
        spreads = quintile_spread(panel, factor)
        if not spreads.empty:
            factors[factor] = spreads["spread"].mean()

    fig, ax = plt.subplots(figsize=(7, 5))
    labels = list(factors.keys())
    values = list(factors.values())
    colors = [POSITIVE_COLOR if v >= 0 else NEGATIVE_COLOR for v in values]
    bars = ax.bar(labels, values, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("Avg quarterly top-vs-bottom quintile spread")
    ax.set_title("Fundamentals Factors: Average Quintile Spread")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1%}"))
    ax.bar_label(bars, labels=[f"{v:+.2%}" for v in values], padding=3)
    ax.margins(y=0.15)
    fig.tight_layout()
    out = os.path.join(CHARTS_DIR, "04_factor_spread_summary.png")
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Saved {out}")


def main():
    os.makedirs(CHARTS_DIR, exist_ok=True)
    con = duckdb.connect(DB_PATH, read_only=True)

    print("Building chart 1: rate sensitivity by sector...")
    chart_rate_sensitivity(con)

    print("Building chart 2: event volatility comparison...")
    chart_event_volatility(con)

    print("Building charts 3-4: fundamentals factors...")
    panel = load_panel(con)
    panel = add_factors(panel)
    chart_net_margin_by_quarter(panel)
    chart_factor_summary(panel)

    con.close()
    print(f"\nAll charts saved to {CHARTS_DIR}/")


if __name__ == "__main__":
    main()
