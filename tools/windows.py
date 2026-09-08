"""
windows.py — shared trailing-return window logic for FundEval analyst tools.

Used by both fund_lookup.py (single-fund one-pager) and compare_funds.py
(N-fund comparison). Pulled out of fund_lookup.py so window/return logic
lives in exactly one place.

ANCHOR MATCHING: nearest NAV within ANCHOR_TOLERANCE_DAYS of the target
date, on EITHER side (fixes an earlier one-sided-backward-only bug).

DEATH HANDLING: a fund's last-known NAV is fixed regardless of horizon.
If more than STALE_TOLERANCE_DAYS before asof, the fund is DEAD as of
this asof date for every horizon. Dead funds get a "died <year>" note
and are EXCLUDED from category avg/rank; the exclusion is counted
(n_dead / n_considered) rather than hidden.

RISK METRICS (Sharpe, max drawdown): per-fund, using each fund's own
optimal anchor/last window -- see fund_window / category_snapshot.

PORTFOLIO METRICS (correlation, weighted vol, diversification ratio):
computed on a COMMON date window across N funds -- necessarily a
different code path from the per-fund risk metrics above, since
per-fund vol answers "how volatile is THIS fund over ITS OWN best
window" while portfolio vol answers "how volatile is the COMBINATION
over the dates ALL selected funds share." These will not (and should
not) numerically match the per-fund vol shown elsewhere.
"""

import datetime as dt
import re

import numpy as np
import pandas as pd

HORIZONS = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}
ANCHOR_TOLERANCE_DAYS = 45
STALE_TOLERANCE_DAYS = 15  # matches returns_panel.py's END_TOL_DAYS "survived" convention
TRADING_DAYS_PER_YEAR = 252
MIN_OBS_FOR_VOL = 20  # below this, annualized vol is too noisy to trust

RISK_FREE_RATE = 0.065
# Static placeholder (~91-day T-bill ballpark), NOT a real historical
# risk-free series -- every horizon and every past as-of date uses this
# same number, so Sharpe understates/overstates depending on where
# actual short-term rates stood at the time. Fine for ranking funds
# against each other WITHIN one run at one as-of date; not yet fine for
# comparing Sharpe across different as-of dates. Swap in an actual
# 91-day T-bill or 10y G-sec series before relying on it beyond a rough
# read.


def sharpe_ratio(cagr, ann_vol, risk_free=RISK_FREE_RATE):
    if cagr is None or ann_vol is None or ann_vol == 0:
        return None
    return (cagr - risk_free) / ann_vol


def _vol_and_obs(con, fund_id, start_date, end_date):
    """Annualized vol + observation count over (start_date, end_date]."""
    return con.execute(
        """
        WITH slice AS (
            SELECT d, ln(nav / lag(nav) OVER (ORDER BY d)) AS lr
            FROM nav_fund WHERE fund_id = ? AND d > ? AND d <= ?
        )
        SELECT stddev_samp(lr) * sqrt(?), count(lr) FROM slice
        """,
        [fund_id, start_date, end_date, TRADING_DAYS_PER_YEAR],
    ).fetchone()


def _max_drawdown(con, fund_id, start_date, end_date):
    """Worst peak-to-trough NAV decline within [start_date, end_date],
    as a negative fraction (-0.35 = -35%). None if no NAVs in range."""
    df = con.execute(
        "SELECT nav FROM nav_fund WHERE fund_id = ? AND d BETWEEN ? AND ? ORDER BY d",
        [fund_id, start_date, end_date],
    ).df()
    if df.empty:
        return None
    running_max = df["nav"].cummax()
    return float((df["nav"] / running_max - 1).min())


def _nearest_anchor(con, fund_id, anchor_target, tolerance_days):
    """Nearest NAV to anchor_target within +/- tolerance_days, either side.
    Returns (date, nav) or None."""
    lo = anchor_target - dt.timedelta(days=tolerance_days)
    hi = anchor_target + dt.timedelta(days=tolerance_days)
    return con.execute(
        """
        SELECT d, nav
        FROM nav_fund
        WHERE fund_id = ? AND d BETWEEN ? AND ?
        ORDER BY abs(date_diff('day', d, ?))
        LIMIT 1
        """,
        [fund_id, lo, hi, anchor_target],
    ).fetchone()


