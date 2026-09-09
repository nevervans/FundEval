#!/usr/bin/env python3
"""
analysis/build_category_map.py -- fixes the category-taxonomy split bug where
the exact same fund category exists under multiple spellings in fund_map
(most commonly "Equity Scheme - X Fund" vs "Equity Schemes - X Fund"), which
silently splits one real peer group into two for every category_snapshot()
call in windows.py -- undercounting peers and computing wrong ranks/averages.
This is what caused Axis Large Cap (9 peers) and Bajaj Finserv Large Cap
(24 peers) to see different-sized "Large Cap" peer sets despite being the
same category.

Builds a category_map(raw_category, canonical_category) table: every
distinct category in fund_map gets exactly one row. Categories that differ
ONLY by the Scheme/Schemes plural and/or a trailing "Fund" word are merged
onto one canonical spelling automatically -- a purely mechanical rule
(byte-identical after normalization), not a fuzzy match, so it cannot
accidentally merge genuinely different fund types.

Deeper taxonomy questions are deliberately NOT auto-merged here -- e.g.
"Debt Scheme - X" vs the pre-2017 "Income/Debt Oriented Schemes - X"
naming, or the one-to-many Sectoral/Thematic split. Those need a human
decision on whether SEBI's category *definitions* actually changed across
that boundary, not just the label. Those categories keep their own raw
name as their own canonical name until reviewed and added explicitly.

This only creates/replaces the category_map table -- it does not touch
fund_map, nav_fund, or returns_panel, and is safe to re-run any time.

Usage:
    python3 analysis/build_category_map.py --out fundeval_analysis.duckdb
    python3 analysis/build_category_map.py --out fundeval_analysis_direct.duckdb
"""
import argparse
import re
from collections import defaultdict

import duckdb


def normalize(cat):
    x = cat.lower().strip()
    x = x.replace("schemes", "scheme")   # plural -> singular
    x = re.sub(r"\bfund\b", " ", x)      # drop standalone word "fund"
    x = re.sub(r"[^a-z0-9]+", " ", x)    # collapse all punctuation to space
    return re.sub(r"\s+", " ", x).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Database to build/update category_map in")
    args = ap.parse_args()

    con = duckdb.connect(args.out)
    cats = [r[0] for r in con.execute(
        "SELECT DISTINCT category FROM fund_map WHERE category IS NOT NULL").fetchall()]

    groups = defaultdict(list)
    for c in cats:
        groups[normalize(c)].append(c)

    rows = []
    merges = []
    for key, members in groups.items():
        if len(members) == 1:
            rows.append((members[0], members[0]))
            continue
        # Prefer the singular "Scheme" spelling as canonical -- matches the
        # more common AMFI convention in this data; falls back to the
        # shortest string if that's somehow ambiguous.
        singular = [m for m in members if "schemes" not in m.lower()]
        canonical = singular[0] if len(singular) == 1 else sorted(members, key=len)[0]
        for m in members:
            rows.append((m, canonical))
        merges.append((canonical, [m for m in members if m != canonical]))

    con.execute("CREATE OR REPLACE TABLE category_map "
                "(raw_category VARCHAR PRIMARY KEY, canonical_category VARCHAR)")
    con.executemany("INSERT INTO category_map VALUES (?, ?)", rows)

    print(f"{args.out}: {len(cats)} distinct categories -> {len(groups)} canonical buckets\n")
    print(f"{len(merges)} auto-merge group(s) applied "
          f"(pure Scheme/Schemes + 'Fund'-suffix normalization only):\n")
    for canonical, absorbed in sorted(merges):
        print(f"  canonical: {canonical}")
        for m in absorbed:
            print(f"    <- {m}")

    untouched = len(groups) - len(merges)
    print(f"\n{untouched} categories left as their own canonical bucket -- includes both "
          f"genuinely distinct categories and pairs still needing a human taxonomy "
          f"decision (e.g. 'Debt Scheme - X' vs the pre-2017 'Income/Debt Oriented "
          f"Schemes - X' naming). Not touched by this script.")

    con.close()


if __name__ == "__main__":
    main()