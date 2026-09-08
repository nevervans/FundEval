"""
fund_lookup.py — one-pager fund query tool for FundEval

Given a scheme name (substring match) or an ISIN/fund_id, prints trailing
1y/3y/5y/10y returns (CAGR for >1y), category average, annualized
volatility, Sharpe ratio, max drawdown, and category rank — computed
LIVE from nav_fund via a rolling anchor date. See windows.py for the
anchor-matching, dead-fund, and risk-metric logic this depends on.

Usage:
    python3 fund_lookup.py "quant small cap"
    python3 fund_lookup.py --isin INF090I01817
    python3 fund_lookup.py "hdfc flexi cap" --asof 2025-03-31
"""

import argparse
import datetime as dt
import sys

import duckdb

from windows import (
    HORIZONS,
    ANCHOR_TOLERANCE_DAYS,
    STALE_TOLERANCE_DAYS,
    RISK_FREE_RATE,
    fund_window,
    category_snapshot,
    rank_among_peers,
    resolve_fund_query,
    format_pct,
    format_ratio,
)


def fund_plan(con, fund_id):
    row = con.execute(
        "SELECT plan, option_type FROM fund_map WHERE fund_id = ? LIMIT 1", [fund_id]
    ).fetchone()
    return f"{row[0]}/{row[1]}" if row else "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="scheme name substring, or ISIN with --isin")
    ap.add_argument("--isin", action="store_true")
    ap.add_argument("--db", default="fundeval_analysis.duckdb")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD; defaults to latest NAV date in the DB")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    asof = (
        dt.date.fromisoformat(args.asof) if args.asof
        else con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    )

    matches = resolve_fund_query(con, args.query, by_isin=args.isin)
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
    plan_label = fund_plan(con, fund_id)

    print("=" * 90)
    print(f"{scheme_name}")
    print(f"fund_id: {fund_id}   category: {category}   plan: {plan_label}")
    print(f"as of: {asof}")
    print("=" * 90)
    print(f"{'Horizon':<8}{'Return':>10}{'Cat. avg':>10}{'Vol (ann)':>11}{'Rank':>12}")

    death_year = None
    stale_peer_notes = []
    low_obs_notes = []
    risk_rows = []  # (label, sharpe, max_dd, cat_vol, cat_sharpe, sharpe_rank_str)

    for label, years in HORIZONS.items():
        stats = fund_window(con, fund_id, asof, years)

        if stats is None:
            print(f"{label:<8}{'insufficient history':>45}")
            continue

        if stats["is_dead"]:
            death_year = stats["death_year"]
            print(f"{label:<8}{'died ' + str(death_year):>45}")
            continue

        return_val = stats["window_return"] if years <= 1 else stats["cagr"]
        peers, meta = category_snapshot(con, category, asof, years)

        if meta["n_dead"] > 0:
            stale_peer_notes.append(
                f"{label}: {meta['n_dead']} of {meta['n_considered']} category peers "
                f"had died by this horizon (excluded from avg/rank, not folded in)"
            )
        if stats["low_obs"]:
            low_obs_notes.append(
                f"{label}: only {stats['n_obs']} return observations in this window -- vol unreliable"
            )

        if not peers.empty:
            n_peers = len(peers)
            peer_col = "window_return" if years <= 1 else "cagr"
            rank_num, _ = rank_among_peers(peers, peer_col, fund_id, return_val)
            rank_str = f"{rank_num}/{n_peers}"
            cat_avg = peers[peer_col].mean()
            cat_vol = peers["ann_vol"].mean()
            cat_sharpe = peers["sharpe"].mean()
            if stats["sharpe"] is not None:
                sharpe_rank_num, _ = rank_among_peers(peers, "sharpe", fund_id, stats["sharpe"])
                sharpe_rank_str = f"{sharpe_rank_num}/{n_peers}"
            else:
                sharpe_rank_str = "n/a"
        else:
            rank_str = "n/a"
            cat_avg = None
            cat_vol = None
            cat_sharpe = None
            sharpe_rank_str = "n/a"

        vol_str = f"{stats['ann_vol'] * 100:.2f}%" if stats["ann_vol"] is not None else "n/a"

        print(
            f"{label:<8}"
            f"{format_pct(return_val):>10}"
            f"{format_pct(cat_avg):>10}"
            f"{vol_str:>11}"
            f"{rank_str:>12}"
        )

        risk_rows.append((label, stats["sharpe"], stats["max_dd"], cat_vol, cat_sharpe, sharpe_rank_str))

    if risk_rows:
        print()
        print(f"{'Horizon':<8}{'Sharpe':>9}{'Cat.Sharpe':>12}{'Max DD':>10}{'Cat.Vol':>10}{'SharpeRk':>12}")
        for label, sharpe, max_dd, cat_vol, cat_sharpe, sharpe_rank_str in risk_rows:
            max_dd_str = format_pct(max_dd) if max_dd is not None else "n/a"
            cat_vol_str = f"{cat_vol * 100:.2f}%" if cat_vol is not None else "n/a"
            print(
                f"{label:<8}"
                f"{format_ratio(sharpe):>9}"
                f"{format_ratio(cat_sharpe):>12}"
                f"{max_dd_str:>10}"
                f"{cat_vol_str:>10}"
                f"{sharpe_rank_str:>12}"
            )

    print("=" * 90)
    print("Rank = ordinal position in category by trailing return (CAGR for >1y), best=1.")
    print(f"Sharpe = (CAGR - {RISK_FREE_RATE * 100:.1f}%) / annualized vol -- {RISK_FREE_RATE * 100:.1f}% is a")
    print("static placeholder risk-free rate, not a real historical bond series.")
    print("Max DD = worst peak-to-trough NAV decline within this window.")
    print(f"Anchor must land within {ANCHOR_TOLERANCE_DAYS} days of the exact target date")
    print("(asof - N years), either side, or the horizon is insufficient history.")
    print(f"'died <year>' = no NAV filed within {STALE_TOLERANCE_DAYS} days of as-of --")
    print("fund likely matured, merged, or stopped reporting. Excluded from category")
    print("avg/rank at every horizon.")

    if death_year:
        print(f"\nThis fund died in {death_year} as of the current as-of date.")

    for note in stale_peer_notes:
        print(f"  ({note})")
    for note in low_obs_notes:
        print(f"  ({note})")

    con.close()


if __name__ == "__main__":
    main()
