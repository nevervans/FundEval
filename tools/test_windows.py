"""
test_windows.py — regression tests for tools/windows.py

Plain assertion script, not pytest -- run directly:
    python3 tools/test_windows.py

Covers three things caught by hand during manual testing this session,
that shouldn't need catching by hand again:

  1. Anchor matching finds the nearest NAV on EITHER side of the target
     date. The original bug: ASOF only searched backward, so a fund with
     its only nearby data slightly AFTER the target was wrongly reported
     as "insufficient history."

  2. The dead-fund boundary is exactly STALE_TOLERANCE_DAYS -- one day
     under is alive, one day over is dead, and the death year is read
     off the fund's actual last NAV date.

  3. category_snapshot never returns a duplicate fund_id, even when
     fund_map carries more than one row per fund_id (confirmed on
     INF251K01894 -- pre/post Baroda-BNP-Paribas-merger name rows under
     one fund_id).

Uses a synthetic in-memory DB so these are fast, deterministic, and
don't depend on the live database's current contents or freshness.
"""

import datetime as dt

import duckdb

from windows import (
    fund_window,
    category_snapshot,
    portfolio_metrics,
    rank_among_peers,
    resolve_fund_query,
    STALE_TOLERANCE_DAYS,
    _nearest_anchor,
)
import pandas as pd

results = []


