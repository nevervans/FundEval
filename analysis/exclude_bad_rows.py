#!/usr/bin/env python3
"""
FundEval L1 -- exclude confirmed single-row data errors.

Scope: only rows labeled REVERT in jump_classification.csv -- confirmed via
full-trajectory inspection to be isolated bad entries that settle back near
1.0 shortly after (ratio_settled ~= 1.0). These get deleted from nav_fund
entirely (treated as a missing observation, which the panel's existing
ANCHOR_TOL_DAYS/END_TOL_DAYS tolerance already handles gracefully).

Also checks for "round x1" REBASE rows (ratio ~= 1.0, not a real face-value
change) that do NOT have a matching REVERT row for the same fund -- these are
printed as needing a manual trajectory check rather than guessed at, since
the bad day in that case is the row's predecessor, not the row itself.

    python3 exclude_bad_rows.py --out fundeval_analysis.duckdb \
        --classification jump_classification.csv
"""
import argparse
import math

import duckdb
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="fundeval_analysis.duckdb")
ap.add_argument("--classification", default="jump_classification.csv")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

cls = pd.read_csv(args.classification, parse_dates=["date"])

revert = cls[cls.label == "REVERT"].copy()
print(f"{len(revert)} confirmed REVERT rows -- will be deleted from nav_fund:")
print(revert[["fund_id", "date", "ratio_immediate", "ratio_settled", "scheme_name"]]
      .to_string(index=False))

cls["log10_ratio"] = cls["ratio_immediate"].apply(
    lambda r: math.log10(r) if pd.notnull(r) and r > 0 else None)
trivial = cls[
    (cls.label == "REBASE") & cls["rebase_shape"].astype(str).str.startswith("round")
    & (cls["log10_ratio"].abs() <= 0.5)
].copy()

# Pre-approved single-row corrections: (fund_id -> bad date to delete).
# These are confirmed via manual trajectory inspection to be a bad print on
# the PRECEDING day, not the flagged jump date itself -- see comment above.
# Add to this dict only after confirming with inspect_full_trajectory.py.
KNOWN_SAFE_CORRECTIONS = {
    "INF090I01817": {"bad_date": pd.Timestamp("2006-07-04"),
                      "note": "single bad AMFI print, confirmed 2026-09-10"},
}

unmatched = trivial[~trivial["fund_id"].isin(revert["fund_id"])]
print(f"\n{len(trivial)} 'round x1' rows found; "
      f"{len(trivial) - len(unmatched)} share a fund_id with a REVERT row above (will self-resolve).")

known_safe_rows = unmatched[unmatched["fund_id"].isin(KNOWN_SAFE_CORRECTIONS)]
still_unmatched = unmatched[~unmatched["fund_id"].isin(KNOWN_SAFE_CORRECTIONS)]

if len(known_safe_rows):
    print(f"{len(known_safe_rows)} match a pre-approved known-safe correction -- will auto-apply:")
    print(known_safe_rows[["fund_id", "date", "ratio_immediate", "scheme_name"]].to_string(index=False))

if len(still_unmatched):
    print(f"{len(still_unmatched)} have NO matching REVERT row and are NOT pre-approved -- "
          f"check these individually with inspect_full_trajectory.py before assuming anything, "
          f"do not auto-fix:")
    print(still_unmatched[["fund_id", "date", "ratio_immediate", "scheme_name"]].to_string(index=False))

if args.dry_run:
    print("\n--dry-run: stopping before deleting anything")
    raise SystemExit

con = duckdb.connect(args.out)
con.execute("CREATE OR REPLACE TEMP TABLE to_delete AS SELECT * FROM revert")
before = con.execute("SELECT count(*) FROM nav_fund").fetchone()[0]
con.execute("""
    DELETE FROM nav_fund
    WHERE (fund_id, d) IN (SELECT fund_id, date FROM to_delete)
""")
for fund_id, info in KNOWN_SAFE_CORRECTIONS.items():
    if fund_id in known_safe_rows["fund_id"].values:
        con.execute(
            "DELETE FROM nav_fund WHERE fund_id = ? AND d = ?",
            [fund_id, info["bad_date"].date()],
        )
        print(f"applied known-safe correction: deleted {fund_id} on {info['bad_date'].date()} ({info['note']})")
after = con.execute("SELECT count(*) FROM nav_fund").fetchone()[0]
print(f"\nnav_fund: {before:,} -> {after:,} rows ({before - after} deleted)")
print(f"\nNext: python3 returns_panel.py --panel-only --out {args.out}")
con.close()

if len(still_unmatched):
    print(f"\nEXIT 2: {len(still_unmatched)} row(s) still need manual trajectory review "
          f"(see list above). Not treated as a hard failure, but data should not be "
          f"trusted until reviewed.")
    raise SystemExit(2)
