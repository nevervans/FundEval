"""
windows.py — shared trailing-return window logic for FundEval analyst tools.

Used by fund_lookup.py, compare_funds.py, and survivorship_report.py.

ANCHOR MATCHING: nearest NAV within ANCHOR_TOLERANCE_DAYS of the target
date, on EITHER side.

DEATH HANDLING: a fund's last-known NAV is fixed regardless of horizon.
If more than STALE_TOLERANCE_DAYS before asof, the fund is DEAD as of
this asof date for every horizon -- excluded from category avg/rank,
with the exclusion counted (n_dead / n_considered), not hidden.

RISK METRICS: Sharpe (static risk-free placeholder -- see RISK_FREE_RATE)
and max drawdown, per fund and category-averaged.

PORTFOLIO METRICS: correlation, weighted vol, diversification ratio, and
marginal/percentage contribution to portfolio risk, all on a COMMON date
window across N funds.

RANKING: rank_among_peers excludes the subject fund from its own
comparison set entirely (see its docstring for why -- a confirmed
floating-point self-comparison bug). percentile_from_rank converts a
rank into a "beat X% of peers" figure, HIGHER = BETTER.

ROLLING RETURNS: rolling_returns/rolling_summary characterize a single
fund's own historical path across many overlapping N-year windows, to
make point-in-time fragility (see fund_lookup.py --rolling) visible
without needing to manually re-run at different --asof dates.

SURVIVORSHIP: survivorship_report quantifies FundEval's founding premise
directly -- what fraction of funds active N years ago no longer exist,
per category.

FUND NAME RESOLUTION: resolve_fund_query matches every word in a query
as a whole word (regex \\bword\\b), anywhere in scheme_name, in any
order -- see its docstring for the two confirmed failure modes (renamed
funds splitting words apart; compound words like "midcap" creating
false positives on a bare substring match) that led to this design.
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
# risk-free series -- fine for ranking funds against each other at one
# as-of date, not yet for comparing Sharpe across different as-of dates.


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
    no NAV close enough to serve as an anchor. A DEAD fund still returns
    a dict (is_dead=True, window_return/cagr/sharpe/max_dd=None) so
    callers print a "died <year>" note instead of a truncated number.
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


def _category_raw_snapshot(con, category, asof, years):
    """Shared raw query behind category_snapshot and survivorship_counts:
    last/anchor NAV + stale_days for every DISTINCT fund_id in `category`
    -- alive AND dead. Callers decide what enrichment to layer on top.
    Extracted so this SQL exists in exactly one place; category_snapshot
    and survivorship_counts need very different amounts of per-peer work
    on top of the same underlying rows."""
    anchor_target = asof - dt.timedelta(days=round(years * 365.25))
    lo = anchor_target - dt.timedelta(days=ANCHOR_TOLERANCE_DAYS)
    hi = anchor_target + dt.timedelta(days=ANCHOR_TOLERANCE_DAYS)

    return con.execute(
        """
        WITH params AS (
            SELECT CAST(? AS DATE) AS asof_date
        ),
        peers AS (
            -- DISTINCT: fund_map can carry more than one row per fund_id
            -- (a name-history row from a renamed fund). Confirmed
            -- instance: INF251K01894 (Baroda-BNP-Paribas merger).
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


def category_snapshot(con, category, asof, years):
    """
    Return (alive_df, meta) for every fund in `category` at this horizon.
    alive_df has one row per LIVE peer: fund_id, window_return, cagr,
    ann_vol, sharpe, max_dd. Dead peers are excluded entirely -- their
    count is in meta, not folded into the average.
    """
    df = _category_raw_snapshot(con, category, asof, years)

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


_CLOSED_END_PATTERN = (
    r"\bfmp\b|\bftp\b|\bfixed maturity\b|\bfixed term\b|\bfixed horizon\b|"
    r"\bfixed tenure\b|\binterval\b|\bcapital protection\b|\bdual advantage\b|"
    r"\bcapital builder\b|\binfrastructure debt\b|"
    r"close[d]?[\s-]?end|"
    r"\(\s*\d{2,5}\s*days?\s*\)"
)
# Broadened once already from the original fmp/ftp/fixed-maturity/fixed-
# horizon/fixed-tenure/capital-protection/dual-advantage list, after
# confirming on real data that "IDF"/"Income"/"Growth" categories still
# showed ~100% death rates on their residual (non-FMP/FTP) funds. Every
# addition below is a confirmed real case, not a guess:
#   - "interval" (was "interval fund"): UTI names these "... Interval
#     Plan", not "Interval Fund" -- e.g. "UTI F I I F ... Quarterly
#     Interval Plan".
#   - "infrastructure debt": IL&FS Infrastructure Debt Fund Series
#     1A/1B/1C -- a distinct RBI/SEBI-regulated closed-end product type.
#   - the (N Days) pattern: a literal day-count in parentheses is an
#     unambiguous closed-end signal in Indian fund naming -- e.g. "UTI
#     FTIF ... (1831 Days)", "UTI Focussed Equity Fund Series - 1
#     (2195 Days)".
#   - "fixed term" (was "fixed term plan"): HSBC names these "Fixed
#     Term Series", not "... Plan".
#   - "capital builder": Reliance's own closed-end equity product line
#     ("Reliance Capital Builder Fund - Series A/B/C").
#   - "close[d] end": literal "Close Ended"/"Closed Ended" naming
#     (Reliance/UTI "Close Ended Equity Fund").
#
# Still NOT matched, and deliberately so: a handful of ICICI funds
# (Dynamic Bond Fund, MIP, Blended Plan) stopped reporting in 2018 with
# no closed-end signal in their names at all -- these read like ordinary
# open-ended funds, and 2018 is exactly when SEBI's one-scheme-per-
# category mandate forced AMC scheme consolidations. That's a real
# research question (did these merge into a surviving ICICI scheme
# under a DIFFERENT fund_id, breaking continuity?), not something a
# naming pattern can or should resolve by guessing.


def _closed_end_fund_ids(con):
    """fund_ids whose (most recent) scheme_name matches common closed-end
    / fixed-maturity naming patterns -- see _CLOSED_END_PATTERN's comment
    for the specific confirmed cases behind each keyword.

    These mature on a SCHEDULED date by design -- their NAV series
    stopping isn't the same phenomenon as an open-ended fund closing.
    Confirmed on real data: the legacy "IDF" category (597 funds) is
    almost entirely FMPs, Fixed Horizon Funds, and Infrastructure Debt
    Funds, inflating its reported death rate to ~100% for a reason that
    has nothing to do with survivorship bias in the interesting sense.

    This is a NAME-PATTERN heuristic, not a schema flag -- fund_map has
    no open/closed-end column -- so it will miss unusual naming and
    can't be treated as exhaustive."""
    df = con.execute(
        """
        WITH ranked AS (
            SELECT fund_id, scheme_name,
                   row_number() OVER (PARTITION BY fund_id ORDER BY scheme_code DESC) AS rn
            FROM fund_map
        )
        SELECT fund_id, scheme_name FROM ranked WHERE rn = 1
        """
    ).df()
    mask = df["scheme_name"].str.contains(_CLOSED_END_PATTERN, case=False, regex=True, na=False)
    return set(df.loc[mask, "fund_id"])


def survivorship_counts(con, category, asof, years, exclude_fund_ids=None):
    """n_considered/n_dead ONLY -- skips category_snapshot's per-peer
    vol/Sharpe/drawdown enrichment. survivorship_report calls this once
    per category across the WHOLE fund universe, and that enrichment
    would be needlessly expensive at that scale for numbers it never
    uses.

    `exclude_fund_ids`, if given, drops those fund_ids before counting
    -- used to exclude likely closed-end/fixed-maturity products (see
    _closed_end_fund_ids)."""
    df = _category_raw_snapshot(con, category, asof, years)
    if exclude_fund_ids:
        df = df[~df["fund_id"].isin(exclude_fund_ids)]
    n_considered = len(df)
    n_dead = int((df["stale_days"] > STALE_TOLERANCE_DAYS).sum())
    return n_considered, n_dead


def survivorship_report(con, asof, years, exclude_closed_end=True):
    """
    For every category, what fraction of funds that existed ~`years`
    ago no longer exist (or stopped reporting) as of `asof`. This
    directly quantifies FundEval's founding premise: analysis built only
    from currently-live funds silently conditions on survival -- this is
    by how much, per category.

    exclude_closed_end (default True): drop funds matching common
    closed-end/fixed-maturity naming patterns before counting -- their
    scheduled maturity isn't the same phenomenon as organic fund
    closure, and conflating the two badly distorted several legacy
    category buckets (confirmed on real data). Pass False to see the
    raw, unfiltered picture.

    KNOWN LIMITATION NOT FIXED HERE: several categories in real data
    appear to be the SAME underlying fund type recorded under different
    naming eras (confirmed: "ELSS", "Equity Scheme - ELSS", and "Equity
    Schemes - ELSS- Tax Saver Fund" as three separate labels). This
    report does NOT canonicalize across those labels -- doing so
    correctly requires deliberately mapping each fragment by hand, which
    is a real analysis decision, not something to guess at silently here.
    """
    exclude_ids = _closed_end_fund_ids(con) if exclude_closed_end else set()

    categories = con.execute(
        "SELECT DISTINCT category FROM fund_map WHERE category IS NOT NULL"
    ).fetchall()
    rows = []
    for (category,) in categories:
        n_considered, n_dead = survivorship_counts(con, category, asof, years, exclude_ids)
        if n_considered == 0:
            continue
        rows.append({
            "category": category,
            "n_considered": n_considered,
            "n_dead": n_dead,
            "death_rate_pct": round(100 * n_dead / n_considered, 1),
        })
    return pd.DataFrame(rows).sort_values("death_rate_pct", ascending=False).reset_index(drop=True)


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
    assuming daily rebalancing back to those weights every day -- a real,
    unrebalanced buy-and-hold allocation's weights drift over time and
    will show somewhat different vol/drawdown, more so the longer the
    window.

    Returns None if fewer than MIN_OBS_FOR_VOL overlapping trading days
    exist across ALL funds.

    pct_contribution_to_risk: each fund's share of TOTAL portfolio risk
    (sums to 100% across funds by construction), via the standard
    covariance-based decomposition on SIMPLE daily returns -- a
    different basis from the log-transformed series used for
    portfolio_vol above. The two agree to a small fraction of a basis
    point for realistic daily fund returns; percentages are normalized
    to sum to 100% regardless, so this doesn't meaningfully contradict
    the headline portfolio_vol figure.
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

    cov = combined[fund_ids].cov()
    w_vec = w[fund_ids]
    port_var_simple = float(w_vec @ cov @ w_vec)
    if port_var_simple > 0:
        port_vol_simple = port_var_simple ** 0.5
        marginal = (cov @ w_vec) / port_vol_simple
        contrib = w_vec * marginal
        pct_contribution = (contrib / contrib.sum()).to_dict()
    else:
        pct_contribution = {fid: None for fid in fund_ids}

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
        "pct_contribution_to_risk": pct_contribution,
        "n_obs": n_obs,
        "start": combined.index[0],
        "end": combined.index[-1],
    }


