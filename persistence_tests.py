#!/usr/bin/env python3
"""
FundEval L2 -- persistence tests.

Does a fund's return quartile in one period predict its quartile in the next?
Tested three ways, on non-overlapping formation -> holding period pairs, for
both metrics (return and volatility) and both horizons (1y, 3y):

  1. Quartile transition matrix + Cramer's V  -- overall association strength
     across the full 4x4 structure, not just top/bottom.
  2. Malkiel repeat-winner Z-statistic -- the classic test: of funds that beat
     the median in the formation period, what fraction also beat it in the
     holding period? Z-tested against the 50% chance baseline.
  3. Spearman rank correlation -- uses the full within-period ranking, not
     just which quartile a fund lands in.

Eligibility, deliberately conservative: a fund enters a formation/holding pair
only if BOTH windows have coverage_frac >= --min-coverage and survived = true.
Partial-year windows produce noisy returns that don't belong in a persistence
ranking. This is a real constraint on sample size, not a formality -- printed
counts show exactly how much of the panel this excludes.

Non-overlapping pairing: for horizon h, a formation window at fy_start=f pairs
with the holding window at fy_start=f+h in the same phase group (phase =
fy_start % h) -- these are the windows the panel already computed as
independent, back-to-back periods, not the overlapping rolling ones.

    python3 persistence_tests.py --panel fundeval_analysis.duckdb
    python3 persistence_tests.py --panel fundeval_analysis.duckdb --horizon 3 --metric ann_vol
"""

import argparse

import duckdb
import numpy as np
import pandas as pd
from scipy import stats


def build_pairs(con, horizon, metric, min_coverage):
    """One row per (fund, formation_fy) with formation & holding metric values,
    restricted to pairs where both windows meet the coverage/survival bar."""
    df = con.execute(f"""
        WITH eligible AS (
            SELECT fund_id, fy_start, phase, {metric} AS val
            FROM returns_panel
            WHERE horizon_y = {horizon}
              AND coverage_frac >= {min_coverage}
              AND survived
              AND {metric} IS NOT NULL
        )
        SELECT f.fund_id, f.fy_start AS formation_fy, f.val AS formation_val,
               h.val AS holding_val
        FROM eligible f
        JOIN eligible h
          ON h.fund_id = f.fund_id
         AND h.phase = f.phase
         AND h.fy_start = f.fy_start + {horizon}
        ORDER BY f.fy_start, f.fund_id
    """).df()
    return df


def add_quartiles(df):
    """Quartile assigned WITHIN each formation_fy cross-section separately --
    a fund's quartile is relative to its peers in that period, not the pooled
    sample across all years."""
    df = df.copy()
    df["formation_q"] = df.groupby("formation_fy")["formation_val"] \
        .transform(lambda s: pd.qcut(s, 4, labels=[1, 2, 3, 4], duplicates="drop"))
    df["holding_q"] = df.groupby("formation_fy")["holding_val"] \
        .transform(lambda s: pd.qcut(s, 4, labels=[1, 2, 3, 4], duplicates="drop"))
    return df.dropna(subset=["formation_q", "holding_q"])


def cramers_v(table):
    chi2, p, dof, _ = stats.chi2_contingency(table)
    n = table.to_numpy().sum()
    k = min(table.shape) - 1
    v = np.sqrt(chi2 / (n * k)) if k > 0 and n > 0 else float("nan")
    return v, chi2, p, dof


