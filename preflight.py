#!/usr/bin/env python3
"""
FundEval L1 preflight.

Introspects mf_nav_full.duckdb, resolves table/column names, runs the integrity
checks that would otherwise silently corrupt the returns panel, and prints a
CONFIG block to paste into the top of returns_panel.py.

Read-only. Touches nothing.

    python3 preflight.py --db /path/to/mf_nav_full.duckdb
"""

import argparse
import sys

import duckdb

# Candidate column names, checked case-insensitively, first match wins.
CANDS = {
    "DATE_COL":   ["nav_date", "date", "d", "asof_date", "nav_dt"],
    "CODE_COL":   ["scheme_code", "code", "amfi_code", "scheme_cd"],
    "NAV_COL":    ["nav", "nav_value", "net_asset_value", "nav_val"],
    "NAME_COL":   ["scheme_name", "name", "scheme"],
    "ISIN_G_COL": ["isin_growth", "isin_div_payout_growth", "isin_payout", "isin"],
    "ISIN_D_COL": ["isin_div", "isin_reinvest", "isin_div_reinvestment",
                   "isin_reinvestment"],
    "CAT_COL":    ["category", "scheme_category", "cat"],
    "OPT_COL":    ["option", "option_type", "plan_option"],
    "PLAN_COL":   ["plan", "plan_type"],
    "STATUS_COL": ["status", "is_active", "staleness"],
    "AMC_COL":    ["amc", "amc_name", "fund_house"],
}


def pick(cols, cands):
    low = {c.lower(): c for c in cols}
    for cand in cands:
        if cand in low:
            return low[cand]
    return None


