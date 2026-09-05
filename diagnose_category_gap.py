"""
diagnose_category_gap.py — is the remaining category gap "fine" or "broken"?

Not every code missing a category is the same problem. A fund whose last
NAV was 2010 is a genuinely matured FMP -- expected, not a bug. A fund
whose last NAV was last month has no business missing a category; that's
the same identity-chain-broke-on-rename shape as L&T -> HSBC, just for an
AMC transition (Reliance -> Nippon India? Baroda -> Baroda BNP Paribas?
something else?) where the ISIN apparently did NOT carry over the way it
did for L&T Mid Cap.

Run:  python diagnose_category_gap.py --db mf_nav_full.duckdb
"""

import argparse
import sys

import nav_store


def run(db_path: str) -> int:
    con = nav_store.connect(db_path)

    split = con.execute("""
        SELECT
          sum(CASE WHEN last_nav_date >= DATE '2023-01-01' THEN 1 ELSE 0 END) AS recently_active,
          sum(CASE WHEN last_nav_date <  DATE '2023-01-01' THEN 1 ELSE 0 END) AS long_dead,
          count(*) AS total
        FROM scheme
        WHERE category IS NULL AND plan='DIRECT' AND option='GROWTH'
    """).fetchone()

    print(f"Recently active (last NAV >= 2023) but no category: {split[0]}")
    print(f"Long dead (last NAV < 2023) -- likely genuine FMP maturity: {split[1]}")
    print(f"Total: {split[2]}\n")

    if split[0] == 0:
        print("Nothing recently active is uncategorized -- the whole gap is "
              "old matured schemes. Nothing to fix; safe to leave as-is.")
        con.close()
        return 0

    print(f"The {split[0]} recently-active ones, with sibling-ISIN check "
          f"(same diagnostic that would catch an L&T-Midcap-style rename):\n")
    rows = con.execute("""
        SELECT s.amfi_code, s.scheme_name, s.isin_growth, s.last_nav_date,
               (SELECT count(*) FROM scheme s2
                WHERE s2.isin_growth = s.isin_growth AND s2.amfi_code != s.amfi_code) AS siblings,
               (SELECT max(s2.category) FROM scheme s2
                WHERE s2.isin_growth = s.isin_growth AND s2.category IS NOT NULL) AS sibling_category
        FROM scheme s
        WHERE s.category IS NULL AND s.plan='DIRECT' AND s.option='GROWTH'
          AND s.last_nav_date >= DATE '2023-01-01'
        ORDER BY s.last_nav_date DESC
        LIMIT 30
    """).fetchall()

    zero_sibling = 0
    for code, name, isin, last_date, siblings, sib_cat in rows:
        flag = "NO ISIN AT ALL" if not isin else (
            "0 siblings -- ISIN never matched anything" if siblings == 0 else
            f"{siblings} sibling(s), none categorized" if not sib_cat else
            "has a categorized sibling?! inheritance should have caught this")
        if siblings == 0 or not isin:
            zero_sibling += 1
        print(f"  {code:>8}  last={last_date}  isin={isin}  -> {flag}")
        print(f"            {name}")

    print(f"\n{zero_sibling}/{len(rows)} shown have zero ISIN-linked siblings at all "
          f"-- for these, the ISIN itself changed on whatever transition they "
          f"went through (unlike L&T -> HSBC, where it didn't). ISIN-based "
          f"inheritance structurally cannot fix this; it would need a curated "
          f"AMC-rename name-mapping instead, which is separate, deliberate work "
          f"-- not something to improvise under time pressure.")

    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    return run(a.db)


if __name__ == "__main__":
    sys.exit(main())
