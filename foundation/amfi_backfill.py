"""
amfi_backfill.py — walk AMFI's historical NAV report and populate the store.

Column layout CONFIRMED against a live response on 2026-08-29 (not assumed
by analogy to NAVAll.txt, which uses a different order):

    Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
    ISIN Div Reinvestment;Net Asset Value;Date

Sample real row:
    119291;L&T Flexicap Fund-Direct Plan-Growth;;;INF917K01FC0;;87.802;01-Jan-2020

Note Plan/Option columns are frequently blank in older data even though the
NAV Name free-text contains "Direct"/"Growth" etc. -- plan/option are derived
from the name via nav_store's classify_plan/classify_option first; when the
name is ambiguous, the report's own Plan/Option columns are used as a
fallback, but ALSO run through the same classifiers (2026-09-11 fix) --
AMFI ships those columns as free-text too ("Direct Plan", "Dividend"), not
a normalized enum, so trusting them verbatim reintroduces exactly the kind
of fragmentation the classifiers exist to prevent.

One call returns EVERY scheme, every trading day, in the window -- so this
single walk populates all three of: scheme metadata (with real ISINs, which
mfapi often lacks for delisted codes), nav history (fills gaps mfapi has),
and universe_snapshot (existence per date -- the actual survivorship fix).

AMFI caps each request at 90 days. Expect roughly 1-1.5 MB of raw text per
trading day across the full universe, so a 90-day window is commonly
80-100 MB. A full 2018-to-present backfill is ~32 windows; expect this to
run for a while and to use a few GB of bandwidth. Start small.

Usage
-----
    python amfi_backfill.py --selftest              offline, no network
    python amfi_backfill.py --start 2026-06-01       small live test window
    python amfi_backfill.py --start 2018-01-01 --resume
                                                      full backfill, resumable
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from collections import defaultdict

import requests

import nav_store

URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
WINDOW_DAYS = 90          # AMFI's hard cap per request
REQUEST_DELAY = 1.0       # be polite; this is a shared public endpoint
TIMEOUT = 120


# ------------------------------------------------------------------ parsing


def is_data_row(parts: list[str]) -> bool:
    return len(parts) == 8 and parts[0].strip().isdigit()


def parse_report(text: str):
    """
    Yields dicts: code, name, plan, option, isin_growth, isin_div, nav, date
    (date as dt.date, nav as float). Skips AMC header lines, the column-title
    row, blanks, and any row with a non-numeric or non-positive NAV.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ";" not in line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if not is_data_row(parts):
            continue
        code, name, plan, option, isin_growth, isin_div, nav_s, date_s = parts
        try:
            nav = float(nav_s)
        except ValueError:
            continue
        if nav <= 0:
            continue
        try:
            date = nav_store.parse_date(date_s)
        except ValueError:
            continue

        # Report's own Plan/Option columns are often blank in older data;
        # the free-text name is the reliable signal, confirmed against
        # real rows during development.
        #
        # (2026-09-11) BUGFIX: when the name itself is ambiguous, this used
        # to fall back to the report's raw Plan/Option column TEXT verbatim
        # (e.g. literal "Direct Plan" / "Regular Plan" / "Dividend") instead
        # of normalizing it. Confirmed real incident: 1,127 scheme rows in
        # mf_nav_full.duckdb carried plan values 'Regular Plan' and
        # 'Direct Plan' as distinct strings alongside the normalized
        # 'REGULAR'/'DIRECT' -- invisible to every returns_panel.py query,
        # since `WHERE plan = 'REGULAR'` is an exact match. Fix: run the
        # raw fallback through the SAME classifier as the name, so any
        # spelling AMFI ships -- "Direct Plan", "DIRECT", "Dividend", "IDCW"
        # -- always collapses to one of DIRECT/REGULAR/UNKNOWN or
        # GROWTH/IDCW/UNKNOWN. Never store the raw column text directly.
        derived_plan = nav_store.classify_plan(name)
        if derived_plan == "UNKNOWN" and plan:
            derived_plan = nav_store.classify_plan(plan)

        derived_option = nav_store.classify_option(name)
        if derived_option == "UNKNOWN" and option:
            derived_option = nav_store.classify_option(option)

        yield dict(
            code=int(code),
            name=name,
            plan=derived_plan,
            option=derived_option,
            isin_growth=None if isin_growth in ("", "-") else isin_growth,
            isin_div=None if isin_div in ("", "-") else isin_div,
            nav=nav,
            date=date,
        )