def fund_window(con, fund_id, asof, years):
    """
    Trailing window for ONE fund. Returns None only when there's genuinely
    no NAV close enough to serve as an anchor (fund didn't exist that far
    back, or there's a data gap wider than tolerance right there).

    A DEAD fund (last NAV far before asof) still returns a dict — with
    is_dead=True and window_return/cagr/sharpe/max_dd=None — so callers
    print a "died <year>" note instead of a truncated number.
    """
    anchor_target = asof - dt.timedelta(days=round(years * 365.25))

    last_row = con.execute(
        "SELECT d, nav FROM nav_fund WHERE fund_id = ? AND d <= ? ORDER BY d DESC LIMIT 1",
        [fund_id, asof],
    ).fetchone()
    if last_row is None:
        return None
    last_date, last_nav = last_row
    stale_days = (asof - last_date).days
    is_dead = stale_days > STALE_TOLERANCE_DAYS

    anchor_row = _nearest_anchor(con, fund_id, anchor_target, ANCHOR_TOLERANCE_DAYS)
    if anchor_row is None:
        return None
    anchor_date, anchor_nav = anchor_row
    gap_days = abs((anchor_date - anchor_target).days)

    if is_dead:
        return {
            "is_dead": True,
            "death_year": last_date.year,
            "last_date": last_date,
            "window_return": None,
            "cagr": None,
            "ann_vol": None,
            "sharpe": None,
            "max_dd": None,
            "n_obs": None,
            "low_obs": False,
            "anchor_date": anchor_date,
            "gap_days": gap_days,
            "stale_days": stale_days,
        }

    if anchor_nav is None or anchor_nav <= 0:
        return None

    window_return = last_nav / anchor_nav - 1
    actual_years = (last_date - anchor_date).days / 365.25
    cagr = (last_nav / anchor_nav) ** (1 / actual_years) - 1 if actual_years > 0 else None

    ann_vol, n_obs = _vol_and_obs(con, fund_id, anchor_date, last_date)
    max_dd = _max_drawdown(con, fund_id, anchor_date, last_date)

    return {
        "is_dead": False,
        "death_year": None,
        "window_return": window_return,
        "cagr": cagr,
        "actual_years": actual_years,
        "ann_vol": ann_vol,
        "sharpe": sharpe_ratio(cagr, ann_vol),
        "max_dd": max_dd,
        "n_obs": n_obs,
        "low_obs": bool(n_obs is not None and n_obs < MIN_OBS_FOR_VOL),
        "anchor_date": anchor_date,
        "last_date": last_date,
        "gap_days": gap_days,
        "stale_days": stale_days,
    }


def category_snapshot(con, category, asof, years):
    """
    Return (alive_df, meta) for every fund in `category` at this horizon.

    alive_df has one row per LIVE peer: fund_id, window_return, cagr,
    ann_vol, sharpe, max_dd. Dead peers are excluded entirely — their
    count is in meta, not folded into the average.
    """
    anchor_target = asof - dt.timedelta(days=round(years * 365.25))
    lo = anchor_target - dt.timedelta(days=ANCHOR_TOLERANCE_DAYS)
    hi = anchor_target + dt.timedelta(days=ANCHOR_TOLERANCE_DAYS)

    df = con.execute(
        """
        WITH params AS (
            SELECT CAST(? AS DATE) AS asof_date
        ),
        peers AS (
            -- DISTINCT: fund_map can carry more than one row per fund_id
            -- (e.g. a name-history row from a renamed fund). Without
            -- this, such a fund is double-counted. Confirmed instance:
            -- INF251K01894 (Baroda-BNP-Paribas merger) in large-cap.
            SELECT DISTINCT fund_id FROM fund_map WHERE category = ?
        ),
        peers_p AS (
            SELECT p.fund_id, pr.asof_date
            FROM peers p CROSS JOIN params pr
        ),
        last_pt AS (
            SELECT pp.fund_id, n.d AS last_date, n.nav AS last_nav
            FROM peers_p pp
            ASOF JOIN nav_fund n ON pp.fund_id = n.fund_id AND n.d <= pp.asof_date
        ),
        anchor_candidates AS (
            SELECT p.fund_id, n.d AS anchor_date, n.nav AS anchor_nav,
                   row_number() OVER (
                       PARTITION BY p.fund_id
                       ORDER BY abs(date_diff('day', n.d, CAST(? AS DATE)))
                   ) AS rn
            FROM peers p
            JOIN nav_fund n ON p.fund_id = n.fund_id
            WHERE n.d BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ),
        anchor_pt AS (
            SELECT fund_id, anchor_date, anchor_nav FROM anchor_candidates WHERE rn = 1
        )
        SELECT
            l.fund_id, l.last_date, l.last_nav,
            a.anchor_date, a.anchor_nav,
            date_diff('day', l.last_date, CAST(? AS DATE)) AS stale_days
        FROM last_pt l
        JOIN anchor_pt a USING (fund_id)
        WHERE l.last_nav IS NOT NULL AND a.anchor_nav IS NOT NULL AND a.anchor_nav > 0
        """,
        [asof, category, anchor_target, lo, hi, asof],
    ).df()

    n_considered = len(df)
    is_dead = df["stale_days"] > STALE_TOLERANCE_DAYS
    n_dead = int(is_dead.sum())

    alive = df.loc[~is_dead].copy()
    alive["window_return"] = alive["last_nav"] / alive["anchor_nav"] - 1
    actual_years = (alive["last_date"] - alive["anchor_date"]).dt.days / 365.25
    alive["actual_years"] = actual_years
    alive["cagr"] = (alive["last_nav"] / alive["anchor_nav"]) ** (1 / actual_years) - 1

    vols, sharpes, mdds = [], [], []
    for _, row in alive.iterrows():
        v, _ = _vol_and_obs(con, row["fund_id"], row["anchor_date"], row["last_date"])
        vols.append(v)
        sharpes.append(sharpe_ratio(row["cagr"], v))
        mdds.append(_max_drawdown(con, row["fund_id"], row["anchor_date"], row["last_date"]))
    alive["ann_vol"] = vols
    alive["sharpe"] = sharpes
    alive["max_dd"] = mdds

    meta = {"n_considered": n_considered, "n_dead": n_dead}
    return alive, meta