def rolling_returns(con, fund_id, years, step_days=5):
    """
    CAGR for every N-year window starting at each available NAV date,
    sampled every `step_days` (daily granularity produces near-identical
    adjacent windows and an oversized, noisy result). Annualizes on each
    window's ACTUAL elapsed time -- same principle as fund_window, not
    an assumed exact `years` -- which makes CAGR exactly invariant to
    small anchor-matching imprecision (the elapsed-days term cancels
    algebraically).

    Returns a DataFrame with columns: start_date, end_date, cagr. Empty
    if the fund doesn't have enough history for even one window.
    """
    df = con.execute(
        "SELECT d, nav FROM nav_fund WHERE fund_id = ? ORDER BY d", [fund_id]
    ).df()
    if df.empty:
        return pd.DataFrame(columns=["start_date", "end_date", "cagr"])
    df = df.set_index("d")
    dates = df.index
    navs = df["nav"]

    target_delta = dt.timedelta(days=round(years * 365.25))
    rows = []
    for i in range(0, len(dates), step_days):
        start_date = dates[i]
        start_nav = navs.iloc[i]
        if start_nav is None or start_nav <= 0:
            continue
        target_end = start_date + target_delta

        idx = dates.searchsorted(target_end)
        candidates = []
        if idx < len(dates):
            candidates.append(idx)
        if idx > 0:
            candidates.append(idx - 1)
        if not candidates:
            continue
        best_idx = min(candidates, key=lambda j: abs((dates[j] - target_end).days))
        end_date = dates[best_idx]
        if abs((end_date - target_end).days) > ANCHOR_TOLERANCE_DAYS:
            continue

        actual_years = (end_date - start_date).days / 365.25
        if actual_years <= 0:
            continue
        end_nav = navs.iloc[best_idx]
        cagr = (end_nav / start_nav) ** (1 / actual_years) - 1
        rows.append({"start_date": start_date, "end_date": end_date, "cagr": cagr})

    return pd.DataFrame(rows)


