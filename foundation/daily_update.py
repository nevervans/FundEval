"""
daily_update.py — the thing that should actually be on the cron schedule,
not amfi_backfill.py directly.

Problems this solves:

1. A single scheduled run (e.g. once at 7pm) can be entirely missed if the
   laptop is asleep at that exact moment -- cron doesn't queue it for wake.
   Fix: run this FREQUENTLY (every couple hours, not once a day) WHEN this
   is running on a machine that can be asleep. Each run is cheap when
   there's nothing new (amfi_backfill --resume finds an empty window and
   exits fast), so frequent attempts cost ~nothing and massively reduce
   the odds of missing every single window in one day. (This whole problem
   disappears once this runs on GitHub Actions instead -- see point 6.)

2. Staleness was only visible by remembering to run check_freshness.py by
   hand -- not sustainable. Fix: this checks staleness itself after every
   run and fires a native macOS notification ONLY when something's actually
   wrong (a failed fetch, or data older than the tolerance). Silence means
   it's working; a notification means look at backfill.log. (On a non-Mac
   runner, notify() silently no-ops -- see its docstring -- but a failed
   or review-needed run still exits non-zero, which GitHub Actions surfaces
   as a failed run and emails about by default.)

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

5. (2026-09-13) The hosted Streamlit Cloud deployment has no access to
   this machine's filesystem at all -- it only ever sees whatever was last
   uploaded to Cloudflare R2 (bucket: "fundeval"). Fix: after a rebuild
   pass, this pushes each derived DB whose OWN chain fully succeeded (see
   _dbs_to_push) up to R2, so the hosted app can pick up fresh NAVs on its
   own. A plan that stopped for review or failed is never pushed -- that
   would risk quietly overwriting the last known-good copy the hosted app
   is serving with something unverified.

6. (2026-09-13) Laptop-dependence goes further than just "might be
   asleep" -- if BOTH Mac and Lenovo are off, nothing updates at all, ever,
   regardless of run frequency. Fix: this same script can now also run on
   GitHub Actions (an ephemeral, ownerless runner -- see the repo's
   .github/workflows/daily_update.yml), on a schedule, independent of any
   physical machine. The one wrinkle: an Actions runner starts from a
   blank checkout every time, so mf_nav_full.duckdb -- the 1.7GB,
   2006-present raw archive -- has to round-trip through R2 too, or every
   run would silently discard everything older than the trailing 90-day
   window. See --sync-raw-db-with-r2 / pull_raw_db_from_r2 below. This is
   gated behind an explicit flag and OFF by default specifically so
   Mac/Lenovo -- which already hold the one authoritative local copy --
   never have it overwritten by a possibly-behind R2 copy.

Usage
-----
    python3 foundation/daily_update.py --db mf_nav_full.duckdb
    python3 foundation/daily_update.py --sync-raw-db-with-r2   # GitHub Actions only
    python3 foundation/daily_update.py --skip-r2-push          # rebuild, don't upload
    python3 foundation/daily_update.py --selftest               # offline, no network
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
R2_DEFAULT_BUCKET = "fundeval"


def notify(title: str, message: str) -> None:
    """Native macOS notification. Never let a notification failure (e.g.
    no GUI session, or running on a non-Mac -- including a GitHub Actions
    runner, where osascript doesn't exist at all) crash the actual update
    -- this is a nice-to-have, not the point of the script. On Actions, a
    failed/review-needed run still exits non-zero, which surfaces as a
    failed workflow run and triggers GitHub's own default failure email --
    a different channel, but not a silent one."""
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


def _r2_client():
    """Builds a boto3 S3-compatible client for Cloudflare R2 from
    environment variables. Returns None (not an exception) if any required
    variable is missing, so a machine that hasn't been set up for R2 yet
    (or simply doesn't need to touch R2 at all, e.g. a plain local run)
    just skips R2 entirely instead of crashing.
    """
    endpoint = os.environ.get("R2_ENDPOINT")
    access_key = os.environ.get("R2_ACCESS_KEY")
    secret_key = os.environ.get("R2_SECRET_KEY")
    if not (endpoint and access_key and secret_key):
        return None
    import boto3  # imported lazily -- only needed on whichever machine(s)
                  # actually talk to R2, not a hard dependency for the rest
                  # of this script's local-only functionality
    from botocore.config import Config
    # (2026-09-13) botocore >=1.36 defaults to attaching integrity-checksum
    # headers to every S3 request ("when_supported"). R2 doesn't handle
    # those the way AWS S3 does, which surfaces as a 403 on reads and
    # SignatureDoesNotMatch on writes -- not a credentials problem, a
    # third-party-S3-compatibility one. Pinning both settings back to
    # "when_required" (pre-2025 behavior) fixes it. Confirmed against the
    # actual GitHub Actions failure on 2026-09-13.
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def push_to_r2(db_files, client_factory=_r2_client, notify_fn=notify) -> None:
    """Uploads the given local DB files to the R2 bucket so the hosted
    Streamlit app -- which has no access to this machine at all -- can
    pick up fresh data on its own. For the two derived DBs, only ever
    called (see rebuild_all_derived_analysis) on files whose OWN rebuild
    chain fully succeeded, so a DB that stopped early for review is never
    published in a half-rebuilt or stale state. For the raw store, only
    ever called when --sync-raw-db-with-r2 is set (see main()).

    Silently no-ops (with a printed note, not a crash or a false-alarm
    notification) if R2 isn't configured on this machine -- e.g. during
    rollout, if only one of Mac/Lenovo/Actions has credentials set.
    """
    bucket = os.environ.get("R2_BUCKET", R2_DEFAULT_BUCKET)
    s3 = client_factory()
    if s3 is None:
        print("R2 not configured on this machine (missing R2_ENDPOINT / "
              "R2_ACCESS_KEY / R2_SECRET_KEY) -- skipping upload.")
        return
    for db_file in db_files:
        if not os.path.exists(db_file):
            continue
        try:
            s3.upload_file(db_file, bucket, db_file)
            print(f"Uploaded {db_file} to R2 bucket '{bucket}'")
        except Exception as e:
            notify_fn("FundEval R2 push failed", f"{db_file}: {e}")
            print(f"FAILED to upload {db_file} to R2: {e}")


def pull_raw_db_from_r2(db_path: str, client_factory=_r2_client) -> bool:
    """Pulls mf_nav_full.duckdb down from R2 before running, for an
    ephemeral machine with no persistent local disk (a GitHub Actions
    runner). Every such run starts from a blank checkout -- without this,
    the incremental backfill below would silently overwrite the persisted
    2006-present archive with just the trailing ~90-day window.

    Returns True if the pull succeeded, False otherwise (including when R2
    isn't configured, or the object doesn't exist yet in the bucket --
    e.g. before the one-time manual seed upload has happened).

    Deliberately never called automatically -- only when
    --sync-raw-db-with-r2 is passed (see main()) -- so Mac/Lenovo, which
    already hold the authoritative local copy, are never at risk of
    having it silently replaced by a possibly-behind R2 copy.
    """
    bucket = os.environ.get("R2_BUCKET", R2_DEFAULT_BUCKET)
    s3 = client_factory()
    if s3 is None:
        print("R2 not configured -- cannot pull raw DB, proceeding with local file as-is.")
        return False
    try:
        s3.download_file(bucket, os.path.basename(db_path), db_path)
        print(f"Pulled {db_path} from R2 bucket '{bucket}'.")
        return True
    except Exception as e:
        print(f"Could not pull {db_path} from R2 ({e}) -- if this is the very "
              f"first run of this workflow, make sure mf_nav_full.duckdb was "
              f"uploaded to R2 manually first.")
        return False


def _dbs_to_push(rc_regular: int, rc_direct: int) -> list[str]:
    """Which derived DBs are safe to publish to R2 after a rebuild pass.

    Only a plan whose own chain returned 0 (fully succeeded) gets pushed.
    rc == 2 means _rebuild_plan stopped early for manual review -- not
    confirmed fresh, so publishing it risks quietly overwriting the last
    known-good copy the hosted app is serving with something unverified.
    rc == 1 (a hard failure) is an even clearer case not to push.
    """
    to_push = []
    if rc_regular == 0:
        to_push.append(REGULAR_ANALYSIS_DB)
    if rc_direct == 0:
        to_push.append(DIRECT_ANALYSIS_DB)
    return to_push


def rebuild_all_derived_analysis(src_db: str, notify_fn=notify, push_fn=push_to_r2) -> int:
    """Rebuilds both derived analysis DBs (Regular and Direct plan) under
    one shared lock file, so streamlit_app.py's staleness check sees a
    single "rebuild in progress" window covering both -- not two separate
    lock/unlock cycles with a gap between them where a concurrent trigger
    could sneak in. The R2 push (see _dbs_to_push) happens inside that same
    lock window too, so a Streamlit-triggered rebuild can't kick off while
    a prior pass is still uploading.

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

        to_push = _dbs_to_push(rc_regular, rc_direct)
        if to_push:
            push_fn(to_push, notify_fn=notify_fn)

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

    # Case 4: only DBs whose OWN chain fully succeeded get pushed
    check("both succeed -> both pushed",
          _dbs_to_push(0, 0) == [REGULAR_ANALYSIS_DB, DIRECT_ANALYSIS_DB])
    check("regular fails -> only direct pushed",
          _dbs_to_push(1, 0) == [DIRECT_ANALYSIS_DB])
    check("direct needs review -> only regular pushed",
          _dbs_to_push(0, 2) == [REGULAR_ANALYSIS_DB])
    check("both fail/need review -> nothing pushed",
          _dbs_to_push(1, 2) == [])

    # Case 5: push_to_r2 skips cleanly (no crash, no false-alarm notify)
    # when this machine has no R2 credentials configured
    notifications.clear()
    push_to_r2([REGULAR_ANALYSIS_DB], client_factory=lambda: None, notify_fn=fake_notify)
    check("push skips silently with no R2 config", len(notifications) == 0)

    # Case 6: a real upload failure DOES notify
    notifications.clear()
    class _FakeS3Fail:
        def upload_file(self, *a, **k):
            raise RuntimeError("network down")
    dummy_path = os.path.join(tempfile.mkdtemp(), REGULAR_ANALYSIS_DB)
    open(dummy_path, "w").close()
    push_to_r2([dummy_path], client_factory=lambda: _FakeS3Fail(), notify_fn=fake_notify)
    check("upload failure triggers a notification", len(notifications) == 1)
    check("failure notification mentions the file", dummy_path in notifications[0][1])

    # Case 7: pull_raw_db_from_r2 skips cleanly (returns False, no crash)
    # when R2 isn't configured
    ok = pull_raw_db_from_r2("mf_nav_full.duckdb", client_factory=lambda: None)
    check("raw pull returns False with no R2 config", ok is False)

    # Case 8: pull_raw_db_from_r2 succeeds and actually writes the file
    class _FakeS3PullOk:
        def download_file(self, bucket, key, path):
            open(path, "w").close()  # simulate a successful download
    pull_path = os.path.join(tempfile.mkdtemp(), "mf_nav_full.duckdb")
    ok = pull_raw_db_from_r2(pull_path, client_factory=lambda: _FakeS3PullOk())
    check("raw pull returns True on success", ok is True)
    check("raw pull actually wrote the file", os.path.exists(pull_path))

    # Case 9: pull_raw_db_from_r2 handles a download failure gracefully
    # (e.g. object doesn't exist yet in the bucket) -- no crash, just False
    class _FakeS3PullFail:
        def download_file(self, *a, **k):
            raise RuntimeError("not found")
    ok = pull_raw_db_from_r2("mf_nav_full.duckdb", client_factory=lambda: _FakeS3PullFail())
    check("raw pull failure returns False, not a crash", ok is False)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--skip-derived-rebuild", action="store_true",
                     help="only refresh mf_nav_full.duckdb, skip rebuilding both "
                          "fundeval_analysis.duckdb and fundeval_analysis_direct.duckdb")
    ap.add_argument("--skip-r2-push", action="store_true",
                     help="rebuild derived DBs locally but don't upload them to "
                          "R2 (e.g. running on a machine without R2 credentials set)")
    ap.add_argument("--sync-raw-db-with-r2", action="store_true",
                     help="pull mf_nav_full.duckdb from R2 before running, and push "
                          "it back after a successful update. For ephemeral CI "
                          "runners ONLY (e.g. GitHub Actions) -- never pass this on "
                          "Mac/Lenovo, which already hold the authoritative local copy")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()

    if a.sync_raw_db_with_r2:
        pulled = pull_raw_db_from_r2(a.db)
        if not pulled:
            # (2026-09-13) Discovered the hard way: without this check, a
            # failed pull left main() running the backfill against a
            # missing/blank local file, producing a DB with only the
            # trailing ~90-day window -- which would then have been
            # pushed back to R2, silently overwriting the real
            # 2006-present archive with it. Continuing here is strictly
            # worse than stopping, even though it means this run does
            # nothing -- a skipped update is recoverable, a destroyed
            # archive is not.
            print("ABORTING: --sync-raw-db-with-r2 was set but the raw DB could not "
                  "be pulled from R2 -- see the error above. Running anyway would "
                  "mean backfilling into an empty file and risking pushing a "
                  "90-day-only DB over the real archive. Fix the R2 connection and "
                  "rerun; nothing has been changed.")
            return 1

    rc = run(a.db)
    if rc != 0:
        return rc  # raw refresh itself failed -- don't rebuild derived DBs on bad
                    # data, and don't push a bad/partial raw DB back to R2 either

    if a.sync_raw_db_with_r2 and not a.skip_r2_push:
        push_to_r2([a.db], notify_fn=notify)

    if a.skip_derived_rebuild:
        return 0

    push_fn = (lambda *_a, **_k: None) if a.skip_r2_push else push_to_r2
    return rebuild_all_derived_analysis(a.db, push_fn=push_fn)


if __name__ == "__main__":
    sys.exit(main())