def check(name, condition):
    results.append((name, condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}")


def make_test_db():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE nav_fund (fund_id VARCHAR, d DATE, nav DOUBLE)")
    con.execute(
        """
        CREATE TABLE fund_map (
            scheme_code INTEGER, scheme_name VARCHAR, category VARCHAR,
            plan VARCHAR, option_type VARCHAR, fund_id VARCHAR, no_isin BOOLEAN
        )
        """
    )
    return con


def seed_daily_navs(con, fund_id, start, end, start_nav=10.0, daily_growth=0.0002):
    d = start
    nav = start_nav
    rows = []
    while d <= end:
        rows.append((fund_id, d, nav))
        nav *= 1 + daily_growth
        d += dt.timedelta(days=1)
    con.executemany("INSERT INTO nav_fund VALUES (?, ?, ?)", rows)


def test_anchor_symmetry():
    con = make_test_db()

    # FWD_ONLY: no NAV before the target date, only a bit after -- the
    # direction the original ASOF-only code could NOT find.
    seed_daily_navs(con, "FWD_ONLY", dt.date(2020, 1, 20), dt.date(2020, 6, 1))
    target = dt.date(2020, 1, 10)
    row = _nearest_anchor(con, "FWD_ONLY", target, tolerance_days=45)
    check(
        "anchor matching finds a NAV AFTER the target (the original bug)",
        row is not None and row[0] == dt.date(2020, 1, 20),
    )

    # BACK_ONLY: no NAV after the target date, only before -- the
    # direction the original code already handled. Must still work.
    seed_daily_navs(con, "BACK_ONLY", dt.date(2019, 6, 1), dt.date(2020, 1, 5))
    target2 = dt.date(2020, 1, 15)
    row2 = _nearest_anchor(con, "BACK_ONLY", target2, tolerance_days=45)
    check(
        "anchor matching finds a NAV BEFORE the target (non-regression)",
        row2 is not None and row2[0] == dt.date(2020, 1, 5),
    )
    con.close()


def test_death_boundary():
    con = make_test_db()
    asof = dt.date(2026, 1, 1)

    con.execute(
        "INSERT INTO fund_map VALUES (1, 'Alive-ish Fund', 'Test Category', 'REGULAR', 'GROWTH', 'ALIVE_EDGE', False)"
    )
    last_date_alive = asof - dt.timedelta(days=STALE_TOLERANCE_DAYS - 1)
    seed_daily_navs(con, "ALIVE_EDGE", last_date_alive - dt.timedelta(days=4000), last_date_alive)
    stats = fund_window(con, "ALIVE_EDGE", asof, years=10)
    check(
        f"fund with last NAV {STALE_TOLERANCE_DAYS - 1}d before asof is NOT dead",
        stats is not None and stats["is_dead"] is False,
    )

    con.execute(
        "INSERT INTO fund_map VALUES (2, 'Dead-ish Fund', 'Test Category', 'REGULAR', 'GROWTH', 'DEAD_EDGE', False)"
    )
    last_date_dead = asof - dt.timedelta(days=STALE_TOLERANCE_DAYS + 1)
    seed_daily_navs(con, "DEAD_EDGE", last_date_dead - dt.timedelta(days=4000), last_date_dead)
    stats2 = fund_window(con, "DEAD_EDGE", asof, years=10)
    check(
        f"fund with last NAV {STALE_TOLERANCE_DAYS + 1}d before asof IS dead",
        stats2 is not None
        and stats2["is_dead"] is True
        and stats2["death_year"] == last_date_dead.year,
    )
    con.close()


def test_no_duplicate_peers():
    con = make_test_db()
    asof = dt.date(2026, 1, 1)

    # DUP_FUND: two fund_map rows, same fund_id + category, different
    # scheme_name/scheme_code -- the exact shape of the INF251K01894 bug.
    con.execute(
        "INSERT INTO fund_map VALUES (100, 'Old Name Fund', 'Test Category', 'REGULAR', 'GROWTH', 'DUP_FUND', False)"
    )
    con.execute(
        "INSERT INTO fund_map VALUES (200, 'New Name Fund', 'Test Category', 'REGULAR', 'GROWTH', 'DUP_FUND', False)"
    )
    con.execute(
        "INSERT INTO fund_map VALUES (300, 'Solo Fund', 'Test Category', 'REGULAR', 'GROWTH', 'SOLO_FUND', False)"
    )
    seed_daily_navs(con, "DUP_FUND", dt.date(2010, 1, 1), asof)
    seed_daily_navs(con, "SOLO_FUND", dt.date(2010, 1, 1), asof)

    peers, meta = category_snapshot(con, "Test Category", asof, years=10)
    fund_ids = list(peers["fund_id"])
    check(
        "category_snapshot returns each fund_id at most once",
        len(fund_ids) == len(set(fund_ids)),
    )
    check(
        "the duplicated fund is still counted ONCE, not dropped entirely",
        "DUP_FUND" in fund_ids,
    )
    con.close()


def seed_alternating_navs(con, fund_id, start, end, amplitude=0.01, sign=1, start_nav=10.0):
    """Deterministic oscillating NAV series -- unlike seed_daily_navs'
    smooth compounding, this has genuine day-to-day variance, which
    correlation/vol tests need. sign=-1 produces the exact day-by-day
    NEGATIVE of sign=1's returns, not just an uncorrelated series --
    that's what makes the opposite-fund test exact rather than
    approximate."""
    d = start
    nav = start_nav
    rows = []
    i = 0
    while d <= end:
        rows.append((fund_id, d, nav))
        step = amplitude if i % 2 == 0 else -amplitude
        nav *= 1 + sign * step
        i += 1
        d += dt.timedelta(days=1)
    con.executemany("INSERT INTO nav_fund VALUES (?, ?, ?)", rows)


def test_portfolio_correlation_and_diversification():
    con = make_test_db()
    start, end = dt.date(2020, 1, 1), dt.date(2022, 12, 31)

    # Identical return streams: correlation should be exactly 1.0, and
    # combining them should show NO diversification benefit (ratio ~1.0)
    # -- diversification comes from funds moving differently, not from
    # having two of them.
    seed_alternating_navs(con, "TWIN_A", start, end)
    seed_alternating_navs(con, "TWIN_B", start, end)
    r1 = portfolio_metrics(con, ["TWIN_A", "TWIN_B"], {"TWIN_A": 0.5, "TWIN_B": 0.5}, start, end)
    check(
        "identical funds show correlation ~1.0",
        r1 is not None and abs(r1["corr"].loc["TWIN_A", "TWIN_B"] - 1.0) < 1e-6,
    )
    check(
        "identical funds show NO diversification benefit (ratio ~1.0)",
        r1 is not None and abs(r1["diversification_ratio"] - 1.0) < 1e-6,
    )

    # Exact opposite return streams: correlation should be exactly -1.0,
    # and at 50/50 weight the portfolio's daily return is exactly zero
    # every day -- diversification_ratio should be None (guarded
    # division by a ~zero portfolio vol), not a crash.
    seed_alternating_navs(con, "OPP_A", start, end, sign=1)
    seed_alternating_navs(con, "OPP_B", start, end, sign=-1)
    r2 = portfolio_metrics(con, ["OPP_A", "OPP_B"], {"OPP_A": 0.5, "OPP_B": 0.5}, start, end)
    check(
        "perfectly opposite funds show correlation ~-1.0",
        r2 is not None and r2["corr"].loc["OPP_A", "OPP_B"] < -0.999999,
    )
    check(
        "perfectly opposite funds at 50/50 nearly cancel portfolio vol",
        r2 is not None and r2["portfolio_vol"] < 1e-6,
    )
    check(
        "near-zero portfolio vol doesn't crash the diversification ratio",
        r2 is not None and r2["diversification_ratio"] is None,
    )
    con.close()


def test_rank_among_peers_self_noise():
    """The exact bug found on INF879O01019 (Parag Parikh Flexi Cap), 10y
    Sharpe: category_snapshot recomputes this fund's own value via a
    different (vectorized) code path than fund_window's (scalar) one,
    and the two can disagree by floating-point noise even for identical
    inputs. Comparing a fund against that recomputed copy of itself
    instead of excluding it produced an impossible rank of "0/15" for a
    fund that was genuinely #1."""
    peers_noise_low = pd.DataFrame({
        "fund_id": ["SUBJECT", "B", "C"],
        "value": [0.10 - 1e-15, 0.05, 0.03],  # SUBJECT's own peers-row sits
    })                                          # a hair BELOW its true value
    rank, n_peers = rank_among_peers(peers_noise_low, "value", "SUBJECT", 0.10)
    check(
        "self-row noised slightly LOW still gives the best fund rank 1, not 0",
        rank == 1 and n_peers == 3,
    )

    peers_noise_high = pd.DataFrame({
        "fund_id": ["SUBJECT", "B", "C"],
        "value": [0.10 + 1e-15, 0.05, 0.03],  # self-row noised HIGH instead
    })
    rank2, _ = rank_among_peers(peers_noise_high, "value", "SUBJECT", 0.10)
    check(
        "self-row noised slightly HIGH still gives the best fund rank 1, not 2",
        rank2 == 1,
    )

    peers_tie = pd.DataFrame({
        "fund_id": ["SUBJECT", "B", "C"],
        "value": [0.10, 0.10, 0.03],  # B genuinely ties SUBJECT
    })
    rank3, _ = rank_among_peers(peers_tie, "value", "SUBJECT", 0.10)
    check(
        "a genuine tie with another (non-self) fund still gives rank 1",
        rank3 == 1,
    )


def test_word_based_name_matching():
    """The exact real-world case: ICICI Prudential Bluechip Fund's
    SEBI-2018-mandated rename to "...Large Cap Fund (erstwhile
    Bluechip Fund)" put a contiguous "prudential bluechip" search out
    of reach -- the words are both present but not adjacent. Confirmed
    on the real data: the old contiguous-phrase search silently matched
    ONLY a dead institutional share class, with no ambiguity warning."""
    con = make_test_db()
    con.execute(
        "INSERT INTO fund_map VALUES (1, "
        "'ICICI Prudential Large Cap Fund (erstwhile Bluechip Fund) - Growth', "
        "'Equity Scheme - Large Cap Fund', 'REGULAR', 'GROWTH', 'ICICI_LIVE', False)"
    )
    con.execute(
        "INSERT INTO fund_map VALUES (2, "
        "'ICICI Prudential Bluechip Fund - Institutional Option - I - Growth', "
        "'Equity Scheme - Large Cap Fund', 'REGULAR', 'GROWTH', 'ICICI_DEAD', False)"
    )

    matches = resolve_fund_query(con, "icici prudential bluechip", by_isin=False)
    fund_ids = {m[0] for m in matches}
    check(
        "word-based matching finds the renamed LIVE fund (missed by contiguous phrase)",
        "ICICI_LIVE" in fund_ids,
    )
    check(
        "word-based matching also surfaces the dead fund -- ambiguity flagged, not silently hidden",
        "ICICI_DEAD" in fund_ids,
    )

    narrowed = resolve_fund_query(con, "icici prudential large cap", by_isin=False)
    narrowed_ids = {m[0] for m in narrowed}
    check(
        "adding a distinguishing word narrows the match to just the live fund",
        narrowed_ids == {"ICICI_LIVE"},
    )
    con.close()


def test_word_boundary_avoids_compound_false_positive():
    """Confirmed real near-miss: fixing the ICICI rename case with plain
    per-word substring matching immediately broke something else --
    "sbi large cap" started also matching "SBI LARGE & MIDCAP FUND",
    because "cap" is a bare substring of "midcap" even though it's a
    different, unrelated product (a large-and-mid-cap blend, not a pure
    large-cap fund). Word-boundary matching must reject that WITHOUT
    reintroducing the original ICICI miss."""
    con = make_test_db()
    con.execute(
        "INSERT INTO fund_map VALUES (1, 'SBI Large Cap FUND-REGULAR PLAN GROWTH', "
        "'Equity Scheme - Large Cap Fund', 'REGULAR', 'GROWTH', 'SBI_LARGECAP', False)"
    )
    con.execute(
        "INSERT INTO fund_map VALUES (2, 'SBI LARGE & MIDCAP FUND- REGULAR PLAN -Growth', "
        "'Equity Scheme - Large & Mid Cap Fund', 'REGULAR', 'GROWTH', 'SBI_LARGEMID', False)"
    )
    matches = resolve_fund_query(con, "sbi large cap", by_isin=False)
    fund_ids = {m[0] for m in matches}
    check(
        "'sbi large cap' matches the pure large-cap fund",
        "SBI_LARGECAP" in fund_ids,
    )
    check(
        "'sbi large cap' does NOT match via a bare 'cap' substring inside 'midcap'",
        "SBI_LARGEMID" not in fund_ids,
    )
    con.close()


if __name__ == "__main__":
    test_anchor_symmetry()
    test_death_boundary()
    test_no_duplicate_peers()
    test_portfolio_correlation_and_diversification()
    test_rank_among_peers_self_noise()
    test_word_based_name_matching()
    test_word_boundary_avoids_compound_false_positive()

    failed = [name for name, ok in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} FAILED: {failed}")
        raise SystemExit(1)
    print(f"All {len(results)} checks passed.")