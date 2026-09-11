"""
daily_update.py — the thing that should actually be on the cron schedule,
not amfi_backfill.py directly.

Two problems this solves:

1. A single scheduled run (e.g. once at 7pm) can be entirely missed if the
   laptop is asleep at that exact moment -- cron doesn't queue it for wake.
   Fix: run this FREQUENTLY (every couple hours, not once a day). Each run
   is cheap when there's nothing new (amfi_backfill --resume finds an empty
   window and exits fast), so frequent attempts cost ~nothing and massively
   reduce the odds of missing every single window in one day.

2. Staleness was only visible by remembering to run check_freshness.py by
   hand -- not sustainable. Fix: this checks staleness itself after every
   run and fires a native macOS notification ONLY when something's actually
   wrong (a failed fetch, or data older than the tolerance). Silence means
   it's working; a notification means look at backfill.log.

3. (2026-09-10) mf_nav_full.duckdb being fresh didn't mean the app showed
   fresh NAVs -- fundeval_analysis.duckdb is a separate derived DB and
   nothing rebuilt it automatically. Fix: rebuild chain below chains the
   full rebuild sequence after every raw refresh, and writes a lock file
   for the duration so streamlit_app.py's freshness check can tell a
   rebuild is already running and not trigger a second one.

4. (2026-09-11) Direct-plan derived DB (fundeval_analysis_direct.duckdb)
   was left out of the above -- confirmed working command from the
   2026-09-09 manual build (comprehensive, no --start-fy override needed)
   is now wired into the same chain, under the same lock, so both DBs
   stay current together instead of Direct silently lagging.

Usage
-----
    python3 daily_update.py --db mf_nav_full.duckdb
    python3 daily_update.py --selftest    offline, no network, no real notify
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time

import amfi_backfill
import nav_store

STALE_THRESHOLD_DAYS = 3
REGULAR_ANALYSIS_DB = "fundeval_analysis.duckdb"
DIRECT_ANALYSIS_DB = "fundeval_analysis_direct.duckdb"
LOCK_PATH = "daily_update.lock"


def notify(title: str, message: str) -> None:
    """Native macOS notification. Never let a notification failure (e.g.
    no GUI session, or running on a non-Mac) crash the actual update --
    this is a nice-to-have, not the point of the script."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def run(db_path: str, backfill_fn=None, notify_fn=notify) -> int:
    backfill_fn = backfill_fn or amfi_backfill.run
    today = dt.date.today()
    start = today - dt.timedelta(days=89)

    # IMPORTANT: don't hold our own connection open here -- backfill_fn
    # opens and closes its own, and DuckDB allows only one writer. Holding
    # both at once reproduces the exact lock error hit earlier this session.
    rc = backfill_fn(db_path, start, today, resume=True)
    if rc != 0:
        notify_fn("FundEval update failed",
                  "amfi_backfill.py returned non-zero -- check backfill.log")
        print("FAILED: backfill returned non-zero")
        return 1

    con = nav_store.connect(db_path)
    last_snap = con.execute("SELECT max(snap_date) FROM universe_snapshot").fetchone()[0]
    con.close()

    staleness = (today - last_snap).days if last_snap else None

    if staleness is None:
        notify_fn("FundEval: no data", "universe_snapshot is empty")
        print("NO DATA")
    elif staleness > STALE_THRESHOLD_DAYS:
        notify_fn("FundEval is stale", f"{staleness} days behind -- check backfill.log")
        print(f"STALE: {staleness} days behind")
    else:
        print(f"OK: {staleness} days behind, within tolerance")

    return 0


def _rebuild_plan(plan: str, src_db: str, out_db: str, notify_fn=notify) -> int:
    """One plan's full rebuild chain -- shared by both Regular and Direct.
    Confirmed working for REGULAR on 2026-09-10 and for DIRECT (comprehensive
    run, no --start-fy override needed) on 2026-09-09.

    Stops (without a crash) and notifies if a genuinely new, unreviewed
    data anomaly turns up -- publishing an unverified correction
    automatically is worse than a day of stale data.
    """
    steps = [
        ["python3", "analysis/returns_panel.py", "--plan", plan,
         "--src", src_db, "--out", out_db],
        ["python3", "analysis/rebase_classifier.py", "--out", out_db],
        ["python3", "analysis/apply_nav_corrections.py", "--out", out_db],
        ["python3", "analysis/exclude_bad_rows.py", "--out", out_db],
        ["python3", "analysis/returns_panel.py", "--panel-only", "--out", out_db],
        ["python3", "analysis/build_category_map.py", "--out", out_db],
    ]
    for cmd in steps:
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout)
        if result.returncode == 2:
            notify_fn("FundEval: new fund needs review",
                       f"{plan}: {cmd[1]} flagged an unreviewed jump -- check nightly_rebuild.log")
            print(f"REVIEW NEEDED ({plan}) -- stopping before panel rebuild, "
                  "so an unverified correction never gets published")
            return 2
        if result.returncode != 0:
            notify_fn("FundEval rebuild failed",
                       f"{plan}: {cmd[1]} failed -- check nightly_rebuild.log")
            print(result.stderr)
            return 1
    return 0


