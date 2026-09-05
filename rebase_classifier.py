#!/usr/bin/env python3
"""
FundEval L1 -- rebase/error classifier for splice_jumps.

Two real patterns showed up in the first 222 flagged jumps:
  REBASE : NAV moves to a new level by a large (often round) multiplier and
           STAYS there -- a genuine face-value consolidation (e.g. par value
           Rs 10 -> Rs 1000). Seen across unrelated AMCs and dates,
           concentrated in debt/liquid/ultra-short schemes. Real information;
           needs a continuity adjustment, not deletion.
  REVERT : NAV spikes for a short stretch then returns close to the pre-jump
           level. Not a corporate action -- a data error for that stretch.
           Needs exclusion, not adjustment.

A single day's ratio can't tell these apart -- both look identical at the
moment of the jump. This classifies using what the series does afterward.

    python3 rebase_classifier.py --out fundeval_analysis.duckdb
"""

import argparse

import duckdb
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fundeval_analysis.duckdb")
    ap.add_argument("--pre-n", type=int, default=5,
                     help="obs before the jump used as the pre-jump baseline")
    ap.add_argument("--post-skip", type=int, default=15,
                     help="obs to skip right after the jump before measuring the settled level")
    ap.add_argument("--post-n", type=int, default=25,
                     help="obs used to measure the settled post-jump level")
    ap.add_argument("--rebase-tol", type=float, default=0.25,
                     help="relative tolerance for 'settled level is still at the new level'")
    ap.add_argument("--revert-tol", type=float, default=0.25,
                     help="relative tolerance for 'settled level is back near the old level'")
    ap.add_argument("--csv-out", default="jump_classification.csv")
    args = ap.parse_args()

    con = duckdb.connect(args.out, read_only=True)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE numbered AS
        SELECT fund_id, d, nav, row_number() OVER (PARTITION BY fund_id ORDER BY d) AS rn
        FROM nav_fund
    """)

    jumps = con.execute("SELECT fund_id, d, lr FROM splice_jumps ORDER BY fund_id, d").df()
    print(f"classifying {len(jumps)} flagged jumps...")

    rows = []
    for _, j in jumps.iterrows():
        fid, jd, lr = j["fund_id"], j["d"], j["lr"]
        rn_row = con.execute(
            "SELECT rn FROM numbered WHERE fund_id = ? AND d = ?", [fid, jd]).fetchone()
        if not rn_row:
            continue
        rn = rn_row[0]

        pre = con.execute(
            "SELECT median(nav) FROM numbered WHERE fund_id = ? AND rn BETWEEN ? AND ?",
            [fid, max(rn - args.pre_n, 1), rn - 1]).fetchone()[0]
        post_immediate = con.execute(
            "SELECT nav FROM numbered WHERE fund_id = ? AND rn = ?", [fid, rn]).fetchone()[0]
        settled, n_settled = con.execute(
            "SELECT median(nav), count(*) FROM numbered WHERE fund_id = ? AND rn BETWEEN ? AND ?",
            [fid, rn + args.post_skip, rn + args.post_skip + args.post_n]).fetchone()

        if pre is None or pre == 0:
            rows.append((fid, jd, lr, None, None, 0, "NO_BASELINE"))
            continue
        ratio_immediate = post_immediate / pre

        if settled is None or (n_settled or 0) < max(5, args.post_n // 3):
            rows.append((fid, jd, lr, ratio_immediate, None, n_settled or 0,
                        "INSUFFICIENT_FORWARD_DATA"))
            continue

        ratio_settled = settled / pre
        if abs(ratio_settled - ratio_immediate) / max(abs(ratio_immediate), 1e-9) < args.rebase_tol:
            label = "REBASE"
        elif abs(ratio_settled - 1.0) < args.revert_tol:
            label = "REVERT"
        else:
            label = "AMBIGUOUS"
        rows.append((fid, jd, lr, ratio_immediate, ratio_settled, n_settled, label))

    out = pd.DataFrame(rows, columns=["fund_id", "date", "lr", "ratio_immediate",
                                       "ratio_settled", "n_settled_obs", "label"])
    name_lookup = {r[0]: r[1] for r in con.execute(
        "SELECT fund_id, any_value(scheme_name) FROM fund_map GROUP BY 1").fetchall()}
    out["scheme_name"] = out["fund_id"].map(name_lookup)

    def round_tag(r):
        if r is None or r <= 0:
            return ""
        import math
        p = round(math.log10(r))
        target = 10 ** p
        return "round x%g -- likely face-value change" % target \
            if abs(r - target) / target < 0.10 else "not round -- check individually, may be a real event"

    out["rebase_shape"] = out["ratio_immediate"].apply(
        lambda r: round_tag(r) if pd.notnull(r) else "")

    print(f"\n{len(out)} jumps classified\n")
    print(out["label"].value_counts().to_string())

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 160)

    print("\n--- REBASE: persistent level change -- split by whether the multiplier is round ---")
    reb = out[out.label == "REBASE"].sort_values("ratio_immediate")
    print(reb[["fund_id", "date", "ratio_immediate", "rebase_shape", "scheme_name"]].to_string(index=False))

    print("\n--- REVERT: transient -- these look like pure data errors ---")
    rev = out[out.label == "REVERT"]
    print(rev[["fund_id", "date", "ratio_immediate", "ratio_settled", "scheme_name"]].to_string(index=False))

    print("\n--- AMBIGUOUS: neither persisted nor reverted cleanly -- needs a manual look ---")
    amb = out[out.label == "AMBIGUOUS"]
    print(amb[["fund_id", "date", "ratio_immediate", "ratio_settled", "scheme_name"]].to_string(index=False))

    print("\n--- INSUFFICIENT_FORWARD_DATA: fund likely wound up shortly after the jump ---")
    ins = out[out.label == "INSUFFICIENT_FORWARD_DATA"]
    print(ins[["fund_id", "date", "ratio_immediate", "n_settled_obs", "scheme_name"]].to_string(index=False))

    out.to_csv(args.csv_out, index=False)
    print(f"\nFull table (all columns) written to {args.csv_out}")


if __name__ == "__main__":
    main()
