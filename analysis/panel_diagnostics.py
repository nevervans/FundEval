#!/usr/bin/env python3
"""
FundEval L1 diagnostics -- run once after returns_panel.py, before trusting
the panel for persistence tests.

Investigates the four things the first real run surfaced:
  1. ISIN-less codes -- concentrated somewhere explicable, or scattered
     (which would be worse, since it means live funds are fragmenting)?
  2. splice_jumps -- near an actual code-handoff boundary (splice risk) or
     mid-single-code (real market event or a NAV face-value reset)?
  3. Early-window within-year attrition -- closed-ended maturities (real,
     expected) or a reporting-tolerance artefact (needs a fix)?
  4. NAV reporting cadence -- actually ~daily throughout, or did it change
     across eras in a way that breaks the fixed sqrt(252) annualisation?

Read-only against both DBs.

    python3 panel_diagnostics.py --out fundeval_analysis.duckdb --src mf_nav_full.duckdb
"""

import argparse

import duckdb


def rule(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fundeval_analysis.duckdb")
    ap.add_argument("--src", default="mf_nav_full.duckdb")
    ap.add_argument("--attrition-fy", type=int, default=2007,
                     help="which FY's within-window attrition to inspect")
    args = ap.parse_args()

    con = duckdb.connect(args.out, read_only=True)
    con.execute(f"ATTACH '{args.src}' AS src (READ_ONLY)")

    rule("1. ISIN-LESS CODES -- WHERE DO THEY CLUSTER?")
    print(con.execute("""
        SELECT s.status,
               CASE WHEN s.first_nav_date < DATE '2013-01-01' THEN 'pre-2013'
                    ELSE '2013+' END AS era,
               count(*) n
        FROM fund_map fm
        JOIN src.scheme s ON s.amfi_code = cast(fm.scheme_code AS INTEGER)
        WHERE fm.no_isin
        GROUP BY 1, 2 ORDER BY 3 DESC
    """).df().to_string(index=False))

    print("\nscheme_type mix of ISIN-less codes:")
    print(con.execute("""
        SELECT s.scheme_type, count(*) n
        FROM fund_map fm
        JOIN src.scheme s ON s.amfi_code = cast(fm.scheme_code AS INTEGER)
        WHERE fm.no_isin
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).df().to_string(index=False))

    print("\nfor comparison, scheme_type mix of the FULL regular-growth universe:")
    print(con.execute("""
        SELECT s.scheme_type, count(*) n
        FROM fund_map fm
        JOIN src.scheme s ON s.amfi_code = cast(fm.scheme_code AS INTEGER)
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).df().to_string(index=False))

    rule("2. SPLICE_JUMPS -- NEAR A CODE HANDOFF, OR MID-SERIES?")
    print(con.execute("""
        WITH multi AS (
            SELECT fund_id FROM fund_map
            GROUP BY fund_id HAVING count(DISTINCT scheme_code) > 1
        ),
        code_span AS (
            SELECT fm.fund_id, fm.scheme_code, max(n.nav_date) AS code_end
            FROM fund_map fm
            JOIN src.nav n ON cast(n.amfi_code AS VARCHAR) = fm.scheme_code
            WHERE fm.fund_id IN (SELECT fund_id FROM multi)
            GROUP BY 1, 2
        ),
        boundaries AS (
            SELECT fund_id, code_end AS boundary_date
            FROM code_span
            QUALIFY row_number() OVER (PARTITION BY fund_id ORDER BY code_end) <
                    count(*) OVER (PARTITION BY fund_id)
        ),
        tagged AS (
            SELECT j.fund_id, j.d, j.lr,
                   min(abs(date_diff('day', j.d, b.boundary_date))) AS gap
            FROM splice_jumps j
            LEFT JOIN boundaries b ON b.fund_id = j.fund_id
            GROUP BY 1, 2, 3
        )
        SELECT
            CASE WHEN gap IS NULL THEN 'not a multi-code fund'
                 WHEN gap <= 7 THEN 'within 7 days of a code handoff -- CHECK SPLICE'
                 ELSE 'mid-series -- likely real event or NAV reset' END AS bucket,
            count(*) n, round(avg(lr), 3) avg_lr
        FROM tagged GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    print("\nworst 10 by magnitude, with bucket and fund name:")
    print(con.execute("""
        WITH multi AS (
            SELECT fund_id FROM fund_map
            GROUP BY fund_id HAVING count(DISTINCT scheme_code) > 1
        ),
        code_span AS (
            SELECT fm.fund_id, fm.scheme_code, max(n.nav_date) AS code_end
            FROM fund_map fm
            JOIN src.nav n ON cast(n.amfi_code AS VARCHAR) = fm.scheme_code
            WHERE fm.fund_id IN (SELECT fund_id FROM multi)
            GROUP BY 1, 2
        ),
        boundaries AS (
            SELECT fund_id, code_end AS boundary_date
            FROM code_span
            QUALIFY row_number() OVER (PARTITION BY fund_id ORDER BY code_end) <
                    count(*) OVER (PARTITION BY fund_id)
        ),
        tagged AS (
            SELECT j.fund_id, j.d, j.lr,
                   min(abs(date_diff('day', j.d, b.boundary_date))) AS gap
            FROM splice_jumps j
            LEFT JOIN boundaries b ON b.fund_id = j.fund_id
            GROUP BY 1, 2, 3
        )
        SELECT t.fund_id, t.d, round(t.lr, 3) AS lr, t.gap,
               (SELECT any_value(fm.scheme_name) FROM fund_map fm
                WHERE fm.fund_id = t.fund_id) AS a_name
        FROM tagged t
        ORDER BY abs(t.lr) DESC LIMIT 10
    """).df().to_string(index=False)[:3000])

    rule(f"3. FY{args.attrition_fy} WITHIN-WINDOW NON-SURVIVORS -- CLOSED-ENDED MATURITIES?")
    print(con.execute(f"""
        SELECT s.scheme_type, count(*) n
        FROM returns_panel rp
        JOIN fund_map fm ON fm.fund_id = rp.fund_id
        JOIN src.scheme s ON s.amfi_code = cast(fm.scheme_code AS INTEGER)
        WHERE rp.horizon_y = 1 AND rp.fy_start = {args.attrition_fy} AND NOT rp.survived
        GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    print(f"\nfor comparison, scheme_type of FY{args.attrition_fy} SURVIVORS:")
    print(con.execute(f"""
        SELECT s.scheme_type, count(*) n
        FROM returns_panel rp
        JOIN fund_map fm ON fm.fund_id = rp.fund_id
        JOIN src.scheme s ON s.amfi_code = cast(fm.scheme_code AS INTEGER)
        WHERE rp.horizon_y = 1 AND rp.fy_start = {args.attrition_fy} AND rp.survived
        GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    rule("4. NAV REPORTING CADENCE -- IS sqrt(252) VALID ACROSS ALL ERAS?")
    print(con.execute("""
        SELECT year(nav_date) yr, count(*) obs, count(DISTINCT amfi_code) codes,
               round(count(*)::DOUBLE / nullif(count(DISTINCT amfi_code), 0), 1) AS obs_per_code
        FROM src.nav GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))

    print("\nmedian gap in days between consecutive NAVs, by year (all codes pooled):")
    print(con.execute("""
        WITH gaps AS (
            SELECT amfi_code, nav_date,
                   date_diff('day',
                       lag(nav_date) OVER (PARTITION BY amfi_code ORDER BY nav_date),
                       nav_date) AS gap
            FROM src.nav
        )
        SELECT year(nav_date) yr, median(gap) med_gap_days, count(*) n
        FROM gaps WHERE gap IS NOT NULL AND gap > 0
        GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))

    con.close()


if __name__ == "__main__":
    main()
