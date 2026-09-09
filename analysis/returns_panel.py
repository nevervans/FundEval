#!/usr/bin/env python3
"""
FundEval L1 -- returns panel.

Builds one row per (fund, horizon, window) with the metrics the persistence
tests need: window return, CAGR, annualised vol, max drawdown, plus the
coverage/survival flags that let L2 decide what to exclude.

Design commitments:
  * Point-in-time. A window's metrics use only NAVs inside that window (plus an
    anchor at/before its start). Nothing downstream of the window end leaks in.
  * One series per fund. Codes are collapsed to a fund_id via ISIN, so a rename
    (L&T Mid Cap -> HSBC Midcap) is one continuous series, not two truncated ones.
  * Single plan+option universe. Regular-Growth by default. Mixing Regular with
    Direct manufactures persistence out of a pure fee spread.
  * Flags, not filters. Dead funds and thin-coverage funds are kept and marked.
    Survivorship decisions belong in L2 where they are visible.
  * Source DB is attached READ_ONLY. Output goes to a separate file.

    python3 preflight.py --db mf_nav_full.duckdb        # paste CONFIG below
    python3 returns_panel.py --plan REGULAR
    python3 returns_panel.py --plan DIRECT --start-fy 2013
"""

import argparse
import datetime as dt

import duckdb

# --- paste from preflight.py, then verify every line -------------------------
CONFIG = {
    "SRC_DB": "test_fund.duckdb",
    "NAV_TABLE": "nav",
    "META_TABLE": "scheme",
    "DATE_COL": "nav_date",
    "CODE_COL": "amfi_code",
    "NAV_COL": "nav",
    "META_CODE_COL": "amfi_code",
    "NAME_COL": "scheme_name",
    "ISIN_G_COL": "isin_growth",
    "ISIN_D_COL": "isin_div",
    "CAT_COL": "category",
    "OPT_COL": "option",
    "PLAN_COL": "plan",
    "AMC_COL": "fund_house",
}

FY_MONTH, FY_DAY = 4, 1        # Indian fiscal year start; matches the 2006-04-01 floor
HORIZONS = [1, 3]
END_TOL_DAYS = 15              # last NAV within this of window end => survived
ANCHOR_TOL_DAYS = 15           # anchor NAV within this of window start => eligible
JUMP_THRESHOLD = 0.35          # |1-day log move| flagged as a possible splice artefact
TRADING_DAYS = 252


def q(col):
    return f'"{col}"' if col else "NULL"