# ------------------------------------------------------------------ windows


def date_windows(start: dt.date, end: dt.date, size: int = WINDOW_DAYS):
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=size - 1), end)
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def fmt_amfi(d: dt.date) -> str:
    return d.strftime("%d-%b-%Y")


# ------------------------------------------------------------------- fetch


def fetch_window(frm: dt.date, to: dt.date) -> str:
    r = requests.get(URL, params={"frmdt": fmt_amfi(frm), "todt": fmt_amfi(to)},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def ingest_window(con, frm: dt.date, to: dt.date, verbose=True) -> dict:
    t0 = time.time()
    text = fetch_window(frm, to)
    t_fetch = time.time() - t0

    t0 = time.time()
    rows = list(parse_report(text))
    t_parse = time.time() - t0

    if verbose:
        print(f"  fetch: {t_fetch:.1f}s ({len(text)/1e6:.1f} MB)  "
              f"parse: {t_parse:.1f}s ({len(rows)} rows, "
              f"{len(set(r['code'] for r in rows))} distinct codes)")

    if not rows:
        return dict(rows=0, codes=0, dates=0)

    t0 = time.time()

    # 1) scheme metadata -- one bulk upsert, not one call per code.
    #    Latest row seen per code within this window (name/ISIN don't change
    #    mid-window in practice, but latest is the safe choice if they ever do).
    latest_by_code: dict[int, dict] = {}
    for r in rows:
        prev = latest_by_code.get(r["code"])
        if prev is None or r["date"] >= prev["date"]:
            latest_by_code[r["code"]] = r

    metas = [
        nav_store.SchemeMeta(
            amfi_code=code, scheme_name=r["name"], plan=r["plan"],
            option=r["option"], isin_growth=r["isin_growth"],
            isin_div=r["isin_div"], source="amfi_history",
        )
        for code, r in latest_by_code.items()
    ]
    nav_store.bulk_upsert_schemes(con, metas)
    t_schemes = time.time() - t0

    # 2) nav rows -- one bulk upsert across every code in the window.
    t0 = time.time()
    nav_rows = [(r["code"], r["date"], r["nav"]) for r in rows]
    nav_added = nav_store.bulk_upsert_navs(con, nav_rows)
    t_navs = time.time() - t0

    # 3) universe snapshot -- one bulk insert across every (date, code) pair.
    t0 = time.time()
    snapshot_rows = list({(r["date"], r["code"]) for r in rows})
    nav_store.bulk_record_universe(con, snapshot_rows)
    t_universe = time.time() - t0

    if verbose:
        print(f"  db writes: schemes {t_schemes:.1f}s  navs {t_navs:.1f}s  "
              f"universe {t_universe:.1f}s")

    nav_store.log_refresh(con, None, "amfi_history", len(rows), nav_added,
                          True, f"{fmt_amfi(frm)}..{fmt_amfi(to)}")

    return dict(rows=len(rows), codes=len(latest_by_code),
               dates=len(set(r["date"] for r in rows)),
               nav_rows_added=nav_added)


# --------------------------------------------------------------- resumption


def resume_start(con, fallback: dt.date) -> dt.date:
    """Earliest date not yet covered, based on the highest universe_snapshot
    date already stored. Only meaningful if the backfill has been walking
    forward from a fixed start with no gaps -- fine for this script's own
    sequential use, not a substitute for real gap detection."""
    row = con.execute("SELECT max(snap_date) FROM universe_snapshot").fetchone()
    if row and row[0]:
        return row[0] + dt.timedelta(days=1)
    return fallback


# -------------------------------------------------------------------- main


def run(db_path: str, start: dt.date, end: dt.date, resume: bool) -> int:
    con = nav_store.connect(db_path)
    if resume:
        new_start = resume_start(con, start)
        if new_start > start:
            print(f"Resuming from {new_start} (found existing snapshots up to "
                  f"{new_start - dt.timedelta(days=1)})")
            start = new_start
    if start > end:
        print("Nothing to do -- start is after end.")
        con.close()
        return 0

    windows = list(date_windows(start, end))
    print(f"{len(windows)} window(s), {fmt_amfi(start)} -> {fmt_amfi(end)}\n")

    for i, (frm, to) in enumerate(windows, 1):
        print(f"[{i}/{len(windows)}] {fmt_amfi(frm)} -> {fmt_amfi(to)}")
        try:
            stats = ingest_window(con, frm, to)
            print(f"  ok: {stats}")
        except Exception as e:
            print(f"  FAILED: {e}")
            print(f"  Re-run with --resume to continue from here, or "
                  f"--start {frm.isoformat()} to retry this exact window.")
            con.close()
            return 1
        if i < len(windows):
            time.sleep(REQUEST_DELAY)

    cov = nav_store.coverage(con)
    print("\nFinal coverage:")
    for k, v in cov.items():
        print(f"  {k:>12}: {v}")
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

    sample = (
        "L&T Mutual Fund\n"
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
        "ISIN Div Reinvestment;Net Asset Value;Date\n"
        "119291;L&T Flexicap Fund-Direct Plan-Growth;;;INF917K01FC0;;87.802;01-Jan-2020\n"
        "119290;L&T Flexicap Fund-Direct Plan-IDCW;;;INF917K01FB2;INF917K01FA4;35.028;01-Jan-2020\n"
        "\n"
        "Some Other Fund House\n"
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
        "ISIN Div Reinvestment;Net Asset Value;Date\n"
        "999999;Junk Row;;;;;N.A.;01-Jan-2020\n"          # bad NAV, dropped
        "119291;L&T Flexicap Fund-Direct Plan-Growth;;;INF917K01FC0;;88.10;02-Jan-2020\n"
    )
    rows = list(parse_report(sample))
    check("junk NAV dropped", len(rows) == 3)
    check("plan derived from name", rows[0]["plan"] == "DIRECT")
    check("option derived from name (growth)", rows[0]["option"] == "GROWTH")
    check("option derived from name (idcw)", rows[1]["option"] == "IDCW")
    check("isin captured", rows[0]["isin_growth"] == "INF917K01FC0")
    check("date parsed", rows[0]["date"] == dt.date(2020, 1, 1))

    # regression test: AMFI uses "-" as a placeholder for "no ISIN", the
    # same convention seen elsewhere for isin_div. A code with "-" here must
    # become None, not the literal string "-" -- storing "-" caused every
    # ISIN-less scheme in the dataset to look like it shared one fake ISIN
    # (found via foundation_diagnostic.py, 2026-08-30: 162 false-positive
    # ISIN groups, e.g. two unrelated Daiwa Liquid Fund variants).
    dash_sample = (
        "Daiwa Mutual Fund\n"
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
        "ISIN Div Reinvestment;Net Asset Value;Date\n"
        "114516;Daiwa Liquid Fund - Institutional - Daily Dividend;;;-;-;12.34;01-Jan-2020\n"
    )
    dash_rows = list(parse_report(dash_sample))
    check("dash placeholder ISIN becomes None, not literal '-'",
          dash_rows[0]["isin_growth"] is None and dash_rows[0]["isin_div"] is None)

    # regression test (2026-09-11): when the NAV Name gives no plan/option
    # signal, the report's own raw Plan/Option columns must be run through
    # the SAME classifier as the name, never stored as literal AMFI text.
    # Confirmed real incident: 1,127 live rows carried plan='Regular Plan'/
    # 'Direct Plan' as distinct strings from 'REGULAR'/'DIRECT', invisible
    # to every exact-match `WHERE plan = 'REGULAR'` query in the codebase.
    raw_fallback_sample = (
        "Ambiguous Fund House\n"
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
        "ISIN Div Reinvestment;Net Asset Value;Date\n"
        "888777;Ambiguous Multi-Category Fund;Direct Plan;Dividend;INF000A00000;;50.00;01-Jan-2020\n"
    )
    fallback_rows = list(parse_report(raw_fallback_sample))
    check("name gives no plan signal -- raw 'Direct Plan' fallback normalizes to DIRECT",
          fallback_rows[0]["plan"] == "DIRECT")
    check("name gives no option signal -- raw 'Dividend' fallback normalizes to IDCW",
          fallback_rows[0]["option"] == "IDCW")
    check("raw fallback never stores literal AMFI text",
          fallback_rows[0]["plan"] not in ("Direct Plan", "Regular Plan") and
          fallback_rows[0]["option"] not in ("Dividend", "Growth"))

    check("windows split at 90 days",
          list(date_windows(dt.date(2020, 1, 1), dt.date(2020, 4, 30)))[0][1]
          == dt.date(2020, 1, 1) + dt.timedelta(days=89))
    check("windows cover exactly to end, no overshoot",
          next(date_windows(dt.date(2020, 1, 1), dt.date(2020, 1, 1)))
          == (dt.date(2020, 1, 1), dt.date(2020, 1, 1)))
    check("amfi date format", fmt_amfi(dt.date(2020, 1, 5)) == "05-Jan-2020")

    tmp = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    con = nav_store.connect(tmp)

    # simulate ingest_window's DB-writing half without a real fetch, via the
    # same bulk path amfi_backfill.py now actually uses
    latest = {}
    for r in rows:
        latest[r["code"]] = r
    metas = [nav_store.SchemeMeta(
        amfi_code=code, scheme_name=r["name"], plan=r["plan"],
        option=r["option"], isin_growth=r["isin_growth"],
        isin_div=r["isin_div"], source="amfi_history")
        for code, r in latest.items()]
    nav_store.bulk_upsert_schemes(con, metas)
    nav_rows = [(r["code"], r["date"], r["nav"]) for r in rows]
    nav_store.bulk_upsert_navs(con, nav_rows)
    snapshot_rows = list({(r["date"], r["code"]) for r in rows})
    nav_store.bulk_record_universe(con, snapshot_rows)

    check("scheme upserted", con.execute(
        "SELECT count(*) FROM scheme").fetchone()[0] == 2)
    check("nav rows written", con.execute(
        "SELECT count(*) FROM nav WHERE amfi_code=119291").fetchone()[0] == 2)
    check("universe snapshot per date", con.execute(
        "SELECT count(*) FROM universe_snapshot WHERE snap_date=DATE '2020-01-02'"
    ).fetchone()[0] == 1)

    # the actual point of this whole exercise: ISIN-linked splice across a
    # simulated code change (119291 "old era" -> 999888 "new era", same ISIN)
    nav_store.upsert_scheme(con, nav_store.SchemeMeta(
        amfi_code=999888, scheme_name="Renamed Fund-Direct Plan-Growth",
        plan="DIRECT", option="GROWTH", isin_growth="INF917K01FC0",
        source="amfi_history"))
    nav_store.upsert_navs(con, 999888, [(dt.date(2022, 12, 1), 200.0)])
    spliced = nav_store.get_spliced_series(con, "INF917K01FC0")
    check("isin group finds both codes",
          set(nav_store.isin_group(con, "INF917K01FC0")) == {119291, 999888})
    # pandas Timestamp vs datetime.date compare False even for the same day;
    # normalise via .date() before asserting (this bit the selftest itself
    # during development -- underlying data was correct throughout).
    max_d = spliced["nav_date"].max()
    min_d = spliced["nav_date"].min()
    max_d = max_d.date() if hasattr(max_d, "date") else max_d
    min_d = min_d.date() if hasattr(min_d, "date") else min_d
    check("spliced series spans both eras",
          len(spliced) == 3 and
          max_d == dt.date(2022, 12, 1) and
          min_d == dt.date(2020, 1, 1))

    con.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--start", type=dt.date.fromisoformat,
                    default=(dt.date.today() - dt.timedelta(days=89)))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--resume", action="store_true",
                    help="continue from the latest stored universe_snapshot date")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    return run(a.db, a.start, a.end, a.resume)


if __name__ == "__main__":
    sys.exit(main())