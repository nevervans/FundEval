"""
streamlit_app.py -- FundEval analyst UI

A thin presentation layer over tools/windows.py. All the actual analysis
(return windows, dead-fund handling, Sharpe, drawdown, correlation,
portfolio combination, fund-name resolution) lives in windows.py and is
unit-tested there (tools/test_windows.py) -- this file only handles
search UX, layout, and charts. No new analytical logic is implemented
here; if a number looks wrong, the bug is in windows.py, not this file.

Run (Codespace Terminal, from repo root):
    pip install streamlit
    streamlit run streamlit_app.py

Requires fundeval_analysis.duckdb and/or fundeval_analysis_direct.duckdb
to be present in the repo root (same as the CLI tools).
"""

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "tools"))

from windows import (  # noqa: E402
    HORIZONS,
    ANCHOR_TOLERANCE_DAYS,
    STALE_TOLERANCE_DAYS,
    RISK_FREE_RATE,
    fund_window,
    category_snapshot,
    rank_among_peers,
    resolve_fund_query,
    fund_date_range,
    portfolio_metrics,
    format_pct,
    format_ratio,
)

st.set_page_config(page_title="FundEval", layout="wide")

DB_OPTIONS = {
    "Regular plan": "fundeval_analysis.duckdb",
    "Direct plan": "fundeval_analysis_direct.duckdb",
}

# (2026-09-10) Only the Regular-plan DB gets an automated rebuild chain --
# see daily_update.py's rebuild_regular_analysis(), which explicitly does
# NOT cover fundeval_analysis_direct.duckdb yet. Direct-plan staleness
# still needs a manual rebuild; this check silently skips it rather than
# implying an automated fix is coming when someone selects it.
REGULAR_DB_PATH = DB_OPTIONS["Regular plan"]
LOCK_PATH = "daily_update.lock"
STALE_TRIGGER_DAYS = 1
LOCK_MAX_AGE_SECONDS = 60 * 60  # safety net only -- daily_update.py removes its own lock on exit


# --------------------------------------------------------------- caching

@st.cache_resource
def get_connection(db_path):
    return duckdb.connect(db_path, read_only=True)


@st.cache_data(show_spinner=False)
def get_latest_date(_con):
    row = _con.execute("SELECT max(d) FROM nav_fund").fetchone()
    return row[0] if row and row[0] else dt.date.today()


@st.cache_data(show_spinner=False)
def get_categories(_con):
    df = _con.execute(
        "SELECT DISTINCT category FROM fund_map WHERE category IS NOT NULL ORDER BY 1"
    ).df()
    return df["category"].tolist()


@st.cache_data(show_spinner=False)
def cached_resolve(_con, query, by_isin):
    return resolve_fund_query(_con, query, by_isin=by_isin)


@st.cache_data(show_spinner=False)
def cached_fund_window(_con, fund_id, asof_iso, years):
    return fund_window(_con, fund_id, dt.date.fromisoformat(asof_iso), years)


@st.cache_data(show_spinner=False)
def cached_category_snapshot(_con, category, asof_iso, years):
    return category_snapshot(_con, category, dt.date.fromisoformat(asof_iso), years)


@st.cache_data(show_spinner=False)
def cached_fund_date_range(_con, fund_id):
    return fund_date_range(_con, fund_id)


@st.cache_data(show_spinner=False)
def cached_nav_series(_con, fund_id):
    return _con.execute(
        "SELECT d, nav FROM nav_fund WHERE fund_id = ? ORDER BY d", [fund_id]
    ).df()


def fund_plan_label(con, fund_id):
    row = con.execute(
        "SELECT plan, option_type FROM fund_map WHERE fund_id = ? LIMIT 1", [fund_id]
    ).fetchone()
    return f"{row[0]}/{row[1]}" if row else "unknown"


def percentile_from_rank(rank, n_peers):
    """HIGHER = BETTER. Recomputed inline (not imported) since this
    module intentionally only depends on functions confirmed committed
    in windows.py -- see the top-of-file note."""
    if n_peers <= 1:
        return 100.0
    return round(100 * (n_peers - rank) / (n_peers - 1), 1)


