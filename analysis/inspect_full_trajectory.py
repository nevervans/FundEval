#!/usr/bin/env python3
"""
Pull the FULL trajectory for a fund_id between two dates -- not just a fixed
context window -- to check whether a flagged anomaly is confined to a single
row or spans a longer corrupted stretch.

    python3 inspect_full_trajectory.py --out fundeval_analysis.duckdb \
        --fund-id INF789F01EL7 --from 2009-04-01 --to 2010-08-01
"""
import argparse
import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="fundeval_analysis.duckdb")
ap.add_argument("--fund-id", required=True)
ap.add_argument("--from", dest="date_from", required=True)
ap.add_argument("--to", dest="date_to", required=True)
args = ap.parse_args()

con = duckdb.connect(args.out, read_only=True)
df = con.execute(f"""
    SELECT d, nav, ln(nav / lag(nav) OVER (ORDER BY d)) AS lr
    FROM nav_fund
    WHERE fund_id = '{args.fund_id}' AND d BETWEEN DATE '{args.date_from}' AND DATE '{args.date_to}'
    ORDER BY d
""").df()
import pandas as pd
pd.set_option("display.max_rows", None)
print(df.to_string(index=False))
print(f"\n{len(df)} rows")