def rule(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="mf_nav_full.duckdb")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    print(f"duckdb {duckdb.__version__}  |  db: {args.db}")

    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    if not tables:
        sys.exit("No tables found. Wrong file?")

    rule("TABLES")
    info = {}
    for t in tables:
        cols = con.execute(f'DESCRIBE "{t}"').df()
        n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        info[t] = {"cols": list(cols["column_name"]), "n": n}
        print(f"\n--- {t}  ({n:,} rows)")
        print(cols[["column_name", "column_type"]].to_string(index=False))
        try:
            print(con.execute(f'SELECT * FROM "{t}" LIMIT 3').df().to_string(index=False))
        except Exception as e:
            print(f"  (sample failed: {e})")

    # --- identify the NAV table: has code + date + nav, most rows -------------
    nav_t = max(
        (t for t, v in info.items()
         if pick(v["cols"], CANDS["CODE_COL"])
         and pick(v["cols"], CANDS["DATE_COL"])
         and pick(v["cols"], CANDS["NAV_COL"])),
        key=lambda t: info[t]["n"], default=None)

    # --- identify the METADATA table: has code + name, fewest rows -----------
    meta_t = min(
        (t for t, v in info.items()
         if t != nav_t
         and pick(v["cols"], CANDS["CODE_COL"])
         and pick(v["cols"], CANDS["NAME_COL"])),
        key=lambda t: info[t]["n"], default=None)

    if not nav_t:
        sys.exit("Could not identify a NAV table. Set names manually in returns_panel.py.")
    if not meta_t:
        print("\n!! No separate metadata table found; assuming it lives in the NAV table.")
        meta_t = nav_t

    nav_c = {k: pick(info[nav_t]["cols"], v) for k, v in CANDS.items()}
    meta_c = {k: pick(info[meta_t]["cols"], v) for k, v in CANDS.items()}

    D, C, N = nav_c["DATE_COL"], nav_c["CODE_COL"], nav_c["NAV_COL"]

    rule("NAV TABLE FACTS")
    q = f'''SELECT min("{D}") lo, max("{D}") hi, count(*) n_rows,
                   count(DISTINCT "{C}") codes,
                   sum(CASE WHEN "{N}" IS NULL OR "{N}" <= 0 THEN 1 ELSE 0 END) bad_nav
            FROM "{nav_t}"'''
    print(con.execute(q).df().to_string(index=False))

    dupes = con.execute(
        f'''SELECT count(*) FROM (SELECT "{C}", "{D}" FROM "{nav_t}"
            GROUP BY 1, 2 HAVING count(*) > 1)''').fetchone()[0]
    print(f"\nduplicate (code, date) pairs: {dupes:,}"
          + ("   <-- dedup required" if dupes else "   (clean)"))

    print("\nrows per calendar year:")
    print(con.execute(
        f'''SELECT year("{D}") AS yr, count(*) AS n_rows, count(DISTINCT "{C}") AS codes
            FROM "{nav_t}" GROUP BY 1 ORDER BY 1''').df().to_string(index=False))

    rule("IDENTITY / ISIN")
    ig, idv = meta_c["ISIN_G_COL"], meta_c["ISIN_D_COL"]
    if ig:
        print(con.execute(
            f'''SELECT
                  sum(CASE WHEN "{ig}" IS NULL THEN 1 ELSE 0 END) AS isin_null,
                  sum(CASE WHEN trim("{ig}") = '-' THEN 1 ELSE 0 END) AS isin_dash,
                  sum(CASE WHEN trim("{ig}") = '' THEN 1 ELSE 0 END) AS isin_blank,
                  count(DISTINCT nullif(trim("{ig}"), '-')) AS distinct_isin,
                  count(*) AS n_rows
                FROM "{meta_t}"''').df().to_string(index=False))
        print("\nISIN groups spanning >1 scheme_code (top 10) -- these are the splices:")
        print(con.execute(
            f'''SELECT nullif(trim("{ig}"), '-') AS isin,
                       count(DISTINCT "{meta_c['CODE_COL']}") AS codes,
                       string_agg(DISTINCT "{meta_c['NAME_COL']}", ' | ') AS names
                FROM "{meta_t}" WHERE nullif(trim("{ig}"), '-') IS NOT NULL
                GROUP BY 1 HAVING count(DISTINCT "{meta_c['CODE_COL']}") > 1
                ORDER BY codes DESC LIMIT 10''').df().to_string(index=False)[:3000])
    else:
        print("!! No growth-ISIN column resolved. Splicing cannot work without it.")

    rule("PLAN / OPTION")
    nm = meta_c["NAME_COL"]
    for key, label in [("OPT_COL", "option"), ("PLAN_COL", "plan"),
                       ("STATUS_COL", "status"), ("CAT_COL", "category")]:
        col = meta_c[key]
        if col:
            print(f"\n{label} -> column '{col}':")
            print(con.execute(
                f'''SELECT "{col}" AS value, count(*) AS n FROM "{meta_t}"
                    GROUP BY 1 ORDER BY n DESC LIMIT 12''').df().to_string(index=False))
        else:
            print(f"\n{label}: no column -- will be inferred from scheme_name")
    if nm:
        print("\nname-inferred plan split (fallback path):")
        print(con.execute(
            f'''SELECT CASE WHEN lower("{nm}") LIKE '%direct%' THEN 'DIRECT'
                            ELSE 'REGULAR' END AS plan, count(*) AS n
                FROM "{meta_t}" GROUP BY 1''').df().to_string(index=False))
        print("\nname-inferred IDCW share (fallback path):")
        print(con.execute(
            f'''SELECT CASE WHEN regexp_matches(lower("{nm}"),
                       'idcw|dividend|income distribution') THEN 'IDCW'
                       ELSE 'NON-IDCW' END AS opt, count(*) AS n
                FROM "{meta_t}" GROUP BY 1''').df().to_string(index=False))

    rule("PASTE THIS INTO returns_panel.py")
    cfg = {
        "SRC_DB": args.db, "NAV_TABLE": nav_t, "META_TABLE": meta_t,
        "DATE_COL": D, "CODE_COL": C, "NAV_COL": N,
        "META_CODE_COL": meta_c["CODE_COL"], "NAME_COL": meta_c["NAME_COL"],
        "ISIN_G_COL": ig, "ISIN_D_COL": idv, "CAT_COL": meta_c["CAT_COL"],
        "OPT_COL": meta_c["OPT_COL"], "PLAN_COL": meta_c["PLAN_COL"],
        "AMC_COL": meta_c["AMC_COL"],
    }
    print("CONFIG = {")
    for k, v in cfg.items():
        print(f'    "{k}": {v!r},')
    print("}")
    print("\nCheck every line above against reality before running the panel builder.")


if __name__ == "__main__":
    main()
