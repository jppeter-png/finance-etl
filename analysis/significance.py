"""
Significance tests for Analyses 1 and 2.

Standard t-tests would badly overstate significance here, because every
stock reacts to the SAME Fed decisions on the SAME days. 500 stocks x 18
decisions is not 9,000 independent observations. Both tests below keep
that cross-stock correlation intact and only randomize the event timing.

Analysis 1 (rate sensitivity), permutation test:
    Shuffle the order of the monthly Fed target rate changes (the same
    shuffle applied to every stock), recompute every ticker's correlation
    and each sector's average, and repeat. The p-value is the share of
    shuffles whose sector average is at least as far from zero as the
    real one (two-sided).

Analysis 2 (event volatility), placebo test:
    Place the same number of fake events at random trading days and
    recompute the event/baseline volatility ratio, many times. The
    p-value is the share of placebo ratios at least as large as the real
    one (one-sided: the question is whether volatility rises). For Fed
    events the 18 fake dates are shared by all stocks, like real ones;
    for earnings each company gets as many fake dates as it has filings.

Usage:
    python analysis/significance.py
"""

import os
import sys

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "finance.duckdb")
N_PERMUTATIONS = 10_000
N_PLACEBOS = 2_000
WINDOWS = (1, 5)
rng = np.random.default_rng(42)


# --- Analysis 1 -------------------------------------------------------------

def rate_sensitivity_test(con) -> pd.DataFrame:
    monthly = con.execute("""
        SELECT ticker, sector, date_trunc('month', date_id) AS month,
               last(COALESCE(adj_close, close) ORDER BY date_id) AS px,
               last(fed_target_rate ORDER BY date_id) AS rate
        FROM analytics_daily
        GROUP BY ticker, sector, date_trunc('month', date_id)
    """).df()
    valid = monthly["sector"].notna() & (monthly["sector"].str.strip() != "")
    monthly = monthly[valid].sort_values(["ticker", "month"])
    monthly["ret"] = monthly.groupby("ticker")["px"].pct_change()

    rates = monthly.groupby("month")["rate"].first().sort_index()
    delta = rates.diff().dropna()
    months = delta.index
    d = delta.to_numpy()

    returns = monthly.pivot(index="ticker", columns="month", values="ret").reindex(columns=months)
    sectors = monthly.groupby("ticker")["sector"].first().reindex(returns.index)
    R = returns.to_numpy()
    M = ~np.isnan(R)
    keep = M.sum(axis=1) >= 3
    R, M, sectors = R[keep], M[keep], sectors[keep].to_numpy()
    R0 = np.where(M, R, 0.0)
    n = M.sum(axis=1)
    Rc = (R0 - (R0.sum(axis=1) / n)[:, None]) * M
    r_norm = np.sqrt((Rc ** 2).sum(axis=1))

    def ticker_corrs(dp):
        md = (M @ dp) / n
        dc = (dp[None, :] - md[:, None]) * M
        # a ticker whose months all had zero rate change has undefined
        # correlation (NaN), and is skipped by nanmean below
        with np.errstate(invalid="ignore", divide="ignore"):
            return (Rc * dc).sum(axis=1) / (r_norm * np.sqrt((dc ** 2).sum(axis=1)))

    groups = {s: sectors == s for s in np.unique(sectors)}
    groups["All sectors"] = np.ones(len(sectors), dtype=bool)

    observed_corr = ticker_corrs(d)
    observed = {s: np.nanmean(observed_corr[m]) for s, m in groups.items()}
    extreme = dict.fromkeys(groups, 0)
    for _ in range(N_PERMUTATIONS):
        c = ticker_corrs(rng.permutation(d))
        for s, m in groups.items():
            if abs(np.nanmean(c[m])) >= abs(observed[s]):
                extreme[s] += 1

    out = pd.DataFrame({
        "sector": list(groups),
        "avg_correlation": [observed[s] for s in groups],
        "n_tickers": [int(m.sum()) for m in groups.values()],
        "p_value": [(extreme[s] + 1) / (N_PERMUTATIONS + 1) for s in groups],
    })
    print(f"(rate changed in {int((d != 0).sum())} of {len(d)} months)")
    return out.sort_values("avg_correlation").reset_index(drop=True)


# --- Analysis 2 -------------------------------------------------------------

def _load_daily(con):
    daily = con.execute("""
        SELECT company_id, date_id, ABS(daily_return) AS abs_ret
        FROM analytics_daily
        WHERE daily_return IS NOT NULL
        ORDER BY company_id, date_id
    """).df()
    daily["date_id"] = pd.to_datetime(daily["date_id"])
    return daily


def _pooled_ratio(event_sum, event_n, total_sum, total_n):
    return (event_sum / event_n) / ((total_sum - event_sum) / (total_n - event_n))


