"""
foundation_diagnostic.py — end-to-end health check for Foundation v1.

Not a re-run of anything already done. This checks the things that would
be embarrassing to discover later: data integrity issues that shouldn't be
possible given the schema/parsers but are worth confirming directly, plus
a summary of where category/staleness coverage actually stand and whether
ISIN-linking still works at full scale (not just in the unit tests).

Run:  python foundation_diagnostic.py --db mf_nav_full.duckdb
"""

import argparse
import datetime as dt
import sys

import nav_store


def section(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def run(db_path: str) -> int:
    con = nav_store.connect(db_path)
    warnings = []

    # ---------------------------------------------------------- overview
    section("Overview")
    cov = nav_store.coverage(con)
    for k, v in cov.items():
        print(f"  {k:>12}: {v}")

    # ------------------------------------------------------ data integrity
    section("Data integrity (should all read zero)")

    bad_nav = con.execute(
        "SELECT count(*) FROM nav WHERE nav <= 0").fetchone()[0]
    print(f"  NAV rows <= 0:                    {bad_nav}")
    if bad_nav:
        warnings.append(f"{bad_nav} non-positive NAV rows made it into the store")

    future = con.execute(
        "SELECT count(*) FROM nav WHERE nav_date > current_date").fetchone()[0]
    print(f"  NAV rows dated in the future:      {future}")
    if future:
        warnings.append(f"{future} NAV rows are dated after today")

    orphan_snap = con.execute("""
        SELECT count(*) FROM universe_snapshot u
        LEFT JOIN scheme s ON u.amfi_code = s.amfi_code
        WHERE s.amfi_code IS NULL
    """).fetchone()[0]
    print(f"  universe_snapshot rows with no scheme record: {orphan_snap}")
    if orphan_snap:
        warnings.append(f"{orphan_snap} universe_snapshot rows reference a code "
                        f"never written to scheme -- a metadata upsert was skipped somewhere")

    # UNKNOWN doesn't mean "a different plan" -- it means "predates the
    # distinction" (classify_plan correctly returns UNKNOWN for pre-2013
    # scheme names, since there was no "Regular" label before "Direct"
    # existed to contrast it against). Only flag a REAL disagreement: two
    # non-UNKNOWN values that differ from each other.
    dup_isin_conflict = con.execute("""
        SELECT count(*) FROM (
            SELECT isin_growth FROM scheme
            WHERE isin_growth IS NOT NULL
            GROUP BY isin_growth
            HAVING count(DISTINCT plan) FILTER (WHERE plan != 'UNKNOWN') > 1
                OR count(DISTINCT option) FILTER (WHERE option != 'UNKNOWN') > 1
        )
    """).fetchone()[0]
    print(f"  ISINs with a REAL plan/option disagreement "
          f"(excluding UNKNOWN, which just means pre-2013): {dup_isin_conflict}")
    if dup_isin_conflict:
        warnings.append(f"{dup_isin_conflict} ISINs have a genuine plan/option "
                        f"disagreement (not just an UNKNOWN vs known case) -- "
                        f"worth inspecting with inspect_isin_plan_conflicts.py")

    # ------------------------------------------------------- category
    section("Category coverage (Direct+Growth canonical universe)")
    total_canon = con.execute(
        "SELECT count(*) FROM scheme WHERE plan='DIRECT' AND option='GROWTH'"
    ).fetchone()[0]
    null_canon = con.execute(
        "SELECT count(*) FROM scheme WHERE plan='DIRECT' AND option='GROWTH' "
        "AND category IS NULL"
    ).fetchone()[0]
    filled_canon = total_canon - null_canon
    pct = 100 * filled_canon / total_canon if total_canon else 0
    print(f"  canonical universe size: {total_canon}")
    print(f"  category filled:         {filled_canon} ({pct:.1f}%)")
    print(f"  category missing:        {null_canon}")

    recent_null = con.execute("""
        SELECT count(*) FROM scheme
        WHERE plan='DIRECT' AND option='GROWTH' AND category IS NULL
          AND last_nav_date >= DATE '2023-01-01'
    """).fetchone()[0]
    print(f"  of which recently active (>=2023) with no category: {recent_null}")
    if recent_null:
        warnings.append(f"{recent_null} recently-active canonical schemes still "
                        f"have no category -- known gap, likely AMC-rename cases "
                        f"(Reliance->Nippon India etc.) where ISIN didn't carry over")

    # ------------------------------------------------------- staleness
    section("Status distribution")
    for status, n in con.execute(
        "SELECT status, count(*) AS n FROM scheme GROUP BY status ORDER BY n DESC"
    ).fetchall():
        print(f"  {status:>10}: {n}")

    # -------------------------------------------------- ISIN-linking check
    section("ISIN-linking sanity check (full-scale, real data)")
    print("  Confirmed real case from earlier this session: L&T Mid Cap Fund")
    print("  (code 119807, pre-2022) and HSBC Midcap Fund (code 151036,")
    print("  post-2022) share ISIN INF917K01FZ1 -- verifying that still holds")
    print("  now that the full 2006-2026 history is loaded:\n")
    group = nav_store.isin_group(con, "INF917K01FZ1")
    print(f"  isin_group('INF917K01FZ1') -> {group}")
    if set(group) == {119807, 151036}:
        print("  OK -- matches the confirmed real case exactly")
    else:
        warnings.append(f"isin_group('INF917K01FZ1') returned {group}, expected "
                        f"[119807, 151036] -- something changed since this was "
                        f"last verified, worth investigating before trusting "
                        f"splicing for the persistence study")

    spliced = nav_store.get_spliced_series(con, "INF917K01FZ1")
    if len(spliced):
        print(f"  spliced series: {len(spliced)} rows, "
              f"{spliced['nav_date'].min()} -> {spliced['nav_date'].max()}")
    else:
        warnings.append("get_spliced_series('INF917K01FZ1') returned nothing")

    isin_groups_with_multiple = con.execute("""
        SELECT count(*) FROM (
            SELECT isin_growth FROM scheme
            WHERE isin_growth IS NOT NULL
            GROUP BY isin_growth HAVING count(*) > 1
        )
    """).fetchone()[0]
    print(f"\n  Total ISINs linking more than one scheme code across the whole "
          f"dataset: {isin_groups_with_multiple}")
    print("  (each of these represents a rename/administrative code change "
          "that naive per-code analysis would silently truncate)")

    # ----------------------------------------------------------- refresh log
    section("Refresh log")
    log_summary = con.execute("""
        SELECT source, count(*) AS runs, sum(CASE WHEN NOT ok THEN 1 ELSE 0 END) AS failures,
               min(run_at) AS first_run, max(run_at) AS last_run
        FROM refresh_log GROUP BY source
    """).fetchall()
    for source, runs, failures, first_run, last_run in log_summary:
        print(f"  {source:>14}: {runs} runs, {failures} failures, "
              f"{first_run} -> {last_run}")
        if failures:
            warnings.append(f"{failures} failed runs logged for source '{source}'")

    # --------------------------------------------------------------- verdict
    section("Verdict")
    if warnings:
        print(f"  {len(warnings)} thing(s) worth a look:\n")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("  No integrity issues found. Known category gap aside (expected, "
              "already understood), Foundation v1 looks solid.")

    con.close()
    return 1 if warnings else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    return run(a.db)


if __name__ == "__main__":
    sys.exit(main())
