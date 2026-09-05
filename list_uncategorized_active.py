"""
list_uncategorized_active.py — the full list, for manual review.

Recently-active (last NAV >= 2023) Direct+Growth schemes with no category.
Sorted by name so same-AMC-prefix cases cluster together and patterns
(e.g. every Reliance-branded name) are easy to spot by eye.
"""
import argparse
import sys

import nav_store


def run(db_path: str) -> int:
    con = nav_store.connect(db_path)
    rows = con.execute("""
        SELECT amfi_code, scheme_name, isin_growth, last_nav_date
        FROM scheme
        WHERE category IS NULL AND plan='DIRECT' AND option='GROWTH'
          AND last_nav_date >= DATE '2023-01-01'
        ORDER BY scheme_name
    """).fetchall()

    print(f"{len(rows)} recently-active, uncategorized, Direct+Growth schemes:\n")
    for code, name, isin, last_date in rows:
        isin_display = isin if isin else "(no ISIN)"
        print(f"  {code:>8}  {isin_display:<14}  last={last_date}  {name}")

    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    return run(a.db)


if __name__ == "__main__":
    sys.exit(main())