def rolling_summary(rolling_df):
    """Distribution stats for a rolling_returns() result. None if empty."""
    if rolling_df.empty:
        return None
    c = rolling_df["cagr"]
    return {
        "n_windows": len(rolling_df),
        "median_cagr": float(c.median()),
        "p10_cagr": float(c.quantile(0.10)),
        "p90_cagr": float(c.quantile(0.90)),
        "worst_cagr": float(c.min()),
        "best_cagr": float(c.max()),
        "pct_negative": float((c < 0).mean() * 100),
    }


def resolve_fund_query(con, query, by_isin=False):
    """
    Match funds by fund_id (exact) or scheme-name search, deduped by
    fund_id. Name search matches every word in `query` as a WHOLE WORD
    (word-boundary regex), anywhere in scheme_name, in any order.

    Two escalating fixes were needed here, both confirmed against real
    data: (1) contiguous-phrase matching missed live, renamed funds --
    e.g. ICICI Prudential Bluechip's post-2018 rename split the words
    apart; fixed via per-word AND matching. (2) plain per-word substring
    matching then created false positives on compound words -- "cap" as
    a bare substring matched inside "SBI LARGE & MIDCAP FUND", a
    different product; fixed via whole-word (\\bword\\b) matching.

    Picks the highest scheme_code per fund_id as "most recent name" --
    a heuristic, since fund_map has no effective-date column.
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
    instead of comparing against a same-fund row inside `peers`.
    category_snapshot recomputes CAGR/Sharpe for every peer -- including
    this fund -- via a vectorized pandas/numpy code path, separate from
    fund_window's scalar one; the two can differ by ~1e-15 for the SAME
    fund/dates. Comparing a fund against a re-derived copy of itself can
    then register as "self < self" or "self > self", shifting the rank
    by one. Confirmed on INF879O01019 (Parag Parikh Flexi Cap), 10y
    Sharpe: a fund genuinely #1 in its category showed rank "0/15" --
    impossible, since ranks are 1-indexed.
    """
    n_peers = len(peers)
    others = peers[peers["fund_id"] != fund_id]
    better = (others[value_col] > subject_value).sum()
    return int(better) + 1, n_peers


def percentile_from_rank(rank, n_peers):
    """Percentile where HIGHER = BETTER: 95 means this fund beat 95% of
    category peers. (Some fund databases use the opposite convention --
    this module always means higher-is-better; say so wherever shown.)"""
    if n_peers <= 1:
        return 100.0
    return round(100 * (n_peers - rank) / (n_peers - 1), 1)


def format_pct(x):
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def format_ratio(x):
    return f"{x:+.2f}" if x is not None else "n/a"