def fund_date_range(con, fund_id):
    """(min_date, max_date) of a fund's full NAV history, or (None, None)."""
    return con.execute(
        "SELECT min(d), max(d) FROM nav_fund WHERE fund_id = ?", [fund_id]
    ).fetchone()


def _daily_simple_returns(con, fund_id, start_date, end_date):
    df = con.execute(
        "SELECT d, nav FROM nav_fund WHERE fund_id = ? AND d BETWEEN ? AND ? ORDER BY d",
        [fund_id, start_date, end_date],
    ).df()
    df = df.set_index("d")
    return df["nav"].pct_change()


def portfolio_metrics(con, fund_ids, weights, start_date, end_date):
    """
    Combine N funds under fixed WEIGHTS over a COMMON date window,
    assuming daily rebalancing back to those weights every day. This is
    a simplifying assumption: a real, unrebalanced buy-and-hold
    allocation's weights drift as the underlying funds move at
    different rates, so its actual vol/drawdown will differ somewhat
    from what's shown here -- more so the longer the window and the
    more the funds' returns diverge from each other.

    fund_ids: list of fund_id strings, matching keys in `weights`.
    weights: dict fund_id -> fraction (should sum to ~1.0).

    Returns None if fewer than MIN_OBS_FOR_VOL overlapping trading days
    exist across ALL funds -- not enough shared history to say anything.
    """
    simple = {fid: _daily_simple_returns(con, fid, start_date, end_date) for fid in fund_ids}
    combined = pd.DataFrame(simple).dropna()
    n_obs = len(combined)
    if n_obs < MIN_OBS_FOR_VOL:
        return None

    log_returns = np.log1p(combined)
    corr = log_returns.corr()

    per_fund_vol = {
        fid: float(log_returns[fid].std(ddof=1) * (TRADING_DAYS_PER_YEAR ** 0.5))
        for fid in fund_ids
    }
    weighted_avg_vol = sum(weights[fid] * per_fund_vol[fid] for fid in fund_ids)

    w = pd.Series(weights)
    portfolio_daily = combined[fund_ids].mul(w[fund_ids].values, axis=1).sum(axis=1)
    portfolio_nav = (1 + portfolio_daily).cumprod()
    elapsed_years = (combined.index[-1] - combined.index[0]).days / 365.25
    portfolio_cagr = (
        portfolio_nav.iloc[-1] ** (1 / elapsed_years) - 1 if elapsed_years > 0 else None
    )
    portfolio_vol = float(np.log1p(portfolio_daily).std(ddof=1) * (TRADING_DAYS_PER_YEAR ** 0.5))
    running_max = portfolio_nav.cummax()
    portfolio_max_dd = float((portfolio_nav / running_max - 1).min())

    return {
        "corr": corr,
        "per_fund_vol": per_fund_vol,
        "weighted_avg_vol": weighted_avg_vol,
        "portfolio_vol": portfolio_vol,
        "diversification_ratio": (
            weighted_avg_vol / portfolio_vol if portfolio_vol > 1e-6 else None
        ),
        "portfolio_cagr": portfolio_cagr,
        "portfolio_sharpe": sharpe_ratio(portfolio_cagr, portfolio_vol),
        "portfolio_max_dd": portfolio_max_dd,
        "n_obs": n_obs,
        "start": combined.index[0],
        "end": combined.index[-1],
    }


