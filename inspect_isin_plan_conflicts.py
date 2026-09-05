"""
inspect_isin_plan_conflicts.py — look at real examples of the 162 flagged ISINs.

Determines whether isin_group()/get_spliced_series() have a real bug (plan/
option misclassified for some scheme names, causing a false ISIN collision)
or whether this is a genuine, rare AMFI data quirk unrelated to our parsing.
"""
import argparse
import sys

import nav_store


def run(db_path: str, limit: int) -> int:
    con = nav_store.connect(db_path)
    rows = con.execute("""
        SELECT isin_growth, amfi_code, scheme_name, plan, option, source
        FROM scheme
        WHERE isin_growth IN (
            SELECT isin_growth FROM scheme
            WHERE isin_growth IS NOT NULL
            GROUP BY isin_growth
            HAVING count(DISTINCT plan) FILTER (WHERE plan != 'UNKNOWN') > 1
                OR count(DISTINCT option) FILTER (WHERE option != 'UNKNOWN') > 1
        )
        ORDER BY isin_growth, amfi_code
        LIMIT ?
    """, [limit]).fetchall()

    current_isin = None
    for isin, code, name, plan, option, source in rows:
        if isin != current_isin:
            print()
            current_isin = isin
        print(f"  {isin}  code={code:<8} plan={plan:<8} option={option:<8} "
              f"src={source:<14} {name}")

    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    return run(a.db, a.limit)


if __name__ == "__main__":
    sys.exit(main())
