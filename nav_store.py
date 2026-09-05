"""
nav_store.py — persistence layer for the MF NAV toolset.

Owns the database. Knows nothing about HTTP, Streamlit, or return math.
The existing refresh_nav.py keeps owning the fetch; this module keeps the data.

Schema
------
scheme            one row per AMFI code, with point-in-time-able status fields
nav               (amfi_code, nav_date) -> nav.  The fact table.
universe_snapshot (snap_date, amfi_code).  Observed universe on a given day.
                  Presence/absence here is what makes point-in-time selection
                  a join instead of a judgement call.
refresh_log       one row per fetch attempt, success or failure.

Usage
-----
    python nav_store.py --selftest       # offline, no network, no real DB
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

import duckdb
import pandas as pd

DB_DEFAULT = "mf_nav_full.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheme (
    amfi_code        INTEGER PRIMARY KEY,
    scheme_name      VARCHAR NOT NULL,
    fund_house       VARCHAR,
    scheme_type      VARCHAR,
    category         VARCHAR,          -- CURRENT category. Not historical. See note.
    plan             VARCHAR,          -- DIRECT | REGULAR | UNKNOWN
    option           VARCHAR,          -- GROWTH | IDCW | UNKNOWN
    isin_growth      VARCHAR,
    isin_div         VARCHAR,
    first_nav_date   DATE,
    last_nav_date    DATE,
    status           VARCHAR DEFAULT 'ACTIVE',   -- ACTIVE | STALE | WOUND_UP | MERGED
    successor_code   INTEGER,          -- where units went, if MERGED
    source           VARCHAR,          -- mfapi | amfi
    updated_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS nav (
    amfi_code  INTEGER NOT NULL,
    nav_date   DATE    NOT NULL,
    nav        DOUBLE  NOT NULL,
    PRIMARY KEY (amfi_code, nav_date)
);

CREATE TABLE IF NOT EXISTS universe_snapshot (
    snap_date  DATE    NOT NULL,
    amfi_code  INTEGER NOT NULL,
    PRIMARY KEY (snap_date, amfi_code)
);

CREATE TABLE IF NOT EXISTS refresh_log (
    run_at      TIMESTAMP,
    amfi_code   INTEGER,
    source      VARCHAR,
    rows_seen   INTEGER,
    rows_added  INTEGER,
    ok          BOOLEAN,
    message     VARCHAR
);
"""


# ---------------------------------------------------------------- connection


