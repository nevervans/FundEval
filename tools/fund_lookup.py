"""
fund_lookup.py — one-pager fund query tool for FundEval

Given a scheme name (substring match) or an ISIN/fund_id, prints trailing
1y/3y/5y/10y returns, the category average for the same window, annualized
volatility, and category rank -- computed LIVE from nav_fund via a rolling
anchor date (asof - horizon), the same ASOF-JOIN idiom returns_panel.py
uses for its own FY-aligned windows.

This is deliberately NOT built on top of returns_panel: that table's windows
are fixed fiscal-year boundaries for the persistence study's formation/
holding pairing, not "trailing N years as of today." Reusing it here would
either silently misdate results or require reshaping it -- easier and more
honest to compute rolling windows directly.

Usage:
    python3 fund_lookup.py "quant small cap"
    python3 fund_lookup.py --isin INF090I01817
    python3 fund_lookup.py "hdfc flexi cap" --asof 2025-03-31
    python3 fund_lookup.py "parag parikh" --db fundeval_analysis.duckdb

Notes:
    - fund_map is single-plan (whichever --plan was used to build the
      current fundeval_analysis.duckdb). The header prints which plan/option
      that is, pulled from the data itself, not assumed.
    - A horizon shows "insufficient history" if the closest available NAV
      to the target anchor date is more than ANCHOR_TOLERANCE_DAYS away --
      e.g. the fund didn't exist that far back. This avoids silently
      returning a return computed against the wrong start date.
"""

import argparse
import datetime as dt
import sys

import duckdb
import pandas as pd

HORIZONS = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}
ANCHOR_TOLERANCE_DAYS = 45
STALE_TOLERANCE_DAYS = 15  # matches returns_panel.py's END_TOL_DAYS "survived" convention
TRADING_DAYS_PER_YEAR = 252


def resolve_fund(con: duckdb.DuckDBPyConnection, query: str, by_isin: bool):
    """Return matching (fund_id, scheme_name, category) rows."""
    if by_isin:
        sql = """
            SELECT DISTINCT fund_id, scheme_name, category
            FROM fund_map WHERE fund_id = ?
        """
        params = [query]
    else:
        sql = """
            SELECT DISTINCT fund_id, scheme_name, category
            FROM fund_map
            WHERE lower(scheme_name) LIKE '%' || lower(?) || '%'
            ORDER BY length(scheme_name)
        """
        params = [query]
    return con.execute(sql, params).fetchall()


def fund_window(con: duckdb.DuckDBPyConnection, fund_id: str, asof: dt.date, years: float):
    """Trailing return + annualized vol for ONE fund over one horizon,
    anchored at (asof - years), as-of the latest NAV <= asof."""
    anchor_target = asof - dt.timedelta(days=round(years * 365.25))

    row = con.execute(
        """
        WITH last_pt AS (
            SELECT nav AS last_nav, d AS last_date
            FROM nav_fund WHERE fund_id = ? AND d <= ?
            ORDER BY d DESC LIMIT 1
        ),
        anchor_pt AS (
            SELECT nav AS anchor_nav, d AS anchor_date
            FROM nav_fund WHERE fund_id = ? AND d <= ?
            ORDER BY d DESC LIMIT 1
        )
        SELECT last_nav, last_date, anchor_nav, anchor_date
        FROM last_pt, anchor_pt
        """,
        [fund_id, asof, fund_id, anchor_target],
    ).fetchone()

    if row is None or row[2] is None:
        return None

    last_nav, last_date, anchor_nav, anchor_date = row
    gap_days = abs((anchor_date - anchor_target).days)
    if gap_days > ANCHOR_TOLERANCE_DAYS:
        return None  # not enough history for this horizon

    vol_row = con.execute(
        """
        WITH slice AS (
            SELECT d, ln(nav / lag(nav) OVER (ORDER BY d)) AS lr
            FROM nav_fund
            WHERE fund_id = ? AND d > ? AND d <= ?
        )
        SELECT stddev_samp(lr) * sqrt(?), count(lr) FROM slice
        """,
        [fund_id, anchor_date, last_date, TRADING_DAYS_PER_YEAR],
    ).fetchone()
    ann_vol, n_obs = vol_row

    stale_days = (asof - last_date).days
    return {
        "window_return": last_nav / anchor_nav - 1,
        "ann_vol": ann_vol,
        "n_obs": n_obs,
        "anchor_date": anchor_date,
        "last_date": last_date,
        "gap_days": gap_days,
        "stale_days": stale_days,
        "is_stale": stale_days > STALE_TOLERANCE_DAYS,
    }


