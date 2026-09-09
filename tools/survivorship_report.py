"""
survivorship_report.py — quantifies FundEval's founding premise directly:
what fraction of funds active N years ago no longer exist today, per
category.

Usage:
    python3 survivorship_report.py --years 10
    python3 survivorship_report.py --years 5 --asof 2024-01-01
    python3 survivorship_report.py --years 10 --min-considered 10
"""

import argparse
import datetime as dt

import duckdb

from windows import survivorship_report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="fundeval_analysis.duckdb")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--years", type=float, default=10)
    ap.add_argument(
        "--min-considered", type=int, default=5,
        help="hide categories with fewer than this many funds considered (default 5)",
    )
    ap.add_argument(
        "--include-closed-end", action="store_true",
        help="include funds matching common closed-end/fixed-maturity naming patterns "
        "(FMP, FTP, Fixed Horizon, Capital Protection, etc) -- excluded by default, "
        "since their scheduled maturity isn't the same phenomenon as organic fund closure",
    )
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    asof = (
        dt.date.fromisoformat(args.asof) if args.asof
        else con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
    )

    df = survivorship_report(con, asof, args.years, exclude_closed_end=not args.include_closed_end)
    shown = df[df["n_considered"] >= args.min_considered]
    hidden_count = len(df) - len(shown)

    print("=" * 90)
    print(f"Survivorship report -- {args.years:g}-year horizon, as of {asof}")
    print("=" * 90)
    print(shown.to_string(index=False))
    print("=" * 90)
    print("death_rate_pct = share of funds that existed ~this many years ago and no longer")
    print(f"report a NAV within {15} days of as-of (matured, merged, or stopped reporting).")
    if hidden_count:
        print(
            f"{hidden_count} categories with fewer than {args.min_considered} funds "
            "considered are hidden -- use --min-considered to change this."
        )

    con.close()


if __name__ == "__main__":
    main()