def build_fund_map(con, cfg, plan, categories=None):
    """Collapse scheme codes to fund_id (ISIN), restricted to one plan+option.
    Always excludes rows with a NULL category -- AMFI leaves matured/closed-
    ended legacy products (Fixed Term Plans, Capital Protection, Interval
    Income Funds) uncategorized, so this filter is what makes a categories=None
    run "comprehensive" rather than "everything AMFI ever listed, including
    dead products with no active NAV."
    categories, if given, further restricts to category names containing any
    of the given substrings (case-insensitive) -- for a small, fast,
    single-plan universe scoped to just what you need, instead of the full
    universe."""
    name = q(cfg["NAME_COL"])

    if cfg["PLAN_COL"]:
        plan_expr = (f"CASE WHEN upper(cast({q(cfg['PLAN_COL'])} AS VARCHAR)) "
                     f"LIKE '%DIRECT%' THEN 'DIRECT' ELSE 'REGULAR' END")
    else:
        plan_expr = f"CASE WHEN lower({name}) LIKE '%direct%' THEN 'DIRECT' ELSE 'REGULAR' END"

    if cfg["OPT_COL"]:
        opt_expr = (f"CASE WHEN upper(cast({q(cfg['OPT_COL'])} AS VARCHAR)) "
                    f"LIKE '%GROWTH%' THEN 'GROWTH' ELSE 'IDCW' END")
    else:
        # Anything not explicitly distribution-flavoured is treated as growth --
        # AMFI growth schemes often omit the word entirely. Assumption is printed.
        opt_expr = (f"CASE WHEN regexp_matches(lower({name}), "
                    f"'idcw|dividend|income distribution') THEN 'IDCW' ELSE 'GROWTH' END")

    ig = f"nullif(nullif(trim(cast({q(cfg['ISIN_G_COL'])} AS VARCHAR)), '-'), '')"
    idv = f"nullif(nullif(trim(cast({q(cfg['ISIN_D_COL'])} AS VARCHAR)), '-'), '')" \
        if cfg["ISIN_D_COL"] else "NULL"

    # Substring OR-match on category, case-insensitive -- deliberately not an
    # exact match, since AMFI category strings aren't consistent even for the
    # same fund type (e.g. "Equity Scheme - Large Cap Fund" vs "Equity
    # Schemes - Large Cap Fund"). An exact match would silently drop some.
    cat_filter = ""
    if categories:
        conds = " OR ".join(
            "lower(category) LIKE '%" + c.strip().lower().replace("'", "''") + "%'"
            for c in categories
        )
        cat_filter = f" AND ({conds})"

    con.execute(f"""
        CREATE OR REPLACE TABLE fund_map AS
        WITH tagged AS (
            SELECT
                cast({q(cfg['META_CODE_COL'])} AS VARCHAR) AS scheme_code,
                {name}                                     AS scheme_name,
                {q(cfg['CAT_COL'])}                        AS category,
                {ig}                                       AS isin_g,
                {idv}                                      AS isin_d,
                {plan_expr}                                AS plan,
                {opt_expr}                                 AS option_type
            FROM src.{cfg['META_TABLE']}
        )
        SELECT
            scheme_code, scheme_name, category, plan, option_type,
            coalesce(isin_g, isin_d, 'CODE:' || scheme_code) AS fund_id,
            (isin_g IS NULL AND isin_d IS NULL)              AS no_isin
        FROM tagged
        WHERE plan = '{plan}' AND option_type = 'GROWTH' AND category IS NOT NULL{cat_filter}
    """)
    return con.execute("""
        SELECT count(*) codes, count(DISTINCT fund_id) funds, sum(no_isin::INT) no_isin
        FROM fund_map""").fetchone()


def build_nav_fund(con, cfg):
    """Splice codes into one series per fund_id, preferring the longest code."""
    D, C, N = q(cfg["DATE_COL"]), q(cfg["CODE_COL"]), q(cfg["NAV_COL"])
    con.execute(f"""
        CREATE OR REPLACE TABLE nav_fund AS
        WITH counts AS (
            SELECT cast({C} AS VARCHAR) AS scheme_code, count(*) AS n
            FROM src.{cfg['NAV_TABLE']} GROUP BY 1
        ),
        prio AS (
            SELECT m.scheme_code, m.fund_id, coalesce(c.n, 0) AS n
            FROM fund_map m LEFT JOIN counts c USING (scheme_code)
        ),
        joined AS (
            SELECT p.fund_id, n.{D} AS d, cast(n.{N} AS DOUBLE) AS nav,
                   row_number() OVER (PARTITION BY p.fund_id, n.{D}
                                      ORDER BY p.n DESC, p.scheme_code,
                                               cast(n.{N} AS DOUBLE) DESC) AS rn
            FROM src.{cfg['NAV_TABLE']} n
            JOIN prio p ON cast(n.{C} AS VARCHAR) = p.scheme_code
            WHERE n.{N} IS NOT NULL AND cast(n.{N} AS DOUBLE) > 0
        )
        SELECT fund_id, d, nav FROM joined WHERE rn = 1
    """)

    # A splice is only valid if NAV levels are continuous across the junction.
    # Flag violations loudly rather than letting them become fake returns.
    jumps = compute_splice_jumps(con)
    rows, funds = con.execute(
        "SELECT count(*), count(DISTINCT fund_id) FROM nav_fund").fetchone()
    return rows, funds, jumps


