#!/usr/bin/env python3
"""
FundEval L1 -- NAV anomaly inspector.

Run after panel_diagnostics.py flagged that splice_jumps has entries far beyond
anything a real market move can produce (avg_lr of 2.3 across 208 "mid-series"
jumps -- that's not a crash, that's a data error).

Two things this does:
  1. Buckets ALL flagged jumps by size, so we know how much of the 222 is
     "impossible" (>2.0 log-return, i.e. >7x overnight -- no fund does this)
     vs "extreme but conceivably real" (0.35-0.7ish, plausible for a genuine
     side-pocket writeoff).
  2. Pulls the RAW (scheme_code, date, nav) rows around the worst examples,
     from the source nav table -- not the spliced series -- so we can see the
     actual numbers and tell a decimal/unit error from a real corporate action.

Read-only against both DBs.

    python3 inspect_nav_anomalies.py --out fundeval_analysis.duckdb --src mf_nav_full.duckdb
"""

import argparse

import duckdb


def rule(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fundeval_analysis.duckdb")
    ap.add_argument("--src", default="mf_nav_full.duckdb")
    ap.add_argument("--impossible-lr", type=float, default=1.0,
                     help="|log return| above this is treated as certainly an error, not a market move")
    ap.add_argument("--context-days", type=int, default=10)
    args = ap.parse_args()

    con = duckdb.connect(args.out, read_only=True)
    con.execute(f"ATTACH '{args.src}' AS src (READ_ONLY)")

    rule("1. FULL DISTRIBUTION OF FLAGGED JUMP SIZES")
    print(con.execute(f"""
        SELECT
            CASE WHEN abs(lr) > 2.0 THEN '>2.0  (>7x overnight -- impossible)'
                 WHEN abs(lr) > {args.impossible_lr} THEN
                      '{args.impossible_lr}-2.0  (very likely a data error)'
                 WHEN abs(lr) > 0.5 THEN '0.5-{args.impossible_lr}  (extreme, maybe real)'
                 ELSE '0.35-0.5  (large but plausible)' END AS band,
            count(*) n,
            round(min(abs(lr)), 3) min_abs_lr,
            round(max(abs(lr)), 3) max_abs_lr
        FROM splice_jumps
        GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    print(f"\nfunds with at least one jump above {args.impossible_lr} "
          f"(these need their whole series checked, not just the flagged date):")
    print(con.execute(f"""
        SELECT j.fund_id, count(*) n_impossible_jumps,
               (SELECT any_value(scheme_name) FROM fund_map fm WHERE fm.fund_id = j.fund_id) AS a_name
        FROM splice_jumps j
        WHERE abs(lr) > {args.impossible_lr}
        GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False)[:4000])

    rule(f"2. RAW NAV VALUES AROUND THE {args.impossible_lr}+ JUMPS (source table, not spliced)")
    targets = con.execute(f"""
        SELECT DISTINCT j.fund_id, j.d
        FROM splice_jumps j WHERE abs(j.lr) > {args.impossible_lr}
        ORDER BY abs(j.lr) DESC LIMIT 15
    """).fetchall()

    if not targets:
        print(f"(none above {args.impossible_lr} -- lower --impossible-lr to inspect smaller ones)")
    for fund_id, d in targets:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT scheme_code FROM fund_map WHERE fund_id = ?", [fund_id]).fetchall()]
        print(f"\n--- fund_id {fund_id}  codes={codes}  around {d} ---")
        for code in codes:
            rows = con.execute(f"""
                SELECT amfi_code, nav_date, nav
                FROM src.nav
                WHERE amfi_code = ?
                  AND nav_date BETWEEN DATE '{d}' - INTERVAL {args.context_days} DAY
                                   AND DATE '{d}' + INTERVAL {args.context_days} DAY
                ORDER BY nav_date
            """, [int(code)]).df()
            if len(rows):
                print(f"  code {code}:")
                print(rows.to_string(index=False))

    con.close()


if __name__ == "__main__":
    main()