def resolve_fund_query(con, query, by_isin=False):
    """
    Match funds by fund_id (exact) or scheme-name search, deduped by
    fund_id (fund_map can carry more than one row per fund_id -- see
    category_snapshot's DISTINCT comment for the confirmed case).

    Name search matches every word in `query` as a WHOLE WORD (word-
    boundary regex), anywhere in scheme_name, in any order. Two
    escalating fixes were needed here, both confirmed against real data:

    1. Contiguous-phrase matching missed live, renamed funds -- "icici
       prudential bluechip" is not a substring of "ICICI Prudential
       Large Cap Fund (erstwhile Bluechip Fund)" even though every word
       is present, because SEBI's 2018 categorization mandate split the
       words apart in the rename. Fixed by matching each word
       independently (AND'd together).

    2. Plain per-word substring matching then broke the OPPOSITE case:
       "cap" as a bare substring matches inside "SBI LARGE & MIDCAP
       FUND" -- a different product (large-and-mid-cap blend, not pure
       large-cap) -- so "sbi large cap" started incorrectly matching it
       too. Fixed by requiring each word to match as a WHOLE word
       (regex \\bword\\b), so "cap" no longer matches the "cap" fused
       inside "midcap".

    Picks the highest scheme_code per fund_id as "most recent name" for
    each returned row -- a heuristic, since fund_map has no
    effective-date column.
    """
    if by_isin:
        where, params = "fund_id = ?", [query]
    else:
        words = query.lower().split()
        patterns = [rf"\b{re.escape(w)}\b" for w in words]
        where = " AND ".join(["regexp_matches(lower(scheme_name), ?)"] * len(patterns))
        params = patterns
    sql = f"""
        WITH matched AS (
            SELECT fund_id, scheme_name, category,
                   row_number() OVER (
                       PARTITION BY fund_id ORDER BY scheme_code DESC
                   ) AS rn
            FROM fund_map
            WHERE {where}
        )
        SELECT fund_id, scheme_name, category
        FROM matched
        WHERE rn = 1
        ORDER BY length(scheme_name)
    """
    return con.execute(sql, params).fetchall()


def rank_among_peers(peers, value_col, fund_id, subject_value):
    """
    1-indexed rank of `subject_value` among `peers[value_col]`, best=1.
    Ties are "competition ranking" -- two funds tied for best both get
    rank 1, the next distinct value gets rank 3, not 2.

    Deliberately EXCLUDES fund_id's own row from the comparison and uses
    `subject_value` (the caller's authoritative figure, from fund_window)
    instead of comparing against a same-fund row inside `peers`. `peers`
    is produced by category_snapshot, which recomputes CAGR/Sharpe for
    every peer -- including this fund -- via a vectorized pandas/numpy
    code path, separate from fund_window's scalar one. The two can differ
    by ~1e-15 for the SAME fund/dates (numpy's vectorized ** and Python's
    scalar ** aren't guaranteed bit-identical for fractional exponents).
    Comparing a fund against a re-derived copy of itself can then
    register as "self < self" or "self > self", shifting the rank by
    one. Confirmed on INF879O01019 (Parag Parikh Flexi Cap), 10y Sharpe:
    a fund genuinely #1 in its category showed rank "0/15" -- impossible,
    since ranks are 1-indexed. Most other cases wouldn't have looked
    obviously wrong -- an off-by-one on rank 10 of 22 just reads as 9/22.
    """
    n_peers = len(peers)
    others = peers[peers["fund_id"] != fund_id]
    better = (others[value_col] > subject_value).sum()
    return int(better) + 1, n_peers


def format_pct(x):
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def format_ratio(x):
    return f"{x:+.2f}" if x is not None else "n/a"