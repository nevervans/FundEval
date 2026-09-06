import argparse
import sys
import nav_store

def run(db_path):
    con = nav_store.connect(db_path)
    rows = con.execute("""
        SELECT DISTINCT amfi_code, scheme_name, category, last_nav_date FROM scheme
        WHERE scheme_name ILIKE '%LIC MF%' OR scheme_name ILIKE '%LIC Mutual Fund%'
        ORDER BY scheme_name
    """).fetchall()
    print(f"{len(rows)} rows with 'LIC MF' or 'LIC Mutual Fund' in the name:\n")
    for code, name, cat, last in rows:
        print(f"  {code:>8}  last={last}  cat={cat}  {name}")
    con.close()
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    sys.exit(run(a.db))