def maybe_trigger_background_refresh(latest_date, db_path):
    """Non-blocking freshness check for the Regular-plan DB only.

    If NAVs look more than STALE_TRIGGER_DAYS old, this does NOT block the
    page. It spawns daily_update.py as a detached background process --
    whoever's viewing right now still gets an immediate answer, just with
    today's (possibly stale) data -- and shows a banner. A second person
    opening the app during that window won't trigger a second rebuild:
    daily_update.py's own lock file (written/removed around its rebuild
    chain) is checked first. LOCK_MAX_AGE_SECONDS is only a safety net for
    a crashed prior run that never got to clean up its lock; a normal run
    always removes it via `finally`, so this branch should rarely fire.
    """
    if db_path != REGULAR_DB_PATH:
        return  # Direct-plan rebuilds are manual for now -- see note above

    staleness_days = (dt.date.today() - latest_date).days
    if staleness_days <= STALE_TRIGGER_DAYS:
        return

    if os.path.exists(LOCK_PATH):
        lock_age = time.time() - os.path.getmtime(LOCK_PATH)
        if lock_age < LOCK_MAX_AGE_SECONDS:
            st.sidebar.info(
                f"Data is {staleness_days} day(s) old -- a refresh is already "
                f"running in the background. Reload in a few minutes."
            )
            return
        # lock older than the safety window -- treat as a crashed prior run, proceed

    st.sidebar.warning(
        f"Data is {staleness_days} day(s) old. Starting a background refresh -- "
        f"this page keeps working with today's data; reload in a few minutes for updated NAVs."
    )
    log_path = "streamlit_triggered_rebuild.log"
    with open(log_path, "a") as logf:
        subprocess.Popen(
            ["python3", "foundation/daily_update.py"],
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )


# ----------------------------------------------------------- fund search

def pick_fund(con, label, key_prefix):
    """Text search box; if the query resolves to >1 fund, renders a
    disambiguation dropdown. Returns (fund_id, scheme_name, category)
    or None. This is the UI fix for the CLI's biggest friction point --
    "N funds matched, narrow with --isin" becoming a dead end there
    becomes a dropdown here."""
    query = st.text_input(label, key=f"{key_prefix}_query", placeholder="e.g. parag parikh flexi cap or an ISIN")
    if not query:
        return None

    by_isin = len(query.strip()) == 12 and query.strip().upper().startswith("INF")
    matches = cached_resolve(con, query.strip(), by_isin)

    if not matches:
        st.warning(f"No fund matched '{query}'.")
        return None
    if len(matches) == 1:
        return matches[0]

    options = {f"{name}  [{cat or 'uncategorized'}]  ({fid})": (fid, name, cat) for fid, name, cat in matches}
    choice = st.selectbox(
        f"{len(matches)} funds matched -- pick one",
        list(options.keys()),
        key=f"{key_prefix}_disambig",
    )
    return options[choice]


# --------------------------------------------------------------- sidebar

st.sidebar.title("FundEval")

available_dbs = {label: path for label, path in DB_OPTIONS.items() if os.path.exists(path)}
if not available_dbs:
    st.error(
        "No database found in the repo root. Expected fundeval_analysis.duckdb "
        "and/or fundeval_analysis_direct.duckdb -- run this from the repo root, "
        "or sync the database into the Codespace first."
    )
    st.stop()

db_label = st.sidebar.radio("Universe", list(available_dbs.keys()))
con = get_connection(available_dbs[db_label])

latest_date = get_latest_date(con)
maybe_trigger_background_refresh(latest_date, available_dbs[db_label])

asof = st.sidebar.date_input("As of", value=latest_date, max_value=latest_date)
asof_iso = asof.isoformat()

with st.sidebar.expander("Methodology & caveats"):
    st.markdown(
        f"""
- **Dead funds** (no NAV within {STALE_TOLERANCE_DAYS} days of "as of") are flagged
  and excluded from category averages/ranks at every horizon -- never folded in
  with a truncated window.
- **Anchor matching**: a trailing-return anchor must land within
  {ANCHOR_TOLERANCE_DAYS} days of the exact target date, either side, or that
  horizon is reported as insufficient history.
- **Sharpe** uses a static risk-free rate of {RISK_FREE_RATE * 100:.1f}%, not a
  real historical bond series -- a placeholder, not a market yield.
- **Portfolio metrics** assume daily rebalancing to fixed weights. A real
  buy-and-hold allocation's weights drift over time and will show somewhat
  different vol/drawdown, more so the longer the window.
- **Rank/percentile** convention: higher percentile = better, consistent
  across this whole app.
        """
    )

st.sidebar.caption(f"Data as of {latest_date} · {db_label}")


# ----------------------------------------------------------------- tabs

tab_lookup, tab_browse, tab_portfolio = st.tabs(["Fund Lookup", "Category Browse", "Portfolio Compare"])


# ------------------------------------------------------------ Tab: Lookup

