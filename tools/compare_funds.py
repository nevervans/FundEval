"""
compare_funds.py — multi-fund comparison generator for FundEval
Same computation as fund_lookup.py's one-pager (live trailing windows from
nav_fund, not returns_panel), applied to N funds side by side.
"""

import argparse
import datetime as dt
import sys

import duckdb
import pandas as pd

from fund_lookup import HORIZONS, STALE_TOLERANCE_DAYS, fund_window, category_snapshot, format_pct


def resolve_query(con, query):
    exact = con.execute(
        "SELECT DISTINCT fund_id, scheme_name, category FROM fund_map WHERE fund_id = ?",
        [query],
    ).fetchall()
    if exact:
        return exact
    return con.execute(
        """
        SELECT DISTINCT fund_id, scheme_name, category
        FROM fund_map
        WHERE lower(scheme_name) LIKE '%' || lower(?) || '%'
        ORDER BY length(scheme_name)
        """,
        [query],
    ).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--db", default="fundeval_analysis.duckdb")
    ap.add_argument("--asof", default=None)
    args = ap.parse_args()

    if len(args.queries) < 2:
        print("Give at least 2 funds to compare.")
        sys.exit(1)

    con = duckdb.connect(args.db, read_only=True)
    asof = (
        dt.date.fromisoformat(args.asof)
        if args.asof
        else con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    )

    funds = []
    for q in args.queries:
        matches = resolve_query(con, q)
        if not matches:
            print(f"No fund matched '{q}'.")
            sys.exit(1)
        if len(matches) > 1:
            print(f"{len(matches)} funds matched '{q}' -- narrow with an exact fund_id/ISIN:\n")
            for fid, name, cat in matches[:15]:
                print(f"  {fid:<20} {name}  [{cat}]")
            sys.exit(0)
        funds.append(matches[0])

    print("=" * 100)
    print(f"as of: {asof}")
    for i, (fid, name, cat) in enumerate(funds, 1):
        print(f"  [{i}] {name}")
        print(f"      fund_id: {fid}   category: {cat}")
    print("=" * 100)

    labels = [f"[{i}]" for i in range(1, len(funds) + 1)]
    returns_row = {h: [] for h in HORIZONS}
    catavg_row = {h: [] for h in HORIZONS}
    vol_row = {h: [] for h in HORIZONS}
    rank_row = {h: [] for h in HORIZONS}

    for fid, name, cat in funds:
        for label, years in HORIZONS.items():
            stats = fund_window(con, fid, asof, years)
            peers = category_snapshot(con, cat, asof, years) if cat else pd.DataFrame()

            if stats is None:
                returns_row[label].append("insuff.")
                catavg_row[label].append("n/a")
                vol_row[label].append("n/a")
                rank_row[label].append("n/a")
                continue

            marker = "\u2020" if stats["is_stale"] else ""
            returns_row[label].append(format_pct(stats["window_return"]) + marker)
            vol_row[label].append(f"{stats['ann_vol'] * 100:.2f}%")

            if not peers.empty:
                cat_avg = peers["window_return"].mean()
                n_peers = len(peers)
                better = (peers["window_return"] < stats["window_return"]).sum()
                rank_row[label].append(f"{n_peers - better}/{n_peers}")
                catavg_row[label].append(format_pct(cat_avg))
            else:
                catavg_row[label].append("n/a")
                rank_row[label].append("n/a")

    def print_table(title, data):
        print(f"\n{title}")
        df = pd.DataFrame(data, index=labels).T
        print(df.to_string())

    print_table("Trailing Return", returns_row)
    print_table("Category Avg Return", catavg_row)
    print_table("Annualized Volatility", vol_row)
    print_table("Rank in Category", rank_row)

    print("\n" + "=" * 100)
    print(f"\u2020 = last NAV on file is >{STALE_TOLERANCE_DAYS}d before as-of.")


if __name__ == "__main__":
    main()