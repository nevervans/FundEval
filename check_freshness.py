"""
check_freshness.py — is the incremental updater actually keeping up?

Cron only fires if the laptop is awake at the scheduled time. This doesn't
lose data (--resume catches up whatever was missed), but it can go stale
silently on a personal laptop in a way it wouldn't on a server. Run this
occasionally to confirm the cron job is actually landing runs, not just
trust that it's configured.

Run:  python check_freshness.py --db mf_nav_full.duckdb
"""

import argparse
import datetime as dt
import sys

import nav_store


def run(db_path: str) -> int:
    con = nav_store.connect(db_path)

    last_nav = con.execute("SELECT max(nav_date) FROM nav").fetchone()[0]
    last_snap = con.execute("SELECT max(snap_date) FROM universe_snapshot").fetchone()[0]
    today = dt.date.today()

    print(f"Today:                {today}")
    print(f"Latest NAV date:      {last_nav}  ({(today - last_nav).days} days old)")
    print(f"Latest universe date: {last_snap}  ({(today - last_snap).days} days old)")
    print()

    # Recent refresh_log entries -- confirms cron is actually invoking the
    # script, not just that the crontab entry exists.
    recent = con.execute("""
        SELECT run_at, ok, message FROM refresh_log
        WHERE source = 'amfi_history'
        ORDER BY run_at DESC LIMIT 5
    """).fetchall()
    print("Last 5 refresh_log entries:")
    if not recent:
        print("  (none at all -- has the backfill ever run?)")
    for run_at, ok, message in recent:
        status = "ok" if ok else "FAILED"
        print(f"  {run_at}  [{status}]  {message}")

    print()
    staleness = (today - last_snap).days if last_snap else None
    if staleness is None:
        print("VERDICT: no universe data at all.")
    elif staleness <= 2:
        print("VERDICT: current. Cron is keeping up.")
    elif staleness <= 5:
        print("VERDICT: a few days behind -- normal if a weekend or a "
              "missed cron cycle just passed. Worth another check in a day or two.")
    else:
        print(f"VERDICT: {staleness} days stale. Cron likely isn't firing "
              f"(laptop asleep at 7pm repeatedly?) or is erroring silently. "
              f"Check backfill.log and crontab -l.")

    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=nav_store.DB_DEFAULT)
    a = ap.parse_args()
    return run(a.db)


if __name__ == "__main__":
    sys.exit(main())