with tab_lookup:
    picked = pick_fund(con, "Search for a fund", "lookup")
    if picked:
        fund_id, scheme_name, category = picked
        plan_label = fund_plan_label(con, fund_id)

        st.subheader(scheme_name)
        st.caption(f"fund_id: {fund_id}  ·  category: {category or 'uncategorized'}  ·  plan: {plan_label}")

        rows = []
        died_note = None
        for label, years in HORIZONS.items():
            w = cached_fund_window(con, fund_id, asof_iso, years)
            if w is None:
                rows.append({"Horizon": label, "Return (CAGR)": "insufficient history",
                             "Cat. avg": "-", "Vol (ann)": "-", "Sharpe": "-", "Max DD": "-",
                             "Rank": "-", "Percentile": "-"})
                continue
            if w["is_dead"]:
                died_note = w["death_year"]
                rows.append({"Horizon": label, "Return (CAGR)": f"died {w['death_year']}",
                             "Cat. avg": "-", "Vol (ann)": "-", "Sharpe": "-", "Max DD": "-",
                             "Rank": "-", "Percentile": "-"})
                continue

            peers, meta = cached_category_snapshot(con, category, asof_iso, years) if category else (None, None)
            if peers is not None and len(peers) > 0:
                rank, n_peers = rank_among_peers(peers, "cagr", fund_id, w["cagr"])
                pctile_str = f"{percentile_from_rank(rank, n_peers):.0f}"
                cat_avg = format_pct(peers.loc[peers["fund_id"] != fund_id, "cagr"].mean())
                rank_str = f"{rank}/{n_peers}"
            else:
                pctile_str, cat_avg, rank_str = "-", "-", "-"

            rows.append({
                "Horizon": label,
                "Return (CAGR)": format_pct(w["cagr"]),
                "Cat. avg": cat_avg,
                "Vol (ann)": format_pct(w["ann_vol"]),
                "Sharpe": format_ratio(w["sharpe"]),
                "Max DD": format_pct(w["max_dd"]),
                "Rank": rank_str,
                "Percentile": pctile_str,
            })

        if died_note:
            st.warning(f"This fund stopped filing NAVs and is treated as died {died_note} as of {asof}.")

        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        nav_df = cached_nav_series(con, fund_id)
        if len(nav_df) > 0:
            st.line_chart(nav_df.set_index("d")["nav"], height=300)
            with st.expander(f"Full NAV history since inception ({len(nav_df)} daily rows)"):
                history_display = nav_df.rename(columns={"d": "Date", "nav": "NAV"}).sort_values("Date", ascending=False)
                st.dataframe(history_display, width="stretch", hide_index=True, height=400)
                st.download_button(
                    "Download as CSV",
                    history_display.to_csv(index=False),
                    file_name=f"{fund_id}_nav_history.csv",
                    mime="text/csv",
                )
        else:
            st.info("No NAV history found for this fund.")


# ------------------------------------------------------------ Tab: Browse

with tab_browse:
    categories = get_categories(con)
    category = st.selectbox("Category", categories, key="browse_category")
    horizon_label = st.radio("Horizon", list(HORIZONS.keys()), horizontal=True, key="browse_horizon")
    years = HORIZONS[horizon_label]

    if category:
        peers, meta = cached_category_snapshot(con, category, asof_iso, years)
        if peers is None or len(peers) == 0:
            st.info("No funds with sufficient history for this category/horizon.")
        else:
            display = peers.copy()
            display = display.sort_values("cagr", ascending=False).reset_index(drop=True)
            display.insert(0, "Rank", range(1, len(display) + 1))
            display["Percentile"] = [
                percentile_from_rank(r, len(display)) for r in display["Rank"]
            ]
            display["Return (CAGR)"] = display["cagr"].map(format_pct)
            display["Vol (ann)"] = display["ann_vol"].map(format_pct)
            display["Sharpe"] = display["sharpe"].map(format_ratio)
            display["Max DD"] = display["max_dd"].map(format_pct)

            fund_names = con.execute(
                "SELECT DISTINCT fund_id, scheme_name FROM fund_map WHERE fund_id IN "
                f"({','.join('?' * len(display))})", display["fund_id"].tolist()
            ).df().drop_duplicates("fund_id").set_index("fund_id")["scheme_name"]
            display["Fund"] = display["fund_id"].map(fund_names)

            st.caption(
                f"{meta['n_considered']} funds existed in this category ~{horizon_label} ago; "
                f"{meta['n_dead']} no longer file NAVs and are excluded below "
                f"({round(100 * meta['n_dead'] / meta['n_considered'], 1) if meta['n_considered'] else 0}% attrition)."
            )

            chart_data = display[["Fund", "cagr"]].head(25).copy()
            chart_data["Fund"] = chart_data["Fund"].fillna("unknown fund").astype(str).str.slice(0, 28)
            st.bar_chart(chart_data.set_index("Fund")["cagr"], height=420)
            if len(display) > 25:
                st.caption(f"Chart shows the top 25 of {len(display)} funds by return -- full ranked list below.")

            st.dataframe(
                display[["Rank", "Fund", "Return (CAGR)", "Vol (ann)", "Sharpe", "Max DD", "Percentile"]],
                width="stretch", hide_index=True,
            )


