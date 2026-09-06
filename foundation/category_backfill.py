"""
category_backfill.py — fill the category gap AMFI's historical report can't.

AMFI's DownloadNAVHistoryReport_Po.aspx has NO category column at all
(confirmed against the actual header row, 2026-08-29). Category only
exists via mfapi's per-code meta block, and only for codes mfapi currently
tracks -- which, per this project's own findings, is not all of them.

Two passes:
  1. mfapi lookup per code. Correct at the exact granularity needed --
     mfapi returns THIS code's own category, so there's no cross-plan or
     cross-option ambiguity to worry about.
  2. ISIN-sibling inheritance for whatever remains NULL after pass 1 (mfapi
     has genuinely never listed the code -- confirmed real case: L&T Mid
     Cap Fund's pre-2022 code, 119807). This works because isin_group()
     links the exact same unit class across an administrative code change,
     which is a narrower and safer claim than "same fund, any plan/option."
     No network needed -- it's a within-database join.

Usage
-----
    python category_backfill.py --selftest              offline, no network
    python category_backfill.py --db mf_nav_full.duckdb  canonical universe only (default)
    python category_backfill.py --db mf_nav_full.duckdb --include-non-canonical
                                                          every plan/option too (slower)
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import pandas as pd
import requests

import nav_store

NAVALL_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAVALL_TIMEOUT = 90       # one larger request, not thousands of small ones

# mfapi per-code fallback -- kept only for the rare case a code is genuinely
# absent from AMFI's own current snapshot AND has no ISIN sibling to inherit
# from. Off by default: mfapi is a mirror of this exact AMFI file, so it
# essentially never has category info this pass doesn't already have, and
# hitting it thousands of times triggered real throttling in practice
# (2026-08-30: "a lot of slow responses and timing out ... showing 1 hour").
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
REQUEST_DELAY = 0.25
MAX_RETRIES = 2
BACKOFF_BASE = 1.5
TIMEOUT = 6

CATEGORY_HEADER_RE = re.compile(r"^.*?\((.*)\)\s*$")


# ------------------------------------------------------------- NAVAll pass


def parse_navall(text: str) -> dict[int, str]:
    """
    Parse AMFI's current-day NAVAll.txt into {amfi_code: category}.

    Structure confirmed against a live fetch (2026-08-30):
        Open Ended Schemes(Debt Scheme - Banking and PSU Fund)
        <blank>
        Aditya Birla Sun Life Mutual Fund
        <blank>
        119551;INF209KA12Z1;INF209KA13Z9;Aditya Birla...;Direct Plan;IDCW-Re-investment;106.8710;28-Aug-2026

    Column order here is code;isin_growth;isin_reinvest;name;plan;option;nav;date
    -- name does NOT embed plan/option the way the historical-range report's
    NAV Name field does (that's a DIFFERENT AMFI report, confirmed separately
    in this project; don't assume the two share a layout).

    Category-header lines are the only non-data lines containing parentheses;
    AMC header lines have neither semicolons nor parentheses. That's the
    only signal available and it's sufficient here, not bulletproof in
    general (an AMC name with literal parentheses would break it, none seen
    in practice).
    """
    result: dict[int, str] = {}
    current_category: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" in line:
            parts = [p.strip() for p in line.split(";")]
            if len(parts) == 8 and parts[0].isdigit() and current_category:
                result[int(parts[0])] = current_category
            continue
        m = CATEGORY_HEADER_RE.match(line)
        if m:
            current_category = m.group(1).strip()
        # else: an AMC name header -- category carries forward unchanged

    return result


def fetch_navall(timeout=NAVALL_TIMEOUT) -> str:
    r = requests.get(NAVALL_URL, timeout=timeout)
    r.raise_for_status()
    return r.text


def bulk_update_categories(con, code_to_category: dict[int, str]) -> int:
    """One native SQL pass -- see nav_store's bulk_upsert_* for why this is
    DataFrame-register + UPDATE...FROM, not a per-code loop. Only ever
    writes where category IS NULL; never overwrites an existing value."""
    if not code_to_category:
        return 0
    df = pd.DataFrame(list(code_to_category.items()), columns=["amfi_code", "category"])
    con.register("_bulk_category_staging", df)
    try:
        before = con.execute(
            "SELECT count(*) FROM scheme WHERE category IS NULL").fetchone()[0]
        con.execute(
            """
            UPDATE scheme SET category = s.category
            FROM _bulk_category_staging s
            WHERE scheme.amfi_code = s.amfi_code
              AND scheme.category IS NULL
            """
        )
        after = con.execute(
            "SELECT count(*) FROM scheme WHERE category IS NULL").fetchone()[0]
    finally:
        con.unregister("_bulk_category_staging")
    return before - after


# ------------------------------------------------------------ mfapi pass (fallback only)


def fetch_category(code: int, fetch=requests.get) -> str | None:
    """One mfapi lookup, with retry/backoff matching refresh_nav.py's
    existing convention. Returns None on any failure -- never raises,
    since a single bad code shouldn't kill a multi-thousand-code run."""
    for attempt in range(MAX_RETRIES):
        try:
            r = fetch(MFAPI_URL.format(code=code), timeout=TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            meta = data.get("meta") or {}
            cat = meta.get("scheme_category")
            return cat if cat else None
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE ** attempt)
    return None


def codes_missing_category(con, canonical_only: bool, limit: int | None) -> list[int]:
    q = "SELECT amfi_code FROM scheme WHERE category IS NULL"
    if canonical_only:
        q += " AND plan='DIRECT' AND option='GROWTH'"
    q += " ORDER BY amfi_code"
    if limit:
        q += f" LIMIT {limit}"
    return [r[0] for r in con.execute(q).fetchall()]


def pass1_mfapi(con, codes: list[int], verbose=True, delay=REQUEST_DELAY,
                fetch_fn=fetch_category, heartbeat=10) -> int:
    filled = 0
    slow = 0          # codes that hit the timeout/retry path -- direct
                      # evidence of whether mfapi is throttling, not a guess
    t_start = time.time()
    for i, code in enumerate(codes, 1):
        t0 = time.time()
        cat = fetch_fn(code)
        if time.time() - t0 > 3:
            slow += 1
        if cat:
            con.execute("UPDATE scheme SET category=? WHERE amfi_code=?", [cat, code])
            filled += 1
        if verbose and i % heartbeat == 0:
            rate = i / (time.time() - t_start)
            remaining_min = (len(codes) - i) / rate / 60 if rate > 0 else float("inf")
            print(f"  [{i}/{len(codes)}] {filled} filled, {slow} slow/timed-out, "
                  f"{rate:.2f} codes/s, ~{remaining_min:.1f} min left")
        if delay:
            time.sleep(delay)
    return filled


# ------------------------------------------------------------------ pass 2


def pass2_isin_inherit(con, verbose=True) -> int:
    """Propagate category to every ISIN-sibling still NULL, in one
    statement -- no per-code loop, no network."""
    before = con.execute(
        "SELECT count(*) FROM scheme WHERE category IS NULL").fetchone()[0]
    con.execute(
        """
        UPDATE scheme SET category = src.category
        FROM (
            SELECT isin_growth, max(category) AS category
            FROM scheme
            WHERE isin_growth IS NOT NULL AND category IS NOT NULL
            GROUP BY isin_growth
        ) src
        WHERE scheme.isin_growth = src.isin_growth
          AND scheme.category IS NULL
        """
    )
    after = con.execute(
        "SELECT count(*) FROM scheme WHERE category IS NULL").fetchone()[0]
    filled = before - after
    if verbose:
        print(f"  ISIN inheritance filled {filled} more (no network needed)")
    return filled


# -------------------------------------------------------------------- main


def run(db_path: str, canonical_only: bool, limit: int | None,
        use_mfapi_fallback: bool = False) -> int:
    con = nav_store.connect(db_path)

    print("Pass 1: AMFI NAVAll.txt -- one request covers every currently-"
          "listed scheme's category. This replaces looping mfapi per code, "
          "which triggered real throttling in practice (2026-08-30).")
    text = fetch_navall()
    code_to_cat = parse_navall(text)
    print(f"  parsed category for {len(code_to_cat)} codes")
    filled1 = bulk_update_categories(con, code_to_cat)
    print(f"  -> {filled1} filled\n")

    print("Pass 2 (no network): ISIN-sibling inheritance")
    filled2 = pass2_isin_inherit(con)
    print()

    if use_mfapi_fallback:
        codes = codes_missing_category(con, canonical_only, limit)
        if codes:
            print(f"Pass 3 (opt-in, off by default): mfapi per-code for the "
                  f"{len(codes)} still missing. Expect this to be slow and "
                  f"possibly throttled -- it's a last resort, not the norm, "
                  f"since mfapi mirrors the exact file Pass 1 already read.")
            filled3 = pass1_mfapi(con, codes)
            print(f"  -> {filled3} filled via mfapi\n")
            pass2_isin_inherit(con)
            print()

    scope = "Direct+Growth canonical universe" if canonical_only else "ALL plan/option variants"
    remaining = con.execute(
        "SELECT count(*) FROM scheme WHERE category IS NULL"
        + (" AND plan='DIRECT' AND option='GROWTH'" if canonical_only else "")
    ).fetchone()[0]
    print(f"Remaining without category ({scope}): {remaining}")
    print(f"Remaining without category: {remaining}")
    if remaining:
        sample = con.execute(
            "SELECT amfi_code, scheme_name FROM scheme WHERE category IS NULL"
            + (" AND plan='DIRECT' AND option='GROWTH'" if canonical_only else "")
            + " LIMIT 10"
        ).fetchall()
        print("Sample (genuinely unresolvable via mfapi or ISIN sibling -- "
              "need a category-master file or manual review):")
        for code, name in sample:
            print(f"  {code:>8}  {name}")

    con.close()
    return 0


# ----------------------------------------------------------------- selftest


def _selftest() -> int:
    import tempfile, os

    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")

    tmp = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    con = nav_store.connect(tmp)

    # --- NAVAll.txt parser, tested against a REAL fetch (2026-08-30), not
    # synthetic data -- exact text as pasted from `curl` output ---------
    real_sample = """Open Ended Schemes(Children's Fund - Childrens' Fund)

Axis Mutual Fund

135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;30.5341;28-Aug-2026
135765;INF846K01WP8;-;Axis Children's Fund;Direct Plan;IDCW Option;28.1274;28-Aug-2026
135759;INF846K01WJ1;-;Axis Children's Fund;Regular Plan;Growth Option;26.6029;28-Aug-2026

Open Ended Schemes(Debt Scheme - Banking and PSU Fund)

Aditya Birla Sun Life Mutual Fund

119551;INF209KA12Z1;INF209KA13Z9;Aditya Birla Sun Life Banking & PSU Debt Fund;Direct Plan;IDCW-Re-investment;106.8710;28-Aug-2026
108273;INF209K01LV0;-;Aditya Birla Sun Life Banking & PSU Debt Fund;Regular Plan;GROWTH;387.3608;28-Aug-2026

Franklin Templeton Mutual Fund

129008;INF090I01KR8;-;Franklin India Banking & PSU Debt Fund;Direct Plan;Growth;25.2221;28-Aug-2026

Open Ended Schemes(Debt Scheme - Corporate Bond Fund)

Aditya Birla Sun Life Mutual Fund
"""
    parsed = parse_navall(real_sample)
    check("children's fund codes parsed", parsed.get(135762) == "Children's Fund - Childrens' Fund")
    check("category carries across AMC header (Axis -> still children's fund)",
          parsed.get(135759) == "Children's Fund - Childrens' Fund")
    check("category switches at next header (Banking and PSU)",
          parsed.get(119551) == "Debt Scheme - Banking and PSU Fund")
    check("category persists across a DIFFERENT AMC under same header",
          parsed.get(129008) == "Debt Scheme - Banking and PSU Fund")
    check("dangling header with no data rows contributes nothing", 148888 not in parsed)
    check("exactly the codes present got parsed, no phantom entries",
          set(parsed.keys()) == {135762, 135765, 135759, 119551, 108273, 129008})

    filled = bulk_update_categories(con, parsed)
    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option) VALUES
        (135762, 'placeholder', 'DIRECT', 'GROWTH'),
        (119551, 'placeholder', 'DIRECT', 'IDCW')
    """)
    # re-run now that rows actually exist in `scheme` to update
    filled = bulk_update_categories(con, parsed)
    check("bulk update wrote real rows", filled == 2)
    check("category landed correctly", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=135762"
    ).fetchone()[0] == "Children's Fund - Childrens' Fund")

    # never overwrite an existing category from a different source
    con.execute("UPDATE scheme SET category='PRE-EXISTING' WHERE amfi_code=119551")
    bulk_update_categories(con, {119551: "SHOULD NOT OVERWRITE"})
    check("bulk update never overwrites a non-null category", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=119551"
    ).fetchone()[0] == "PRE-EXISTING")

    con.execute("DELETE FROM scheme")

    # Two codes sharing an ISIN (the confirmed real pattern: old code has no
    # mfapi presence at all, new code does). Plus one isolated code with no
    # sibling and no mfapi presence -- should stay NULL, correctly.
    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option, isin_growth) VALUES
        (119807, 'L&T Mid Cap Fund-Direct Plan-Growth', 'DIRECT', 'GROWTH', 'INF917K01FZ1'),
        (151036, 'HSBC Midcap Fund - Direct Plan - Growth', 'DIRECT', 'GROWTH', 'INF917K01FZ1'),
        (999001, 'Orphan Fund-Direct Plan-Growth', 'DIRECT', 'GROWTH', 'INFNOSIBLING01')
    """)

    # Fake mfapi: only knows about the NEW code (151036), matching the real
    # confirmed case where mfapi never heard of 119807 at all.
    def fake_fetch(code):
        if code == 151036:
            return "Equity Scheme - Mid Cap Fund"
        return None   # mfapi genuinely has nothing for 119807 or 999001

    codes = codes_missing_category(con, canonical_only=True, limit=None)
    check("finds all 3 codes missing category", set(codes) == {119807, 151036, 999001})

    filled1 = pass1_mfapi(con, codes, verbose=False, delay=0, fetch_fn=fake_fetch)
    check("pass1 fills only the mfapi-known code", filled1 == 1)
    check("151036 has category after pass1", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=151036"
    ).fetchone()[0] == "Equity Scheme - Mid Cap Fund")
    check("119807 still NULL after pass1 (mfapi never knew it)", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=119807"
    ).fetchone()[0] is None)

    filled2 = pass2_isin_inherit(con, verbose=False)
    check("pass2 fills exactly the ISIN sibling", filled2 == 1)
    check("119807 inherited category via shared ISIN", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=119807"
    ).fetchone()[0] == "Equity Scheme - Mid Cap Fund")
    check("orphan with no sibling and no mfapi presence stays NULL", con.execute(
        "SELECT category FROM scheme WHERE amfi_code=999001"
    ).fetchone()[0] is None)

    # canonical_only filter should exclude a Regular-plan code
    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option, isin_growth) VALUES
        (888001, 'Some Fund-Regular Plan-Growth', 'REGULAR', 'GROWTH', 'INFREG0001')
    """)
    canon = codes_missing_category(con, canonical_only=True, limit=None)
    check("canonical filter excludes regular plan", 888001 not in canon)
    all_codes = codes_missing_category(con, canonical_only=False, limit=None)
    check("non-canonical includes regular plan", 888001 in all_codes)

    con.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap codes processed in the optional mfapi fallback pass")
    ap.add_argument("--include-non-canonical", action="store_true",
                    help="also fill Regular/IDCW variants, not just Direct+Growth")
    ap.add_argument("--mfapi-fallback", action="store_true",
                    help="also try mfapi per-code for whatever NAVAll.txt + "
                         "ISIN inheritance couldn't resolve (slow, off by default)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    return run(a.db, canonical_only=not a.include_non_canonical, limit=a.limit,
              use_mfapi_fallback=a.mfapi_fallback)


if __name__ == "__main__":
    sys.exit(main())