def malkiel_z(df):
    """Of formation-period above-median funds, what fraction are ALSO
    above-median in the holding period? Z-test vs the 50% chance baseline,
    done separately per formation_fy then pooled (so one huge year can't
    dominate), then also pooled naively for comparison."""
    rows = []
    for fy, g in df.groupby("formation_fy"):
        med_f = g["formation_val"].median()
        med_h = g["holding_val"].median()
        winners = g[g["formation_val"] >= med_f]
        if len(winners) < 10:
            continue
        repeat = (winners["holding_val"] >= med_h).sum()
        n = len(winners)
        p_hat = repeat / n
        z = (p_hat - 0.5) / np.sqrt(0.25 / n)
        rows.append((fy, n, repeat, p_hat, z))
    per_year = pd.DataFrame(rows, columns=["formation_fy", "n_winners", "n_repeat", "repeat_rate", "z"])

    total_n = per_year["n_winners"].sum()
    total_repeat = per_year["n_repeat"].sum()
    pooled_p = total_repeat / total_n if total_n else float("nan")
    pooled_z = (pooled_p - 0.5) / np.sqrt(0.25 / total_n) if total_n else float("nan")
    return per_year, pooled_p, pooled_z, total_n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="fundeval_analysis.duckdb")
    ap.add_argument("--horizon", type=int, default=1, choices=[1, 3])
    ap.add_argument("--metric", default="window_return", choices=["window_return", "ann_vol"])
    ap.add_argument("--min-coverage", type=float, default=0.9)
    args = ap.parse_args()

    con = duckdb.connect(args.panel, read_only=True)

    total = con.execute(f"""
        SELECT count(*) FROM returns_panel WHERE horizon_y = {args.horizon}""").fetchone()[0]
    df = build_pairs(con, args.horizon, args.metric, args.min_coverage)
    print(f"horizon={args.horizon}y  metric={args.metric}  min_coverage={args.min_coverage}")
    print(f"{total:,} total {args.horizon}y fund-windows in panel")
    print(f"{len(df):,} eligible formation->holding pairs "
          f"({len(df) / total:.1%} of total, both windows meeting the coverage/survival bar)")

    if len(df) < 50:
        print("\nToo few eligible pairs for a meaningful test at this coverage bar. "
              "Try --min-coverage lower, or check the panel.")
        return

    df = add_quartiles(df)
    print(f"{len(df):,} pairs after quartile assignment (some formation years may have <4 "
          f"distinct value groups and get dropped by pandas' qcut)")

    print("\n--- pairs per formation year ---")
    print(df.groupby("formation_fy").size().to_string())

    print("\n--- quartile transition matrix (rows=formation Q, cols=holding Q) ---")
    table = pd.crosstab(df["formation_q"], df["holding_q"])
    print(table.to_string())
    print("\n--- as row percentages (what formation-Q1 funds become) ---")
    print((table.div(table.sum(axis=1), axis=0) * 100).round(1).to_string())

    v, chi2, p, dof = cramers_v(table)
    print(f"\nCramer's V = {v:.4f}   (chi2={chi2:.1f}, dof={dof}, p={p:.4g})")
    print("  0 = no association beyond chance; 1 = perfect persistence. "
          "Rule of thumb: <0.1 negligible, 0.1-0.3 weak, 0.3-0.5 moderate, >0.5 strong.")

    per_year, pooled_p, pooled_z, total_n = malkiel_z(df)
    print(f"\n--- Malkiel repeat-winner test (per formation year) ---")
    print(per_year.round(4).to_string(index=False))
    print(f"\npooled across all years: {total_n:,} above-median funds, "
          f"{pooled_p:.1%} repeated above-median next period")
    print(f"pooled Z = {pooled_z:.3f}  (|Z|>1.96 significant at 5%, >2.58 at 1%)")

    rho, srho_p = stats.spearmanr(df["formation_val"], df["holding_val"])
    print(f"\n--- Spearman rank correlation (formation value vs holding value, pooled) ---")
    print(f"rho = {rho:.4f}  p = {srho_p:.4g}  n = {len(df):,}")
    print("  Pooling years mixes different market regimes into one number -- "
          "treat this as a headline, not the full picture. Per-year version below.")
    per_year_rho = df.groupby("formation_fy").apply(
        lambda g: pd.Series(stats.spearmanr(g["formation_val"], g["holding_val"]),
                             index=["rho", "p"]) if len(g) >= 10 else pd.Series([np.nan, np.nan], index=["rho", "p"]))
    print(per_year_rho.round(4).to_string())

    con.close()


if __name__ == "__main__":
    main()
