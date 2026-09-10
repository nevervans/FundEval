#!/usr/bin/env python3
"""
FundEval L1 -- apply face-value continuity correction.

Scope, deliberately narrow: this touches ONLY rows from jump_classification.csv
where label == REBASE and the multiplier is a clean round number away from 1.0
(x0.01, x0.1, x10, x100, ...). Those are confirmed face-value consolidations /
unit splits -- real corporate actions, but not real RETURNS, so returns must be
computed across them as if the redenomination never happened.

Everything else is left alone on purpose:
  * REBASE rows that are NOT round (segregated portfolios, credit write-downs)
    are real events. Adjusting them away would hide the exact risk signal the
    persistence study cares about.
  * REVERT rows and the near-1.0 "round x1" rows are NOT corrected here --
    they need the full-trajectory check (inspect_full_trajectory.py) first,
    since a quick two-point fix could leave a longer bad stretch untouched.
  * INSUFFICIENT_FORWARD_DATA rows are left alone -- mostly segregated
    portfolios winding down, real by the same logic as above.

Mechanics: for a confirmed event at (fund_id, date d) with factor
r = exp(lr) (the exact single-step ratio from splice_jumps, not the smoothed
classifier ratio), every NAV for that fund_id on or after d is divided by r.
Multiple events for the same fund compose correctly because they're applied
in date order and each only touches d and later.

This OVERWRITES nav_fund in --out (a derived table, not the raw foundation
DB -- mf_nav_full.duckdb is never touched). Follow with:
    python3 returns_panel.py --panel-only --out fundeval_analysis.duckdb

    python3 apply_nav_corrections.py --out fundeval_analysis.duckdb \
        --classification jump_classification.csv
"""

import argparse

import duckdb
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fundeval_analysis.duckdb")
    ap.add_argument("--classification", default="jump_classification.csv")
    ap.add_argument("--round-tol", type=float, default=0.10,
                     help="must match the tolerance rebase_classifier.py used for 'round'")
    ap.add_argument("--min-log10-dev", type=float, default=0.5,
                     help="minimum |log10(ratio)| to treat as a real level shift, "
                          "not the near-1.0 'round x1' classifier artefact")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()

    cls = pd.read_csv(args.classification, parse_dates=["date"])
    import math
    cls["log10_ratio"] = cls["ratio_immediate"].apply(
        lambda r: math.log10(r) if pd.notnull(r) and r > 0 else None)

    confirmed = cls[
        (cls["label"] == "REBASE")
        & cls["rebase_shape"].astype(str).str.startswith("round")
        & (cls["log10_ratio"].abs() > args.min_log10_dev)
    ].copy()
    confirmed["adj_factor"] = confirmed["lr"].apply(lambda x: pow(2.718281828459045, x))

    print(f"{len(cls)} total classified jumps")
    print(f"{len(confirmed)} confirmed round-multiplier events selected for correction")
    print(confirmed[["fund_id", "date", "adj_factor", "rebase_shape", "scheme_name"]]
          .sort_values("date").to_string(index=False)[:6000])

    if args.dry_run:
        print("\n--dry-run: stopping before writing anything")
        return

    con = duckdb.connect(args.out)
    con.execute("CREATE OR REPLACE TEMP TABLE corrections AS SELECT * FROM confirmed")

    before_rows, before_funds = con.execute(
        "SELECT count(*), count(DISTINCT fund_id) FROM nav_fund").fetchone()

    con.execute("""
        CREATE OR REPLACE TEMP TABLE corrections_cum AS
        SELECT fund_id, date AS event_date,
               exp(sum(ln(adj_factor)) OVER (PARTITION BY fund_id ORDER BY date
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS cum_adj
        FROM corrections
    """)

    con.execute("""
        CREATE OR REPLACE TABLE nav_fund_corrected AS
        SELECT n.fund_id, n.d, n.nav / coalesce(cc.cum_adj, 1.0) AS nav
        FROM nav_fund n
        ASOF LEFT JOIN corrections_cum cc
          ON n.fund_id = cc.fund_id AND n.d >= cc.event_date
    """)

    after_rows, after_funds = con.execute(
        "SELECT count(*), count(DISTINCT fund_id) FROM nav_fund_corrected").fetchone()
    print(f"\nnav_fund:            {before_rows:,} rows, {before_funds:,} funds")
    print(f"nav_fund_corrected:  {after_rows:,} rows, {after_funds:,} funds")

    if before_rows != after_rows or before_funds != after_funds:
        raise SystemExit("row/fund count changed during correction -- stopping without overwriting nav_fund")

    print("\n--- sanity check: one corrected fund, before vs after, around its event date ---")
    sample_fund = confirmed.iloc[0]["fund_id"] if len(confirmed) else None
    if sample_fund:
        sample_date = confirmed.iloc[0]["date"]
        print(f"fund_id={sample_fund}  event={sample_date.date()}")
        check = con.execute(f"""
            SELECT n.d, n.nav AS nav_before, c.nav AS nav_after
            FROM nav_fund n JOIN nav_fund_corrected c ON c.fund_id = n.fund_id AND c.d = n.d
            WHERE n.fund_id = '{sample_fund}'
              AND n.d BETWEEN DATE '{sample_date.date()}' - INTERVAL 5 DAY
                          AND DATE '{sample_date.date()}' + INTERVAL 5 DAY
            ORDER BY n.d
        """).df()
        print(check.to_string(index=False))

    existing_tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "nav_fund_raw_precorrection" in existing_tables:
        raise SystemExit(
            "nav_fund_raw_precorrection already exists -- refusing to proceed. "
            "This is almost always a leftover backup from an earlier interrupted run. "
            "Inspect it (row count vs today's nav_fund), then drop or rename it before re-running this script."
        )

    con.execute("ALTER TABLE nav_fund RENAME TO nav_fund_raw_precorrection")
    con.execute("ALTER TABLE nav_fund_corrected RENAME TO nav_fund")
    print("\nnav_fund overwritten with corrected values.")
    print("Original preserved as nav_fund_raw_precorrection in case this needs to be undone.")
    print(f"\nNext: python3 returns_panel.py --panel-only --out {args.out}")

    con.close()


if __name__ == "__main__":
    main()