def category_snapshot(con: duckdb.DuckDBPyConnection, category: str, asof: dt.date, years: float) -> pd.DataFrame:
    """Trailing return for every fund in `category`, in ONE set-based query
    (ASOF join), not a per-fund loop -- this is what makes category average
    and rank cheap even across a category with hundreds of funds.

    DuckDB's ASOF JOIN needs a genuine column-to-column inequality; a bound
    parameter on the right of `<=` doesn't qualify (raises "Missing ASOF
    JOIN inequality"). Peers are cross-joined against a one-row params CTE
    so each peer carries its own asof_date/anchor_target column instead."""
    anchor_target = asof - dt.timedelta(days=round(years * 365.25))

    df = con.execute(
        """
        WITH params AS (
            SELECT CAST(? AS DATE) AS asof_date, CAST(? AS DATE) AS anchor_target
        ),
        peers AS (
            SELECT fund_id FROM fund_map WHERE category = ?
        ),
        peers_p AS (
            SELECT p.fund_id, pr.asof_date, pr.anchor_target
            FROM peers p CROSS JOIN params pr
        ),
        last_pt AS (
            SELECT pp.fund_id, pp.asof_date, n.d AS last_date, n.nav AS last_nav
            FROM peers_p pp
            ASOF LEFT JOIN nav_fund n ON pp.fund_id = n.fund_id AND n.d <= pp.asof_date
        ),
        anchor_pt AS (
            SELECT pp.fund_id, n.d AS anchor_date, n.nav AS anchor_nav
            FROM peers_p pp
            ASOF LEFT JOIN nav_fund n ON pp.fund_id = n.fund_id AND n.d <= pp.anchor_target
        )
        SELECT l.fund_id, l.last_nav / a.anchor_nav - 1 AS window_return,
               date_diff('day', l.last_date, l.asof_date) AS stale_days
        FROM last_pt l JOIN anchor_pt a USING (fund_id)
        WHERE l.last_nav IS NOT NULL AND a.anchor_nav IS NOT NULL
          AND abs(date_diff('day', a.anchor_date, CAST(? AS DATE))) <= ?
        """,
        [asof, anchor_target, category, anchor_target, ANCHOR_TOLERANCE_DAYS],
    ).df()
    return df


def format_pct(x):
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="scheme name substring, or ISIN with --isin")
    ap.add_argument("--isin", action="store_true", help="treat query as an exact ISIN/fund_id")
    ap.add_argument("--db", default="fundeval_analysis.duckdb")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD; defaults to latest NAV date in the DB")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)

    plan_row = con.execute("SELECT DISTINCT plan, option_type FROM fund_map").fetchall()
    plan_label = ", ".join(f"{p}/{o}" for p, o in plan_row) if plan_row else "unknown"

    asof = (
        dt.date.fromisoformat(args.asof)
        if args.asof
        else con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    )

    matches = resolve_fund(con, args.query, args.isin)
    if not matches:
        print(f"No fund matched '{args.query}'.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"{len(matches)} funds matched '{args.query}' -- narrow with --isin:\n")
        for fund_id, name, cat in matches[:15]:
            print(f"  {fund_id:<20} {name}  [{cat}]")
        if len(matches) > 15:
            print(f"  ... and {len(matches) - 15} more")
        sys.exit(0)

    fund_id, scheme_name, category = matches[0]

    print("=" * 78)
    print(f"{scheme_name}")
    print(f"fund_id: {fund_id}   category: {category}   plan: {plan_label}")
    print(f"as of: {asof}")
    print("=" * 78)
    print(f"{'Horizon':<8}{'Return':>10}{'Cat. avg':>10}{'Vol (ann)':>12}{'Rank':>14}")

    fund_last_date = None
    fund_stale_days = None
    stale_peer_notes = []

    for label, years in HORIZONS.items():
        fund_stats = fund_window(con, fund_id, asof, years)
        peers = category_snapshot(con, category, asof, years)

        if fund_stats is None:
            print(f"{label:<8}{'insufficient history':>46}")
            continue

        if fund_last_date is None:
            fund_last_date = fund_stats["last_date"]
            fund_stale_days = fund_stats["stale_days"]

        cat_avg = peers["window_return"].mean() if not peers.empty else None
        n_peers = len(peers)
        n_stale_peers = int((peers["stale_days"] > STALE_TOLERANCE_DAYS).sum()) if n_peers > 0 else 0
        if n_stale_peers > 0:
            stale_peer_notes.append(
                f"{label}: {n_stale_peers}/{n_peers} category peers stale "
                f"(kept in avg/rank, not dropped)"
            )

        if n_peers > 0:
            better = (peers["window_return"] < fund_stats["window_return"]).sum()
            rank_str = f"{n_peers - better}/{n_peers}"
        else:
            rank_str = "n/a"

        marker = "\u2020" if fund_stats["is_stale"] else ""
        return_str = format_pct(fund_stats["window_return"]) + marker

        print(
            f"{label:<8}"
            f"{return_str:>10}"
            f"{format_pct(cat_avg):>10}"
            f"{fund_stats['ann_vol'] * 100:>11.2f}%"
            f"{rank_str:>14}"
        )

    print("=" * 78)
    print("Rank = ordinal position in category by trailing return, best=1.")
    print(f"A horizon's anchor must land within {ANCHOR_TOLERANCE_DAYS} days of the exact")
    print("target date (asof - N years) or it's marked insufficient history.")

    if fund_stale_days is not None and fund_stale_days > STALE_TOLERANCE_DAYS:
        print(f"\u2020 last NAV on file is {fund_last_date} ({fund_stale_days}d before as-of) --")
        print("  fund may have matured, merged, or stopped filing. Return is vs. its last known NAV.")

    for note in stale_peer_notes:
        print(f"  ({note})")

    con.close()


if __name__ == "__main__":
    main()
