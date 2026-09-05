"""
apply_manual_amc_mapping.py — the two clean, small, verifiable cases from
the 133-code review (2026-08-30).

Both sourced directly, not guessed:

  IDBI -> LIC MF: official merger table, https://www.licmf.com/LICMF-IDBI-Mergerpage/
  Effective 29-Jul-2023 (matches our data: all 10 codes' last NAV = 28-Jul-2023).
  IMPORTANT: this was NOT a uniform rename. 10 of IDBI's 20 schemes merged into
  PRE-EXISTING LIC schemes -- one with a genuine category change (IDBI Credit
  Risk Fund -> LIC MF Bond Fund, Credit Risk -> Medium to Long Duration). All
  10 of our uncategorized IDBI Direct+Growth codes fall in this merged bucket.

  PGIM India internal consolidation, effective 30-Sep-2023 (matches our data:
  last NAV = 29-Sep-2023). NOT an AMC exit -- PGIM India was still PGIM India
  in 2026; this was folding smaller debt schemes into larger ones within the
  same fund house. Confirmed via Value Research coverage + PaytmMoney's own
  scheme pages showing the exact merge date.
    - PGIM India Banking & PSU Debt Fund  -> PGIM India Corporate Bond Fund
    - PGIM India Short Duration Fund      -> PGIM India Corporate Bond Fund
    - PGIM India Low Duration Fund        -> PGIM India Money Market Fund

Rather than hardcode a category STRING (risking a format that doesn't match
what's already in the database), this looks up the successor's ACTUAL stored
category and copies it -- self-verifying the successor exists, and
guaranteeing vocabulary consistency.

Usage
-----
    python apply_manual_amc_mapping.py --selftest              offline
    python apply_manual_amc_mapping.py --db mf_nav_full.duckdb  dry run (default)
    python apply_manual_amc_mapping.py --db mf_nav_full.duckdb --apply
"""

import argparse
import sys

import nav_store

# (old_code, old_name_for_display, [successor name candidates, tried in order])
MAPPING = [
    # --- IDBI -> LIC MF, effective 29-Jul-2023, per LIC's official table ---
    (143353, "IDBI Banking & Financial Services Fund",
     ["LIC MF Banking and Financial Services", "LIC MF Banking & Financial Services"]),
    (127181, "IDBI Credit Risk Fund",
     ["LIC MF Medium to Long Term Fund", "LIC MF Medium to Long Duration Bond Fund",
      "LIC MF Bond Fund"]),
    (123637, "IDBI Equity Advantage Fund",
     ["LIC MF ELSS", "LIC MF Tax Plan"]),
    (128236, "IDBI Flexi Cap Fund",
     ["LIC MF Flexi Cap Fund"]),
    (139971, "IDBI Hybrid Equity Fund",
     ["LIC MF Aggressive Hybrid Fund", "LIC MF Equity Hybrid Fund"]),
    (118344, "IDBI India Top 100 Equity Fund",
     ["LIC MF Large Cap Fund"]),
    (118345, "IDBI Liquid Fund",
     ["LIC MF Liquid Fund"]),
    (118347, "IDBI NIFTY 50 Index Fund",
     ["LIC MF Nifty 50 Index Fund", "LIC MF Nifty Index Fund"]),
    (118349, "IDBI Short Term Bond Fund",
     ["LIC MF Short Term Fund", "LIC MF Short Duration Fund", "LIC MF Short Term Debt Fund"]),
    (118350, "IDBI UST",
     ["LIC MF Ultra Short Duration Fund", "LIC MF Ultra Short Term Fund"]),
    # --- PGIM India internal consolidation, effective 30-Sep-2023 ---
    (138564, "PGIM India Banking and PSU Debt fund",
     ["PGIM India Corporate Bond Fund"]),
    (138270, "PGIM India Short Duration Fund",
     ["PGIM India Corporate Bond Fund"]),
    (138443, "PGIM India Low Duration Fund",
     ["PGIM India Money Market Fund"]),
]