def compute_splice_jumps(con):
    con.execute(f"""
        CREATE OR REPLACE TABLE splice_jumps AS
        WITH r AS (
            SELECT fund_id, d, nav,
                   ln(nav / lag(nav) OVER (PARTITION BY fund_id ORDER BY d)) AS lr
            FROM nav_fund
        )
        SELECT fund_id, d, nav, lr FROM r
        WHERE lr IS NOT NULL AND abs(lr) > {JUMP_THRESHOLD}
        ORDER BY abs(lr) DESC
    """)
    return con.execute("SELECT count(*) FROM splice_jumps").fetchone()[0]


def windows(start_fy, end_fy):
    """(horizon, fy_start, start_date, end_date) for every FY window that fits."""
    out = []
    for h in HORIZONS:
        for fy in range(start_fy, end_fy - h + 1):
            out.append((h, fy,
                        dt.date(fy, FY_MONTH, FY_DAY),
                        dt.date(fy + h, FY_MONTH, FY_DAY) - dt.timedelta(days=1)))
    return out


def build_panel(con, wins):
    con.execute("""
        CREATE OR REPLACE TABLE returns_panel (
            fund_id VARCHAR, horizon_y INT, fy_start INT, phase INT,
            start_date DATE, end_date DATE,
            anchor_date DATE, anchor_nav DOUBLE, anchor_gap_days INT,
            last_date DATE, last_nav DOUBLE,
            n_obs INT, coverage_frac DOUBLE,
            window_return DOUBLE, cagr DOUBLE, ann_vol DOUBLE, max_drawdown DOUBLE,
            years_eff DOUBLE, survived BOOLEAN, max_abs_daily_lr DOUBLE
        )
    """)

    for i, (h, fy, sd, ed) in enumerate(wins, 1):
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE slice AS
            SELECT fund_id, d, nav,
                   ln(nav / lag(nav) OVER (PARTITION BY fund_id ORDER BY d)) AS lr,
                   max(nav) OVER (PARTITION BY fund_id ORDER BY d
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_max
            FROM nav_fund
            WHERE d > DATE '{sd}' AND d <= DATE '{ed}'
        """)

        con.execute(f"""
            INSERT INTO returns_panel
            WITH agg AS (
                SELECT fund_id,
                       count(*)                     AS n_obs,
                       max(d)                       AS last_date,
                       arg_max(nav, d)              AS last_nav,
                       stddev_samp(lr)              AS sd_lr,
                       max(abs(lr))                 AS max_abs_lr,
                       max(1.0 - nav / run_max)     AS mdd
                FROM slice GROUP BY fund_id
            ),
            anchored AS (
                SELECT a.*, n.d AS anchor_date, n.nav AS anchor_nav
                FROM (SELECT *, DATE '{sd}' AS sd FROM agg) a
                ASOF LEFT JOIN nav_fund n
                  ON a.fund_id = n.fund_id AND a.sd >= n.d
            ),
            denom AS (SELECT median(n_obs) AS m FROM agg),
            calc AS (
                SELECT x.*,
                       date_diff('day', x.anchor_date, DATE '{sd}')          AS gap,
                       date_diff('day', x.anchor_date, x.last_date) / 365.25 AS yrs,
                       x.last_nav / x.anchor_nav - 1.0                       AS ret
                FROM anchored x WHERE x.anchor_nav IS NOT NULL
            )
            SELECT
                c.fund_id, {h}, {fy}, {fy % h},
                DATE '{sd}', DATE '{ed}',
                c.anchor_date, c.anchor_nav, c.gap,
                c.last_date, c.last_nav,
                c.n_obs, LEAST(c.n_obs::DOUBLE / nullif(d.m, 0), 1.0),
                c.ret,
                CASE WHEN c.yrs > 0.5 AND 1.0 + c.ret > 0
                     THEN pow(1.0 + c.ret, 1.0 / c.yrs) - 1.0 END,
                c.sd_lr * sqrt({TRADING_DAYS}),
                c.mdd,
                c.yrs,
                c.last_date >= DATE '{ed}' - INTERVAL {END_TOL_DAYS} DAY,
                c.max_abs_lr
            FROM calc c CROSS JOIN denom d
            WHERE c.gap <= {ANCHOR_TOL_DAYS}
        """)
        n = con.execute("SELECT count(*) FROM returns_panel").fetchone()[0]
        print(f"  [{i:>2}/{len(wins)}] {h}y FY{fy}  {sd} -> {ed}   panel={n:,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="REGULAR", choices=["REGULAR", "DIRECT"])
    ap.add_argument("--categories", default=None,
                     help="Comma-separated category substrings (case-insensitive), "
                          "e.g. 'large cap'. Default: all categories.")
    ap.add_argument("--src", default=None, help="override CONFIG['SRC_DB']")
    ap.add_argument("--out", default="fundeval_analysis.duckdb")
    ap.add_argument("--start-fy", type=int, default=2006)
    ap.add_argument("--end-fy", type=int, default=None, help="last FY start; default = latest complete")
    ap.add_argument("--panel-only", action="store_true",
                     help="rebuild splice_jumps + returns_panel from the CURRENT nav_fund table "
                          "(e.g. after a correction script has overwritten it) -- "
                          "does NOT touch fund_map or re-derive nav_fund from source")
    args = ap.parse_args()

    cfg = dict(CONFIG)
    if args.src:
        cfg["SRC_DB"] = args.src

    con = duckdb.connect(args.out)

    if args.panel_only:
        existing = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'nav_fund'").fetchone()[0]
        if not existing:
            raise SystemExit("--panel-only requires an existing nav_fund table in --out. Run a full build first.")
        print(f"--panel-only: rebuilding splice_jumps + returns_panel from the CURRENT nav_fund "
              f"table in {args.out} (fund_map and nav_fund are left untouched)")
        jumps = compute_splice_jumps(con)
        print(f"splice_jumps: {jumps:,} days with |log move| > {JUMP_THRESHOLD} remaining")
    else:
        con.execute(f"ATTACH '{cfg['SRC_DB']}' AS src (READ_ONLY)")
        print(f"src: {cfg['SRC_DB']}  ->  out: {args.out}")
        print(f"plan={args.plan}  option=GROWTH  "
              f"plan_col={cfg['PLAN_COL']}  opt_col={cfg['OPT_COL']}"
              + ("   (both inferred from scheme_name)"
                 if not cfg["PLAN_COL"] and not cfg["OPT_COL"] else ""))

        categories = [c for c in args.categories.split(",")] if args.categories else None
        if categories:
            print(f"categories: restricted to substrings {categories}")

        codes, funds, no_isin = build_fund_map(con, cfg, args.plan, categories)
        print(f"\nfund_map: {codes:,} codes -> {funds:,} funds  ({no_isin:,} lacking ISIN)")

        rows, nfunds, jumps = build_nav_fund(con, cfg)
        print(f"nav_fund: {rows:,} spliced NAV rows across {nfunds:,} funds")
        print(f"splice_jumps: {jumps:,} days with |log move| > {JUMP_THRESHOLD} "
              f"-- inspect before trusting those funds")

    hi = con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    end_fy = args.end_fy or (hi.year if hi.month >= FY_MONTH else hi.year - 1)
    wins = windows(args.start_fy, end_fy)
    print(f"\nlatest NAV {hi}; FY{args.start_fy}..FY{end_fy}; {len(wins)} windows\n")

    build_panel(con, wins)

    print("\n--- panel by horizon ---")
    print(con.execute("""
        SELECT horizon_y, count(*) n_rows, count(DISTINCT fund_id) funds,
               count(DISTINCT fy_start) windows,
               round(avg(coverage_frac), 3) avg_cov,
               round(avg(survived::INT), 3) survived_rate
        FROM returns_panel GROUP BY 1 ORDER BY 1""").df().to_string(index=False))

    print("\n--- 1y funds per window (universe growth + attrition) ---")
    print(con.execute("""
        SELECT fy_start, count(*) funds, sum(survived::INT) survived,
               round(median(window_return), 4) med_ret,
               round(median(ann_vol), 4) med_vol
        FROM returns_panel WHERE horizon_y = 1
        GROUP BY 1 ORDER BY 1""").df().to_string(index=False))

    con.close()
    print(f"\nDone. Panel in {args.out}: returns_panel, fund_map, nav_fund, splice_jumps.")


if __name__ == "__main__":
    main()