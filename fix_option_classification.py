"""
fix_option_classification.py — retroactively apply the classify_option fix.

The bug (missing "income distribution"/"capital withdrawal" recognition)
lived in code, but its wrong output is already sitting in the database as
stored `option` values. Since scheme_name is already stored, re-deriving
option is a pure function of data already present -- no network needed.

Safe to run repeatedly: recomputing from name is idempotent. Correctly-
classified rows get the same value back; only genuinely mis-classified
ones (found 2026-08-30: schemes named "...Income Distribution CUM Capital
Withdrawal Option" whose FUND name happened to contain "Growth") change.

Run:  python fix_option_classification.py --db mf_nav_full.duckdb
"""

import argparse
import sys

import pandas as pd

import nav_store


def run(db_path: str) -> int:
    con = nav_store.connect(db_path)

    rows = con.execute("SELECT amfi_code, scheme_name, option FROM scheme").fetchall()
    print(f"Re-deriving option for {len(rows)} schemes from stored scheme_name...")

    updates = [(code, nav_store.classify_option(name))
              for code, name, _old in rows]
    changed = sum(1 for (code, new), (_, _, old) in zip(updates, rows) if new != old)
    print(f"  {changed} rows will actually change value")

    df = pd.DataFrame(updates, columns=["amfi_code", "option"])
    con.register("_option_fix_staging", df)
    try:
        con.execute("""
            UPDATE scheme SET option = s.option
            FROM _option_fix_staging s
            WHERE scheme.amfi_code = s.amfi_code
        """)
    finally:
        con.unregister("_option_fix_staging")

    print(f"Done. {changed} option values corrected.")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    return run(a.db)


if __name__ == "__main__":
    sys.exit(main())