def connect(path: str = DB_DEFAULT) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the store and ensure the schema exists."""
    con = duckdb.connect(path)
    con.execute(SCHEMA_SQL)
    return con


# ------------------------------------------------------------------ parsing


@dataclass
class SchemeMeta:
    amfi_code: int
    scheme_name: str
    fund_house: str | None = None
    scheme_type: str | None = None
    category: str | None = None
    isin_growth: str | None = None
    isin_div: str | None = None
    plan: str = "UNKNOWN"
    option: str = "UNKNOWN"
    source: str = "mfapi"


def classify_plan(name: str) -> str:
    n = name.lower()
    if "direct" in n:
        return "DIRECT"
    if "regular" in n:
        return "REGULAR"
    return "UNKNOWN"


def classify_option(name: str) -> str:
    """
    IDCW is checked first: names like '... - IDCW - Growth Option' exist, and a
    payout/reinvestment marker is the stronger signal of what the series is.

    "income distribution" / "capital withdrawal" catch SEBI's 2021-mandated
    replacement wording for "Dividend" (confirmed 2026-08-30: schemes named
    "...Income Distribution CUM Capital Withdrawal Option" were falling
    through to GROWTH purely because the FUND's own name contained "Growth"
    as an investment-style descriptor, e.g. "Multi Cap Growth Fund" -- an
    unrelated use of the word, nothing to do with the option type).
    """
    n = name.lower()
    for token in ("idcw", "dividend", "payout", "reinvest", "bonus",
                  "income distribution", "capital withdrawal"):
        if token in n:
            return "IDCW"
    if "growth" in n:
        return "GROWTH"
    return "UNKNOWN"


def parse_date(s: str) -> dt.date:
    """mfapi serves DD-MM-YYYY. AMFI serves DD-MMM-YYYY."""
    s = s.strip()
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def parse_mfapi_payload(payload: dict) -> tuple[SchemeMeta, list[tuple[dt.date, float]]]:
    """
    Turn a raw /mf/{code} response into (meta, rows).

    Skips rows whose NAV is non-numeric or zero. AMFI emits 'N.A.' and
    sometimes 0.00000 for non-dealing days; both would poison a return series.
    """
    meta_raw = payload.get("meta") or {}
    code = meta_raw.get("scheme_code")
    if code is None:
        raise ValueError("payload has no scheme_code")
    name = meta_raw.get("scheme_name") or ""

    meta = SchemeMeta(
        amfi_code=int(code),
        scheme_name=name,
        fund_house=meta_raw.get("fund_house"),
        scheme_type=meta_raw.get("scheme_type"),
        category=meta_raw.get("scheme_category"),
        isin_growth=meta_raw.get("isin_growth"),
        isin_div=meta_raw.get("isin_div_reinvestment"),
        plan=classify_plan(name),
        option=classify_option(name),
    )

    rows: list[tuple[dt.date, float]] = []
    seen: set[dt.date] = set()
    for r in payload.get("data") or []:
        try:
            v = float(r["nav"])
        except (TypeError, ValueError, KeyError):
            continue
        if v <= 0:
            continue
        try:
            d = parse_date(r["date"])
        except (ValueError, KeyError):
            continue
        if d in seen:          # mfapi has been known to repeat a date
            continue
        seen.add(d)
        rows.append((d, v))

    rows.sort(key=lambda t: t[0])
    return meta, rows


# ------------------------------------------------------------------ writing


def upsert_scheme(con, meta: SchemeMeta) -> None:
    bulk_upsert_schemes(con, [meta])


def bulk_upsert_schemes(con, metas: Sequence[SchemeMeta]) -> None:
    """
    Same upsert as upsert_scheme, for many schemes in one native SQL pass.

    IMPORTANT: this registers a pandas DataFrame and runs INSERT...SELECT,
    NOT executemany(). Benchmarked directly (2026-08-29): executemany's
    per-row ON CONFLICT check costs ~2.6ms/row and gets far worse at tens of
    thousands of rows in one call -- looping upsert_scheme() per code was
    measured taking 5-7 minutes for one AMFI history window (~8,700 schemes).
    The DataFrame-register + native INSERT...SELECT path does the equivalent
    565,000-row nav upsert in under a second: DuckDB's own vectorized engine
    handles the conflict resolution instead of the Python driver looping
    row-by-row under the hood of what looks like a single call.
    """
    if not metas:
        return
    df = pd.DataFrame(
        [(m.amfi_code, m.scheme_name, m.fund_house, m.scheme_type, m.category,
          m.plan, m.option, m.isin_growth, m.isin_div, m.source) for m in metas],
        columns=["amfi_code", "scheme_name", "fund_house", "scheme_type",
                "category", "plan", "option", "isin_growth", "isin_div", "source"],
    )
    con.register("_bulk_scheme_staging", df)
    try:
        con.execute(
            """
            INSERT INTO scheme (amfi_code, scheme_name, fund_house, scheme_type,
                                category, plan, option, isin_growth, isin_div,
                                source, updated_at)
            SELECT amfi_code, scheme_name, fund_house, scheme_type, category,
                   plan, option, isin_growth, isin_div, source, now()
            FROM _bulk_scheme_staging
            ON CONFLICT (amfi_code) DO UPDATE SET
                scheme_name = excluded.scheme_name,
                fund_house  = excluded.fund_house,
                scheme_type = excluded.scheme_type,
                category    = excluded.category,
                plan        = excluded.plan,
                option      = excluded.option,
                isin_growth = excluded.isin_growth,
                isin_div    = excluded.isin_div,
                source      = excluded.source,
                updated_at  = excluded.updated_at
            """
        )
    finally:
        con.unregister("_bulk_scheme_staging")


def isin_group(con, isin_growth: str):
    """
    All amfi_codes ever seen sharing this ISIN, oldest-first by first_nav_date.

    This is the real identity join. A scheme_code can change on a pure
    AMC-transition rename with zero economic disruption (confirmed 2026-08:
    L&T Mid Cap Fund code 119807 and HSBC Midcap Fund code 151036 share ISIN
    INF917K01FZ1 -- same security, no conversion ratio, code changed anyway).
    Splicing a continuous long-run return series means chaining across codes
    that share an ISIN, not trusting any single code to carry full history.

    Guards against a genuine (if rare) AMFI data anomaly, confirmed
    2026-08-30: a closed-ended FMP series (DWS -> DHFL Pramerica Hybrid
    Fixed Term Fund, Series 9) where a Growth-option code and an
    IDCW-option code ended up sharing one ISIN despite being fundamentally
    different, incompatible payout mechanics. UNKNOWN never counts as a
    conflict -- it just means "predates the Direct/Regular distinction"
    (pre-2013 scheme names, e.g. the confirmed ING -> Aditya Birla Sun Life
    and Principal -> Sundaram rename chains) -- only two differing
    NON-UNKNOWN option values do. When a real conflict exists, returns only
    the self-consistent majority subgroup by option rather than silently
    blending two incompatible series together.
    """
    if not isin_growth:
        return []
    rows = con.execute(
        """
        SELECT amfi_code, option FROM scheme
        WHERE isin_growth = ?
        ORDER BY first_nav_date NULLS LAST
        """,
        [isin_growth],
    ).fetchall()
    if len(rows) <= 1:
        return [r[0] for r in rows]

    known_options = {o for _, o in rows if o and o != "UNKNOWN"}
    if len(known_options) <= 1:
        return [r[0] for r in rows]        # no conflict -- UNKNOWN or unanimous

    from collections import Counter
    majority = Counter(o for _, o in rows if o and o != "UNKNOWN").most_common(1)[0][0]
    return [code for code, o in rows if not o or o == "UNKNOWN" or o == majority]


def get_spliced_series(con, isin_growth: str):
    """
    Continuous NAV series for an ISIN across every scheme_code it has ever
    been filed under. Use this for any return calculation spanning an AMC
    transition -- get_nav_series() alone silently truncates at the boundary.
    """
    codes = isin_group(con, isin_growth)
    if not codes:
        return con.execute("SELECT * FROM nav WHERE false").df()
    placeholders = ",".join("?" * len(codes))
    return con.execute(
        f"""
        SELECT nav_date, nav FROM nav
        WHERE amfi_code IN ({placeholders})
        ORDER BY nav_date
        """,
        codes,
    ).df()


def upsert_navs(con, amfi_code: int, rows: Sequence[tuple[dt.date, float]]) -> int:
    """Insert NAV rows idempotently. Returns count of genuinely new rows."""
    if not rows:
        return 0
    before = con.execute(
        "SELECT count(*) FROM nav WHERE amfi_code = ?", [amfi_code]
    ).fetchone()[0]
    con.executemany(
        """
        INSERT INTO nav (amfi_code, nav_date, nav) VALUES (?,?,?)
        ON CONFLICT (amfi_code, nav_date) DO UPDATE SET nav = excluded.nav
        """,
        [(amfi_code, d, v) for d, v in rows],
    )
    after = con.execute(
        "SELECT count(*) FROM nav WHERE amfi_code = ?", [amfi_code]
    ).fetchone()[0]
    _refresh_bounds(con, amfi_code)
    return after - before


def bulk_upsert_navs(con, rows: Sequence[tuple[int, dt.date, float]]) -> int:
    """
    rows: (amfi_code, nav_date, nav) tuples, ANY MIX of codes, one native pass.

    Registers a pandas DataFrame and runs INSERT...SELECT rather than
    executemany() -- see bulk_upsert_schemes' docstring for why. Benchmarked
    at 565,000 rows / 8,678 codes: under 1 second, versus 5-7 minutes for
    the equivalent per-code upsert_navs() loop.

    Does not return an exact new-vs-updated split (that would need the same
    per-row counting this exists to avoid) -- returns rows attempted. Good
    enough for a backfill's progress log; use upsert_navs() on a single code
    when the precise count matters.
    """
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["amfi_code", "nav_date", "nav"])
    con.register("_bulk_nav_staging", df)
    try:
        con.execute(
            """
            INSERT INTO nav SELECT * FROM _bulk_nav_staging
            ON CONFLICT (amfi_code, nav_date) DO UPDATE SET nav = excluded.nav
            """
        )
    finally:
        con.unregister("_bulk_nav_staging")
    refresh_all_bounds(con)
    return len(rows)


def refresh_all_bounds(con) -> None:
    """
    Recompute first_nav_date/last_nav_date for every scheme in one pass.
    A single GROUP BY aggregate over the whole nav table is a vectorized
    DuckDB operation -- fast even at millions of rows -- versus thousands
    of individual per-code correlated-subquery updates.
    """
    con.execute(
        """
        UPDATE scheme SET
            first_nav_date = b.lo,
            last_nav_date  = b.hi
        FROM (SELECT amfi_code, min(nav_date) lo, max(nav_date) hi
              FROM nav GROUP BY amfi_code) b
        WHERE scheme.amfi_code = b.amfi_code
        """
    )


def _refresh_bounds(con, amfi_code: int) -> None:
    con.execute(
        """
        UPDATE scheme SET
            first_nav_date = b.lo,
            last_nav_date  = b.hi
        FROM (SELECT min(nav_date) lo, max(nav_date) hi
              FROM nav WHERE amfi_code = ?) b
        WHERE scheme.amfi_code = ?
        """,
        [amfi_code, amfi_code],
    )


def record_universe(con, snap_date: dt.date, codes: Iterable[int]) -> int:
    codes = list({int(c) for c in codes})
    if not codes:
        return 0
    con.executemany(
        "INSERT INTO universe_snapshot VALUES (?,?) ON CONFLICT DO NOTHING",
        [(snap_date, c) for c in codes],
    )
    return len(codes)


def bulk_record_universe(con, rows: Sequence[tuple[dt.date, int]]) -> int:
    """
    rows: (snap_date, amfi_code) tuples, ANY MIX of dates, one native pass.
    Same executemany-vs-DataFrame-register reasoning as bulk_upsert_navs.
    """
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["snap_date", "amfi_code"])
    con.register("_bulk_universe_staging", df)
    try:
        con.execute(
            "INSERT INTO universe_snapshot SELECT * FROM _bulk_universe_staging "
            "ON CONFLICT DO NOTHING"
        )
    finally:
        con.unregister("_bulk_universe_staging")
    return len(rows)


def log_refresh(con, amfi_code, source, rows_seen, rows_added, ok, message="") -> None:
    con.execute(
        "INSERT INTO refresh_log VALUES (now(), ?,?,?,?,?,?)",
        [amfi_code, source, rows_seen, rows_added, ok, message],
    )


def mark_stale(con, as_of: dt.date, days: int = 30) -> int:
    """
    Flag schemes whose last NAV is older than `days`. STALE is a candidate set
    for manual classification into WOUND_UP or MERGED -- it is NOT a conclusion.
    Never let an automated rule write WOUND_UP; that field must be trustworthy.
    """
    cutoff = as_of - dt.timedelta(days=days)
    con.execute(
        """
        UPDATE scheme SET status = 'STALE'
        WHERE last_nav_date IS NOT NULL
          AND last_nav_date < ?
          AND status = 'ACTIVE'
        """,
        [cutoff],
    )
    return con.execute(
        "SELECT count(*) FROM scheme WHERE status = 'STALE'"
    ).fetchone()[0]


# ------------------------------------------------------------------ reading


def get_nav_series(con, amfi_code: int, start=None, end=None):
    """Ascending DataFrame[nav_date, nav]. This is what the return math eats."""
    q = "SELECT nav_date, nav FROM nav WHERE amfi_code = ?"
    args: list = [amfi_code]
    if start:
        q += " AND nav_date >= ?"
        args.append(start)
    if end:
        q += " AND nav_date <= ?"
        args.append(end)
    return con.execute(q + " ORDER BY nav_date", args).df()


def universe_as_of(con, snap_date: dt.date, plan="DIRECT", option="GROWTH"):
    """
    Codes that actually existed on snap_date. Survivorship-free by construction:
    it reads the observed snapshot, not today's fund list.
    """
    return [r[0] for r in con.execute(
        """
        SELECT u.amfi_code FROM universe_snapshot u
        JOIN scheme s USING (amfi_code)
        WHERE u.snap_date = ?
          AND (? IS NULL OR s.plan = ?)
          AND (? IS NULL OR s.option = ?)
        ORDER BY u.amfi_code
        """,
        [snap_date, plan, plan, option, option],
    ).fetchall()]


def coverage(con) -> dict:
    row = con.execute(
        """
        SELECT (SELECT count(*) FROM scheme),
               (SELECT count(*) FROM nav),
               (SELECT min(nav_date) FROM nav),
               (SELECT max(nav_date) FROM nav),
               (SELECT count(*) FROM scheme WHERE status <> 'ACTIVE')
        """
    ).fetchone()
    return dict(schemes=row[0], nav_rows=row[1], first=row[2],
                last=row[3], non_active=row[4])


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

    # --- parsing -----------------------------------------------------------
    check("date DD-MM-YYYY", parse_date("25-08-2026") == dt.date(2026, 8, 25))
    check("date DD-MMM-YYYY", parse_date("25-Aug-2026") == dt.date(2026, 8, 25))
    check("plan direct", classify_plan("SBI SMALL CAP FUND - Direct Plan - Growth") == "DIRECT")
    check("plan regular", classify_plan("HDFC Top 100 - Regular Plan - Growth") == "REGULAR")
    check("plan unknown", classify_plan("Some Old Scheme - Growth") == "UNKNOWN")
    check("option growth", classify_option("X - Direct Plan - Growth") == "GROWTH")
    check("option idcw beats growth",
          classify_option("X - Direct - IDCW - Growth Option") == "IDCW")
    check("option dividend", classify_option("X - Dividend Payout") == "IDCW")
    # regression: "Income Distribution cum Capital Withdrawal" is SEBI's
    # 2021 replacement wording for "Dividend" -- must be IDCW even when the
    # FUND's own name contains "Growth" as an investment-style descriptor
    # (found 2026-08-30: "Principal Multi Cap Growth Fund- Half Yearly
    # Income Distribution CUM Capital Withdrawal Option" was misclassifying
    # as GROWTH, purely from "Growth" in "Multi Cap Growth Fund").
    check("IDCW via 'income distribution' wording, despite 'Growth' in fund name",
          classify_option("Principal Multi Cap Growth Fund- Half Yearly "
                          "Income Distribution CUM Capital Withdrawal Option") == "IDCW")
    check("IDCW via 'capital withdrawal' alone",
          classify_option("Some Fund - Capital Withdrawal Option") == "IDCW")

    payload = {
        "meta": {"fund_house": "SBI Mutual Fund",
                 "scheme_type": "Open Ended Schemes",
                 "scheme_category": "Equity Scheme - Small Cap Fund",
                 "scheme_code": 125497,
                 "scheme_name": "SBI SMALL CAP FUND - Direct Plan - Growth",
                 "isin_growth": "INF200K01T51",
                 "isin_div_reinvestment": None},
        "data": [{"date": "25-08-2026", "nav": "214.88810"},
                 {"date": "24-08-2026", "nav": "213.10000"},
                 {"date": "23-08-2026", "nav": "N.A."},      # junk
                 {"date": "22-08-2026", "nav": "0.00000"},   # junk
                 {"date": "22-08-2026", "nav": "212.00000"},
                 {"date": "not-a-date", "nav": "1.0"}],      # junk
        "status": "SUCCESS",
    }
    meta, rows = parse_mfapi_payload(payload)
    check("meta code", meta.amfi_code == 125497)
    check("meta plan/option", (meta.plan, meta.option) == ("DIRECT", "GROWTH"))
    check("junk rows dropped", len(rows) == 3)
    check("rows ascending", [r[0] for r in rows] == sorted(r[0] for r in rows))
    check("zero nav excluded", all(v > 0 for _, v in rows))

    # a duplicate date must not survive parsing (PK would silently overwrite)
    dupe = {"meta": {"scheme_code": 1, "scheme_name": "X - Direct - Growth"},
            "data": [{"date": "01-01-2026", "nav": "10"},
                     {"date": "01-01-2026", "nav": "99"}]}
    _, drows = parse_mfapi_payload(dupe)
    check("dupe date collapsed", len(drows) == 1 and drows[0][1] == 10.0)

    # --- store -------------------------------------------------------------
    tmp = os.path.join(tempfile.mkdtemp(), "t.duckdb")
    con = connect(tmp)
    upsert_scheme(con, meta)
    added = upsert_navs(con, meta.amfi_code, rows)
    check("first insert counts", added == 3)

    again = upsert_navs(con, meta.amfi_code, rows)
    check("reimport is idempotent", again == 0)

    bounds = con.execute(
        "SELECT first_nav_date, last_nav_date FROM scheme WHERE amfi_code=?",
        [meta.amfi_code]).fetchone()
    check("bounds tracked",
          bounds == (dt.date(2026, 8, 22), dt.date(2026, 8, 25)))

    ser = get_nav_series(con, meta.amfi_code)
    check("series ascending", list(ser["nav_date"]) == sorted(ser["nav_date"]))
    check("series length", len(ser) == 3)

    # scheme metadata changes (rename) must not duplicate the row
    meta2 = SchemeMeta(amfi_code=125497, scheme_name="SBI Small Cap Fund - Direct Growth",
                       category="Equity Scheme - Small Cap Fund", plan="DIRECT",
                       option="GROWTH")
    upsert_scheme(con, meta2)
    check("rename does not duplicate",
          con.execute("SELECT count(*) FROM scheme").fetchone()[0] == 1)

    # universe snapshots
    record_universe(con, dt.date(2026, 8, 25), [125497, 999999])
    record_universe(con, dt.date(2026, 8, 25), [125497])       # replay
    check("snapshot idempotent",
          con.execute("SELECT count(*) FROM universe_snapshot").fetchone()[0] == 2)
    check("universe filters to direct growth",
          universe_as_of(con, dt.date(2026, 8, 25)) == [125497])
    check("unknown date -> empty universe",
          universe_as_of(con, dt.date(2020, 1, 1)) == [])

    # staleness
    con.execute("INSERT INTO scheme (amfi_code, scheme_name, last_nav_date, status) "
                "VALUES (777, 'Dead Fund - Direct - Growth', DATE '2020-04-23', 'ACTIVE')")
    n = mark_stale(con, dt.date(2026, 8, 25), days=30)
    check("stale flagged", n == 1)
    check("live scheme untouched",
          con.execute("SELECT status FROM scheme WHERE amfi_code=125497"
                      ).fetchone()[0] == 'ACTIVE')

    log_refresh(con, 125497, "mfapi", len(rows), added, True, "")
    check("log written",
          con.execute("SELECT count(*) FROM refresh_log").fetchone()[0] == 1)

    cov = coverage(con)
    check("coverage counts", cov["schemes"] == 2 and cov["nav_rows"] == 3)

    # isin_group conflict resolution: replicates the real DWS -> DHFL
    # Pramerica Hybrid Fixed Term Fund Series 9 case (2026-08-30) -- a
    # Growth-option code and an IDCW-option code genuinely sharing one ISIN.
    # Splicing them would blend two incompatible payout mechanics into one
    # nonsensical series.
    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option, isin_growth, first_nav_date) VALUES
        (117882, 'DWS Hybrid FTF Series 9 - Regular Dividend (Payout)', 'REGULAR', 'IDCW', 'CONFLICTISIN01', DATE '2015-01-01'),
        (138549, 'DHFL Pramerica Hybrid FTF Series 9 - Growth', 'UNKNOWN', 'GROWTH', 'CONFLICTISIN01', DATE '2017-01-01')
    """)
    conflict_group = isin_group(con, "CONFLICTISIN01")
    check("genuine option conflict excludes the minority, not blended together",
          conflict_group == [117882])   # majority by count; only 1 IDCW vs 1 GROWTH,
                                         # tie broken by Counter.most_common ordering,
                                         # but the key property is it picks ONE, not both

    # UNKNOWN must never count as a conflict -- confirmed real cases (ING ->
    # Aditya Birla Sun Life, Principal -> Sundaram) are pre-2013 schemes
    # whose old name has no "Regular" label to detect, correctly UNKNOWN.
    con.execute("""
        INSERT INTO scheme (amfi_code, scheme_name, plan, option, isin_growth, first_nav_date) VALUES
        (200001, 'ING Old Fund - Growth Option', 'UNKNOWN', 'GROWTH', 'RENAMEISIN01', DATE '2005-01-01'),
        (200002, 'ABSL New Fund - Regular Plan - Growth Option', 'REGULAR', 'GROWTH', 'RENAMEISIN01', DATE '2014-01-01')
    """)
    check("UNKNOWN-vs-known is not a conflict -- both codes kept",
          set(isin_group(con, "RENAMEISIN01")) == {200001, 200002})

    con.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.coverage:
        con = connect(a.db)
        for k, v in coverage(con).items():
            print(f"{k:>12}: {v}")
        con.close()
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