def fed_placebo_test(con, daily):
    event_dates = pd.to_datetime(con.execute("""
        WITH t AS (
            SELECT date_id, value, LAG(value) OVER (ORDER BY date_id) AS prev
            FROM fact_macro_observation WHERE series_id = 'DFEDTARU'
        )
        SELECT date_id FROM t WHERE prev IS NOT NULL AND value != prev
    """).df()["date_id"])

    by_date = daily.groupby("date_id")["abs_ret"].agg(["sum", "count"])
    dates = by_date.index
    S, C = by_date["sum"].to_numpy(), by_date["count"].to_numpy()
    total_s, total_c = S.sum(), C.sum()
    # anchor each event to the last trading day on/before it (as the event study does)
    anchors = np.searchsorted(dates.values, event_dates.values, side="right") - 1
    anchors = anchors[anchors >= 0]

    def ratio(anchor_idx, w):
        mask = np.zeros(len(dates), dtype=bool)
        for off in range(-w, w + 1):
            idx = anchor_idx + off
            mask[idx[(idx >= 0) & (idx < len(dates))]] = True
        return _pooled_ratio(S[mask].sum(), C[mask].sum(), total_s, total_c)

    results = []
    for w in WINDOWS:
        observed = ratio(anchors, w)
        placebo = np.array([
            ratio(rng.choice(len(dates), size=len(anchors), replace=False), w)
            for _ in range(N_PLACEBOS)
        ])
        p = ((placebo >= observed).sum() + 1) / (N_PLACEBOS + 1)
        results.append(("Fed rate changes", w, len(anchors), observed,
                        np.percentile(placebo, 95), p))
    return results


def earnings_placebo_test(con, daily):
    filings = con.execute("""
        SELECT DISTINCT company_id, filing_date
        FROM fact_filing WHERE form_type IN ('10-Q', '10-K')
    """).df()
    filings["filing_date"] = pd.to_datetime(filings["filing_date"])
    filings_by_co = filings.groupby("company_id")["filing_date"].apply(lambda s: s.values)

    companies = []
    for cid, g in daily.groupby("company_id"):
        if cid not in filings_by_co.index:
            continue
        dates = g["date_id"].values
        idx = np.searchsorted(dates, filings_by_co[cid], side="right") - 1
        companies.append((g["abs_ret"].to_numpy(), idx[idx >= 0]))
    total_s = sum(a.sum() for a, _ in companies)
    total_c = sum(len(a) for a, _ in companies)

    def ratio(anchor_lists, w):
        ev_s = ev_c = 0.0
        for (a, _), anchors in zip(companies, anchor_lists):
            mask = np.zeros(len(a), dtype=bool)
            for off in range(-w, w + 1):
                idx = anchors + off
                mask[idx[(idx >= 0) & (idx < len(a))]] = True
            ev_s += a[mask].sum()
            ev_c += mask.sum()
        return _pooled_ratio(ev_s, ev_c, total_s, total_c)

    n_events = sum(len(anchors) for _, anchors in companies)
    results = []
    n_placebos = N_PLACEBOS // 10  # thousands of events per draw; far fewer draws needed
    for w in WINDOWS:
        observed = ratio([anchors for _, anchors in companies], w)
        placebo = np.array([
            ratio([rng.choice(len(a), size=min(len(anchors), len(a)), replace=False)
                   for a, anchors in companies], w)
            for _ in range(n_placebos)
        ])
        p = ((placebo >= observed).sum() + 1) / (n_placebos + 1)
        results.append(("Earnings filings", w, n_events, observed,
                        np.percentile(placebo, 95), p))
    return results


def main():
    con = duckdb.connect(DB_PATH, read_only=True)

    print("=" * 70)
    print(f"ANALYSIS 1: sector rate sensitivity, permutation test ({N_PERMUTATIONS:,} shuffles)")
    print("=" * 70)
    print(rate_sensitivity_test(con).to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 70)
    print("ANALYSIS 2: event volatility, placebo test")
    print("=" * 70)
    daily = _load_daily(con)
    rows = earnings_placebo_test(con, daily) + fed_placebo_test(con, daily)
    con.close()
    table = pd.DataFrame(rows, columns=["event_type", "window_days", "n_events",
                                        "observed_ratio", "placebo_95th_pct", "p_value"])
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("""
Reading this:
  - Analysis 1 p-value: two-sided; how often shuffled rate-change timing
    produces a sector average at least as far from zero as the real one.
  - Analysis 2 p-value: one-sided; how often randomly placed fake events
    produce a volatility ratio at least as high as the real one.
  - The smallest reportable p-value is 1 / (number of draws + 1).
""")


if __name__ == "__main__":
    main()