def find_successor_category(con, candidates):
    """Try each candidate name (ILIKE substring) in order; return the first
    match with a non-null category, as (matched_code, matched_name, category)."""
    for candidate in candidates:
        row = con.execute(
            "SELECT amfi_code, scheme_name, category FROM scheme "
            "WHERE scheme_name ILIKE ? AND category IS NOT NULL LIMIT 1",
            [f"%{candidate}%"],
        ).fetchone()
        if row:
            return row
    return None


def run(db_path: str, apply: bool) -> int:
    con = nav_store.connect(db_path)
    resolved = unresolved = 0

    for old_code, old_name, candidates in MAPPING:
        current = con.execute(
            "SELECT category FROM scheme WHERE amfi_code = ?", [old_code]
        ).fetchone()
        if current is None:
            print(f"  SKIP  {old_code} not found in this database at all: {old_name}")
            continue
        if current[0] is not None:
            print(f"  SKIP  {old_code} already has a category ({current[0]}): {old_name}")
            continue

        match = find_successor_category(con, candidates)
        if not match:
            print(f"  UNRESOLVED  {old_code} ({old_name}): none of "
                  f"{candidates} found with a category in this database")
            unresolved += 1
            continue

        succ_code, succ_name, category = match
        print(f"  {old_code} ({old_name})")
        print(f"    -> {succ_code} ({succ_name})")
        print(f"    category: {category}")
        if apply:
            con.execute("UPDATE scheme SET category = ? WHERE amfi_code = ?",
                       [category, old_code])
            print(f"    APPLIED")
        resolved += 1
        print()

    print(f"\n{resolved} resolved, {unresolved} unresolved out of {len(MAPPING)}")
    if not apply and resolved:
        print("Dry run -- nothing written. Re-run with --apply to commit.")

    con.close()
    return 0


def _selftest() -> int:
    import tempfile, os

    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")

    tmp = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    con = nav_store.connect(tmp)

    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option, category) VALUES
        (128236, 'IDBI FLEXI CAP FUND Growth Direct', 'DIRECT', 'GROWTH', NULL),
        (999900, 'LIC MF Flexi Cap Fund - Direct Plan - Growth', 'DIRECT', 'GROWTH',
         'Equity Scheme - Flexi Cap Fund'),
        (138270, 'PGIM India Short Duration Fund - Direct Plan - Growth', 'DIRECT', 'GROWTH', NULL),
        (999901, 'PGIM India Corporate Bond Fund - Direct Plan - Growth', 'DIRECT', 'GROWTH',
         'Debt Scheme - Corporate Bond Fund'),
        (999902, 'Already Has Category Fund', 'DIRECT', 'GROWTH', 'Some Category'),
    """)

    match = find_successor_category(con, ["LIC MF Flexi Cap Fund"])
    check("finds successor by name", match is not None and match[2] == 'Equity Scheme - Flexi Cap Fund')

    no_match = find_successor_category(con, ["Totally Nonexistent Fund Name"])
    check("returns None when nothing matches", no_match is None)

    # dry run should not write anything
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(tmp, apply=False)
    check("dry run leaves category NULL", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=128236").fetchone()[0] is None)

    con.close()
    con = nav_store.connect(tmp)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        run(tmp, apply=True)
    check("apply writes the correct category (IDBI->LIC case)", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=128236"
    ).fetchone()[0] == 'Equity Scheme - Flexi Cap Fund')
    check("apply writes the correct category (PGIM internal case)", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=138270"
    ).fetchone()[0] == 'Debt Scheme - Corporate Bond Fund')
    check("already-categorized row untouched", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=999902"
    ).fetchone()[0] == 'Some Category')

    con.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    return run(a.db, a.apply)


if __name__ == "__main__":
    sys.exit(main())