# --------------------------------------------------------- Tab: Portfolio

with tab_portfolio:
    n_funds = st.number_input("Number of funds", min_value=2, max_value=6, value=3, step=1)

    picks = []
    weight_inputs = []
    cols = st.columns(int(n_funds))
    for i in range(int(n_funds)):
        with cols[i]:
            p = pick_fund(con, f"Fund {i + 1}", f"port_{i}")
            picks.append(p)
            w = st.slider(f"Weight {i + 1}", 0.0, 1.0, round(1 / n_funds, 2), 0.05, key=f"port_w_{i}")
            weight_inputs.append(w)

    resolved = [p for p in picks if p is not None]
    if len(resolved) < 2:
        st.info("Pick at least two funds to see portfolio metrics.")
    else:
        fund_ids = [p[0] for p in resolved]
        labels = [p[1] for p in resolved]
        raw_weights = weight_inputs[: len(resolved)]
        total_w = sum(raw_weights) or 1.0
        weights = {fid: w / total_w for fid, w in zip(fund_ids, raw_weights)}

        st.caption("Weights normalized to sum to 100%: " + ", ".join(
            f"{lbl.split(' - ')[0]}: {weights[fid] * 100:.0f}%" for fid, lbl in zip(fund_ids, labels)
        ))

        ranges = {fid: cached_fund_date_range(con, fid) for fid in fund_ids}
        if any(r[0] is None for r in ranges.values()):
            st.warning("At least one fund has no NAV history at all.")
        else:
            common_start = max(r[0] for r in ranges.values())
            common_end = min(r[1] for r in ranges.values())
            if common_start >= common_end:
                st.warning("No overlapping NAV history across all selected funds.")
            else:
                start_ts = pd.Timestamp(common_start)
                end_ts = pd.Timestamp(common_end)
                st.write("Normalized NAV (rebased to 100 at the common start date)")
                nav_frames = {}
                for fid, lbl in zip(fund_ids, labels):
                    s = cached_nav_series(con, fid)
                    s = s[(s["d"] >= start_ts) & (s["d"] <= end_ts)].set_index("d")["nav"]
                    if len(s) > 0:
                        nav_frames[lbl] = s / s.iloc[0] * 100
                if nav_frames:
                    st.line_chart(pd.DataFrame(nav_frames), height=350)

                result = portfolio_metrics(con, fund_ids, weights, common_start, common_end)
                if result is None:
                    st.warning(
                        f"Only overlapping window is {common_start} to {common_end}, "
                        "too short for a reliable correlation/vol figure."
                    )
                else:
                    st.caption(
                        f"Common window: {result['start']} to {result['end']} "
                        f"({result['n_obs']} shared trading days)"
                    )

                    corr = result["corr"].copy()
                    corr.index = labels
                    corr.columns = labels
                    st.write("Pairwise correlation (daily log returns)")
                    st.dataframe(
                        corr.style.format("{:.2f}").background_gradient(cmap="RdBu_r", vmin=-1, vmax=1),
                        width="stretch",
                    )

                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Diversification ratio",
                              f"{result['diversification_ratio']:.2f}" if result["diversification_ratio"] else "n/a")
                    m2.metric("Portfolio CAGR", format_pct(result["portfolio_cagr"]))
                    m3.metric("Portfolio Sharpe", format_ratio(result["portfolio_sharpe"]))
                    m4.metric("Portfolio Max DD", format_pct(result["portfolio_max_dd"]))

                    st.caption(
                        f"Weighted-avg vol (no diversification): {result['weighted_avg_vol'] * 100:.2f}%  ·  "
                        f"Actual portfolio vol: {result['portfolio_vol'] * 100:.2f}%"
                    )

                    if result.get("pct_contribution_to_risk"):
                        risk_df = pd.DataFrame({
                            "Fund": labels,
                            "Weight": [f"{weights[fid] * 100:.0f}%" for fid in fund_ids],
                            "% of portfolio risk": [
                                f"{result['pct_contribution_to_risk'][fid] * 100:.1f}%" for fid in fund_ids
                            ],
                        })
                        st.write("Risk contribution (who's actually driving portfolio risk)")
                        st.dataframe(risk_df, width="stretch", hide_index=True)

                    st.caption(
                        "Assumes daily rebalancing to these fixed weights -- a real buy-and-hold "
                        "allocation's weights drift over time and will show somewhat different "
                        "vol/drawdown, more so the longer the window."
                    )