def rebuild_all_derived_analysis(src_db: str, notify_fn=notify) -> int:
    """Rebuilds both derived analysis DBs (Regular and Direct plan) under
    one shared lock file, so streamlit_app.py's staleness check sees a
    single "rebuild in progress" window covering both -- not two separate
    lock/unlock cycles with a gap between them where a concurrent trigger
    could sneak in.

    Runs both chains even if one needs review or fails -- they're
    independent databases with independent pipelines, so a genuine
    problem in one shouldn't stall a fix that's ready to ship for the
    other. Returns the more severe of the two exit codes (2 > 1 > 0).
    """
    with open(LOCK_PATH, "w") as f:
        f.write(str(time.time()))

    try:
        rc_regular = _rebuild_plan("REGULAR", src_db, REGULAR_ANALYSIS_DB, notify_fn)
        rc_direct = _rebuild_plan("DIRECT", src_db, DIRECT_ANALYSIS_DB, notify_fn)
        return max(rc_regular, rc_direct)
    finally:
        try:
            os.remove(LOCK_PATH)
        except FileNotFoundError:
            pass


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
    con.close()   # don't hold it open -- run() opens its own

    notifications = []
    def fake_notify(title, message):
        notifications.append((title, message))

    # Case 1: backfill "succeeds" but data is old -> should notify stale
    def fake_backfill_ok(db_path, start, end, resume):
        con = nav_store.connect(db_path)
        nav_store.bulk_upsert_schemes(con, [nav_store.SchemeMeta(
            amfi_code=1, scheme_name="Test-Direct-Growth", plan="DIRECT", option="GROWTH")])
        old_date = dt.date.today() - dt.timedelta(days=10)
        nav_store.bulk_upsert_navs(con, [(1, old_date, 100.0)])
        nav_store.bulk_record_universe(con, [(old_date, 1)])
        con.close()
        return 0

    rc = run(tmp, backfill_fn=fake_backfill_ok, notify_fn=fake_notify)
    check("returns 0 even when stale (stale is not a crash)", rc == 0)
    check("stale data triggers a notification", len(notifications) == 1)
    check("stale notification mentions days behind",
          "days behind" in notifications[0][1])

    # Case 2: backfill fails -> should notify failure, return 1
    notifications.clear()
    def fake_backfill_fail(db_path, start, end, resume):
        return 1

    rc = run(tmp, backfill_fn=fake_backfill_fail, notify_fn=fake_notify)
    check("returns 1 on backfill failure", rc == 1)
    check("failure triggers a notification", len(notifications) == 1)
    check("failure notification says 'failed'",
          "failed" in notifications[0][0].lower())

    # Case 3: backfill succeeds AND data is current -> silence
    notifications.clear()
    def fake_backfill_current(db_path, start, end, resume):
        con = nav_store.connect(db_path)
        nav_store.bulk_upsert_schemes(con, [nav_store.SchemeMeta(
            amfi_code=2, scheme_name="Test2-Direct-Growth", plan="DIRECT", option="GROWTH")])
        today = dt.date.today()
        nav_store.bulk_upsert_navs(con, [(2, today, 100.0)])
        nav_store.bulk_record_universe(con, [(today, 2)])
        con.close()
        return 0

    rc = run(tmp, backfill_fn=fake_backfill_current, notify_fn=fake_notify)
    check("current data triggers NO notification", len(notifications) == 0)
    check("returns 0 when current", rc == 0)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--skip-derived-rebuild", action="store_true",
                     help="only refresh mf_nav_full.duckdb, skip rebuilding both "
                          "fundeval_analysis.duckdb and fundeval_analysis_direct.duckdb")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()

    rc = run(a.db)
    if rc != 0:
        return rc  # raw refresh itself failed -- don't rebuild derived DBs on bad data

    if a.skip_derived_rebuild:
        return 0

    return rebuild_all_derived_analysis(a.db)


if __name__ == "__main__":
    sys.exit(main())