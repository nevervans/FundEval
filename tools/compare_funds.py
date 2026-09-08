"""
compare_funds.py — multi-fund comparison generator for FundEval
Same computation as fund_lookup.py's one-pager (via windows.py), applied
to N funds side by side, plus a correlation/portfolio section that
treats the funds as an actual allocation rather than a list.

Usage:
    python3 compare_funds.py "sbi large cap" "hdfc mid cap" "sbi gilt" "quant small cap"
    python3 compare_funds.py INF200K01180 INF179K01CR2 --weights 60,40
"""

import argparse
import datetime as dt
import sys

import duckdb
import pandas as pd

from windows import (
    HORIZONS,
    STALE_TOLERANCE_DAYS,
    fund_window,
    category_snapshot,
    fund_date_range,
    portfolio_metrics,
    format_pct,
    format_ratio,
)


def resolve_query(con, query):
    """Same dedup as fund_lookup.resolve_fund -- see that docstring for
    why (fund_map multi-row-per-fund_id, confirmed on INF251K01894)."""
    dedup_sql = """
        WITH matched AS (
            SELECT fund_id, scheme_name, category,
                   row_number() OVER (
                       PARTITION BY fund_id ORDER BY scheme_code DESC
                   ) AS rn
            FROM fund_map
            WHERE {where}
        )
        SELECT fund_id, scheme_name, category
        FROM matched
        WHERE rn = 1
        ORDER BY length(scheme_name)
    """
    exact = con.execute(dedup_sql.format(where="fund_id = ?"), [query]).fetchall()
    if exact:
        return exact
    return con.execute(
        dedup_sql.format(where="lower(scheme_name) LIKE '%' || lower(?) || '%'"), [query]
    ).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--db", default="fundeval_analysis.duckdb")
    ap.add_argument("--asof", default=None)
    ap.add_argument(
        "--weights",
        default=None,
        help="comma-separated weights matching fund order, e.g. 40,25,15,20 "
        "(percent or fraction, auto-normalized). Defaults to equal weight.",
    )
    args = ap.parse_args()

    if len(args.queries) < 2:
        print("Give at least 2 funds to compare.")
        sys.exit(1)

    con = duckdb.connect(args.db, read_only=True)
    asof = (
        dt.date.fromisoformat(args.asof) if args.asof
        else con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    )

    funds = []
    for q in args.queries:
        matches = resolve_query(con, q)
        if (not matches or len(matches) > 1) and funds:
            print("Already matched:")
            for i, (fid, name, cat) in enumerate(funds, 1):
                print(f"  [{i}] {name}  ({fid})")
            print()
        if not matches:
            print(f"No fund matched '{q}'.")
            sys.exit(1)
        if len(matches) > 1:
            print(f"{len(matches)} funds matched '{q}' -- narrow with an exact fund_id/ISIN:\n")
            for fid, name, cat in matches[:15]:
                print(f"  {fid:<20} {name}  [{cat}]")
            sys.exit(0)
        funds.append(matches[0])

    if args.weights:
        raw = [float(x) for x in args.weights.split(",")]
        if len(raw) != len(funds):
            print(f"--weights needs exactly {len(funds)} values, got {len(raw)}.")
            sys.exit(1)
        if any(x < 0 for x in raw) or sum(raw) <= 0:
            print("--weights must be non-negative and sum to more than zero.")
            sys.exit(1)
        total = sum(raw)
        weight_list = [x / total for x in raw]
    else:
        weight_list = [1 / len(funds)] * len(funds)

    print("=" * 100)
    print(f"as of: {asof}")
    for i, (fid, name, cat) in enumerate(funds, 1):
        print(f"  [{i}] {name}")
        print(f"      fund_id: {fid}   category: {cat}")
    print("=" * 100)

    labels = [f"[{i}]" for i in range(1, len(funds) + 1)]
    return_row = {h: [] for h in HORIZONS}
    catavg_row = {h: [] for h in HORIZONS}
    vol_row = {h: [] for h in HORIZONS}
    rank_row = {h: [] for h in HORIZONS}
    sharpe_row = {h: [] for h in HORIZONS}
    maxdd_row = {h: [] for h in HORIZONS}
    death_notes = []
    stale_peer_notes = {h: set() for h in HORIZONS}

    for idx, (fid, name, cat) in enumerate(funds, 1):
        for label, years in HORIZONS.items():
            stats = fund_window(con, fid, asof, years)

            if stats is None:
                return_row[label].append("insuff.")
                catavg_row[label].append("n/a")
                vol_row[label].append("n/a")
                rank_row[label].append("n/a")
                sharpe_row[label].append("n/a")
                maxdd_row[label].append("n/a")
                continue

            if stats["is_dead"]:
                return_row[label].append(f"died {stats['death_year']}")
                catavg_row[label].append("n/a")
                vol_row[label].append("n/a")
                rank_row[label].append("n/a")
                sharpe_row[label].append("n/a")
                maxdd_row[label].append("n/a")
                death_notes.append(f"[{idx}] {name}: died {stats['death_year']}")
                continue

            return_val = stats["window_return"] if years <= 1 else stats["cagr"]
            return_row[label].append(format_pct(return_val))
            vol_row[label].append(
                f"{stats['ann_vol'] * 100:.2f}%" if stats["ann_vol"] is not None else "n/a"
            )
            sharpe_row[label].append(format_ratio(stats["sharpe"]))
            maxdd_row[label].append(format_pct(stats["max_dd"]) if stats["max_dd"] is not None else "n/a")

            if cat:
                peers, meta = category_snapshot(con, cat, asof, years)
            else:
                peers, meta = pd.DataFrame(), {"n_dead": 0, "n_considered": 0}

            if meta["n_dead"] > 0:
                stale_peer_notes[label].add(
                    f"{cat}: {meta['n_dead']} of {meta['n_considered']} peers died by this horizon"
                )

            if not peers.empty:
                peer_returns = peers["window_return"] if years <= 1 else peers["cagr"]
                n_peers = len(peers)
                worse = (peer_returns < return_val).sum()
                rank_row[label].append(f"{n_peers - worse}/{n_peers}")
                catavg_row[label].append(format_pct(peer_returns.mean()))
            else:
                catavg_row[label].append("n/a")
                rank_row[label].append("n/a")

    def print_table(title, data):
        print(f"\n{title}")
        df = pd.DataFrame(data, index=labels).T
        print(df.to_string())

    print_table("Trailing Return (CAGR for >1y)", return_row)
    print_table("Category Avg Return (CAGR for >1y)", catavg_row)
    print_table("Annualized Volatility", vol_row)
    print_table("Rank in Category", rank_row)
    print_table("Sharpe Ratio (vs static risk-free placeholder)", sharpe_row)
    print_table("Max Drawdown (within window)", maxdd_row)

    any_stale = any(stale_peer_notes.values())
    if any_stale or death_notes:
        print("\n" + "=" * 100)
        print("'died <year>' funds are excluded from category avg/rank at every horizon.")
        for label in HORIZONS:
            for note in sorted(stale_peer_notes[label]):
                print(f"  {label}: {note} (excluded from avg/rank, not folded in)")
        for note in death_notes:
            print(f"  {note}")

    # --- Correlation & Portfolio -------------------------------------
    fund_ids = [fid for fid, _, _ in funds]
    names_by_id = {fid: name for fid, name, _ in funds}
    weights = dict(zip(fund_ids, weight_list))

    print("\n" + "=" * 100)
    print("Correlation & Portfolio")
    weight_str = ", ".join(f"{labels[i]} {weight_list[i] * 100:.1f}%" for i in range(len(funds)))
    if args.weights:
        print(f"Weights: {weight_str}")
    else:
        print(f"Weights: {weight_str}  (equal weight -- pass --weights to set your own, e.g. --weights 40,25,15,20)")

    ranges = {fid: fund_date_range(con, fid) for fid in fund_ids}
    dead_funds = [
        fid for fid, (lo, hi) in ranges.items()
        if hi is None or (asof - hi).days > STALE_TOLERANCE_DAYS
    ]

    if dead_funds:
        dead_list = ", ".join(names_by_id[f] for f in dead_funds)
        print(f"Skipped -- {dead_list} has no recent NAV (appears dead).")
        print("A fund that's stopped trading can't sensibly sit in a forward-looking mix.")
    else:
        common_start = max(lo for lo, hi in ranges.values())
        common_end = min(hi for lo, hi in ranges.values())
        if common_start >= common_end:
            print("Skipped -- no overlapping NAV history across all selected funds.")
        else:
            result = portfolio_metrics(con, fund_ids, weights, common_start, common_end)
            if result is None:
                print(
                    f"Skipped -- only overlapping window is {common_start} to {common_end}, "
                    "too short to compute a reliable correlation/vol figure."
                )
            else:
                print(
                    f"Common window: {result['start']} to {result['end']}  "
                    f"({result['n_obs']} shared trading days)"
                )
                print("\nPairwise correlation (daily log returns):")
                corr_display = result["corr"].copy()
                corr_display.index = labels
                corr_display.columns = labels
                print(corr_display.round(2).to_string())

                print(f"\nWeighted-avg vol (no diversification): {result['weighted_avg_vol'] * 100:.2f}%")
                print(f"Actual portfolio vol:                  {result['portfolio_vol'] * 100:.2f}%")
                print(f"Diversification ratio (higher = more benefit): {result['diversification_ratio']:.2f}")
                print(f"\nPortfolio CAGR:   {format_pct(result['portfolio_cagr'])}")
                print(f"Portfolio Sharpe: {format_ratio(result['portfolio_sharpe'])}")
                print(f"Portfolio Max DD: {format_pct(result['portfolio_max_dd'])}")
                print(
                    "\n(Assumes daily rebalancing to these fixed weights -- a real "
                    "buy-and-hold allocation's weights drift over time and will show "
                    "somewhat different vol/drawdown, more so the longer the window.)"
                )


if __name__ == "__main__":
    main()