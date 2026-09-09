"""
tools/export_office_snapshot.py — FundEval side feature: office-facing NAV +
performance snapshot workbook.

Purpose: give RMs a plain Excel view of trailing returns + category context
for a curated list of funds, computed off the same survivorship-bias-free
backend (fundeval_analysis.duckdb) that powers the real research tools --
NOT a live mfapi.in pull like the old MF_NAV_Master.xlsx. A fund that died
still shows up with a "Died <year>" note instead of silently vanishing.

Sheets produced:
  Snapshot     -- one row per fund: trailing returns, category avg, rank,
                  data freshness. Color-scaled so it scans at a glance.
  NAV History  -- full daily NAV since each fund's inception, wide format
                  (one column per fund), straight from nav_fund. This is
                  the one raw-data tab in the workbook -- kept separate
                  from Snapshot on purpose, since it's extraction, not
                  analysis, and nobody should have to scroll it to get
                  the headline numbers.
  NAV Chart    -- a line chart built off the NAV History tab, so the
                  trend is visible without reading the raw table.

Return math is delegated entirely to tools/windows.py's category_snapshot()
-- there is exactly one place in the whole project that defines what a
"return" means.

Input:  tools/office_fund_list.txt -- one fund per line, ISIN or a scheme-
        name substring (same matching rules as fund_lookup.py). Blank
        lines and lines starting with # are skipped. Auto-created (empty,
        with instructions) on first run if missing.
Output: office_snapshot_<asof>.xlsx, written next to this script.

Usage (Codespace Terminal):
    python3 tools/export_office_snapshot.py
    python3 tools/export_office_snapshot.py --asof 2025-03-31
    python3 tools/export_office_snapshot.py --db fundeval_analysis_direct.duckdb
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.chart import LineChart, Reference

sys.path.insert(0, str(Path(__file__).parent))
from windows import HORIZONS, STALE_TOLERANCE_DAYS, category_snapshot, format_pct  # noqa: E402

DB_PATH = "fundeval_analysis.duckdb"
FUND_LIST_PATH = Path(__file__).parent / "office_fund_list.txt"


def resolve_fund(con, query, by_isin):
    """Match by fund_id or scheme-name substring, deduped by fund_id.
    Mirrors resolve_fund() in fund_lookup.py -- duplicated here (not
    imported) so this script has no dependency on fund_lookup.py's
    argparse setup, only on the shared windows.py core."""
    if by_isin:
        sql = "SELECT DISTINCT fund_id, scheme_name, category FROM fund_map WHERE fund_id = ?"
    else:
        sql = """
            SELECT DISTINCT fund_id, scheme_name, category
            FROM fund_map
            WHERE lower(scheme_name) LIKE '%' || lower(?) || '%'
            ORDER BY length(scheme_name)
        """
    return con.execute(sql, [query]).fetchall()


def looks_like_isin(s: str) -> bool:
    s = s.strip().upper()
    return len(s) == 12 and s.startswith("INF")


def load_fund_list(path: Path):
    if not path.exists():
        path.write_text(
            "# One fund per line -- ISIN (e.g. INF090I01239) or a scheme-name\n"
            '# substring (e.g. "hdfc flexi cap"). Lines starting with # are ignored.\n'
        )
        print(f"No fund list found -- created a blank template at {path}.")
        print("Add your funds (one per line) and re-run this script.")
        sys.exit(0)
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def last_nav_date(con, fund_id, asof):
    row = con.execute(
        "SELECT d FROM nav_fund WHERE fund_id = ? AND d <= ? ORDER BY d DESC LIMIT 1",
        [fund_id, asof],
    ).fetchone()
    return row[0] if row else None


def fetch_full_history(con, fund_id):
    """Full daily NAV series since inception, straight from nav_fund.
    nav_fund already has splice corrections applied (the 145 face-value
    consolidations were rescaled into this table directly), so no extra
    stitching is needed here."""
    df = con.execute(
        "SELECT d, nav FROM nav_fund WHERE fund_id = ? ORDER BY d", [fund_id]
    ).df()
    return df.set_index("d")["nav"]


def build_rows(con, resolved, asof):
    """One dict per fund. Return/Cat Avg columns hold raw floats (or None)
    so Excel can color-scale and sort them -- formatting to % happens at
    write time, not here."""
    categories = sorted({cat for _, _, cat in resolved if cat})
    snapshots = {}  # (category, horizon_label) -> (alive_df, meta)
    for cat in categories:
        for label, years in HORIZONS.items():
            snapshots[(cat, label)] = category_snapshot(con, cat, asof, years)

    rows = []
    for fund_id, scheme_name, category in resolved:
        ld = last_nav_date(con, fund_id, asof)
        is_dead = (ld is None) or ((asof - ld).days > STALE_TOLERANCE_DAYS)
        days_since = (asof - ld).days if ld else None

        row = {
            "Fund": scheme_name,
            "Category": category or "Uncategorized",
            "Days Since NAV": days_since if days_since is not None else "n/a",
        }
        for label, years in HORIZONS.items():
            if not category:
                row[f"{label} Return"] = None
                row[f"{label} Cat Avg"] = None
                row[f"{label} Rank"] = "n/a"
                continue
            alive_df, meta = snapshots[(category, label)]
            metric_col = "window_return" if years == 1 else "cagr"
            match = alive_df.loc[alive_df["fund_id"] == fund_id]
            if match.empty:
                # Distinguish "dead" from "too young for this horizon" --
                # both are absent from alive_df, but they mean different
                # things and an RM shouldn't see a 3-year-old fund labeled
                # dead just because it lacks 10y history.
                row[f"{label} Return"] = "Died" if is_dead else "Too new"
                row[f"{label} Cat Avg"] = float(alive_df[metric_col].mean()) if len(alive_df) else None
                row[f"{label} Rank"] = "n/a"
            else:
                val = float(match.iloc[0][metric_col])
                row[f"{label} Return"] = val
                row[f"{label} Cat Avg"] = float(alive_df[metric_col].mean())
                rank = int((alive_df[metric_col] > val).sum()) + 1
                row[f"{label} Rank"] = f"{rank}/{len(alive_df)}"
        row["Status"] = f"Died {ld.year}" if is_dead and ld else ("Died" if is_dead else "")
        rows.append(row)
    return rows


def write_snapshot_sheet(wb, rows, asof):
    ws = wb.active
    ws.title = "Snapshot"

    df_out = pd.DataFrame(rows)
    headers = list(df_out.columns)

    ws["A1"] = "FundEval -- Office Snapshot"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Data as of {asof}  |  survivorship-bias-free: dead/renamed funds shown, not dropped"
    ws["A2"].font = Font(italic=True, size=9, color="666666")

    header_row = 4
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=header_row, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(horizontal="center")

    return_cols = [j for j, h in enumerate(headers, start=1) if h.endswith("Return")]
    for i, r in enumerate(df_out.itertuples(index=False), start=header_row + 1):
        for j, val in enumerate(r, start=1):
            cell = ws.cell(row=i, column=j)
            header = headers[j - 1]
            if isinstance(val, str) and val.lower().startswith("died"):
                cell.value = val
                cell.font = Font(color="C00000", italic=True)
            elif isinstance(val, str) and val == "Too new":
                cell.value = val
                cell.font = Font(color="808080", italic=True)
            elif header.endswith("Return") or header.endswith("Cat Avg"):
                cell.value = val  # float or None
                cell.number_format = "+0.00%;-0.00%"
            elif header == "Days Since NAV" and isinstance(val, (int, float)):
                cell.value = val
                if val > STALE_TOLERANCE_DAYS:
                    cell.font = Font(color="C00000")
            else:
                cell.value = val

    last_row = header_row + len(df_out)
    # Color-scale each Return column independently (not Cat Avg -- that's
    # context, not the thing you want to rank funds by at a glance).
    for j in return_cols:
        col = get_column_letter(j)
        rng = f"{col}{header_row + 1}:{col}{last_row}"
        ws.conditional_formatting.add(
            rng,
            ColorScaleRule(
                start_type="min", start_color="F8696B",
                mid_type="percentile", mid_value=50, mid_color="FFEB84",
                end_type="max", end_color="63BE7B",
            ),
        )

    for j, h in enumerate(headers, start=1):
        col_vals = df_out[h].astype(str)
        width = max(12, min(30, col_vals.str.len().max() + 4 if len(col_vals) else 12, len(h) + 4))
        ws.column_dimensions[get_column_letter(j)].width = width

    ws.freeze_panes = f"A{header_row + 1}"
    return ws


def write_history_sheet(wb, con, resolved):
    """Wide format: one row per date, one column per fund -- same shape
    RMs already get from vendor NAV exports. This is the raw-data tab;
    the Snapshot sheet stays clean of it on purpose."""
    series = {}
    for fund_id, scheme_name, _ in resolved:
        s = fetch_full_history(con, fund_id)
        if not s.empty:
            series[scheme_name] = s
    if not series:
        return None, 0, 0

    wide = pd.DataFrame(series).sort_index()

    ws = wb.create_sheet("NAV History")
    ws.append(["NAV Date"] + list(wide.columns))
    for h in ws[1]:
        h.font = Font(bold=True, color="FFFFFF")
        h.fill = PatternFill("solid", fgColor="1F4E78")

    for date, vals in wide.iterrows():
        ws.append([date] + [None if pd.isna(v) else round(float(v), 4) for v in vals])

    n_rows = len(wide)
    for row in ws.iter_rows(min_row=2, max_row=n_rows + 1, min_col=1, max_col=1):
        row[0].number_format = "yyyy-mm-dd"
    for row in ws.iter_rows(min_row=2, max_row=n_rows + 1, min_col=2, max_col=1 + len(wide.columns)):
        for cell in row:
            cell.number_format = "0.0000"

    ws.freeze_panes = "B2"
    ws.column_dimensions["A"].width = 14
    for j in range(2, 2 + len(wide.columns)):
        ws.column_dimensions[get_column_letter(j)].width = 16

    return ws, n_rows, len(wide.columns)


def add_nav_chart(wb, history_ws, n_rows, n_funds):
    if history_ws is None or n_rows == 0:
        return
    chart = LineChart()
    chart.title = "NAV History Since Inception"
    chart.style = 2
    chart.y_axis.title = "NAV (Rs)"
    chart.x_axis.title = "Date"
    chart.width = 32
    chart.height = 16

    data = Reference(history_ws, min_col=2, max_col=1 + n_funds, min_row=1, max_row=n_rows + 1)
    cats = Reference(history_ws, min_col=1, max_col=1, min_row=2, max_row=n_rows + 1)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)

    ws_chart = wb.create_sheet("NAV Chart")
    ws_chart.add_chart(chart, "A1")
    ws_chart.sheet_view.showGridLines = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--asof", type=str, default=None,
        help="YYYY-MM-DD, defaults to the latest NAV date in the panel",
    )
    parser.add_argument(
        "--db", type=str, default=DB_PATH,
        help=f"Path to the analysis database. Default: {DB_PATH} (Regular-plan "
             f"universe). Pass fundeval_analysis_direct.duckdb for Direct-plan funds.",
    )
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=True)

    if args.asof:
        asof = dt.date.fromisoformat(args.asof)
    else:
        asof = con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]

    identifiers = load_fund_list(FUND_LIST_PATH)
    print(f"Loaded {len(identifiers)} fund(s) from {FUND_LIST_PATH.name}. Data as of {asof}.")

    resolved = []      # (fund_id, scheme_name, category)
    unresolved = []     # (identifier, reason)
    for ident in identifiers:
        matches = resolve_fund(con, ident, looks_like_isin(ident))
        if len(matches) == 0:
            unresolved.append((ident, "no match"))
        elif len(matches) > 1:
            unresolved.append((ident, f"{len(matches)} matches -- be more specific"))
        else:
            resolved.append(matches[0])

    if unresolved:
        print("\nCouldn't resolve these -- skipped, everything else still ran:")
        for ident, reason in unresolved:
            print(f"  - {ident!r}: {reason}")

    if not resolved:
        print("\nNothing resolved -- nothing to export.")
        sys.exit(1)

    rows = build_rows(con, resolved, asof)

    wb = Workbook()
    write_snapshot_sheet(wb, rows, asof)
    history_ws, n_rows, n_funds = write_history_sheet(wb, con, resolved)
    add_nav_chart(wb, history_ws, n_rows, n_funds)

    # Tag the filename with the plan universe so Regular and Direct runs on
    # the same --asof never silently overwrite each other.
    db_tag = Path(args.db).stem.replace("fundeval_analysis", "").lstrip("_")
    out_name = f"office_snapshot_{db_tag + '_' if db_tag else ''}{asof}.xlsx"
    wb.save(out_name)
    print(f"\nWrote {out_name} ({len(rows)} fund(s), {len(unresolved)} skipped).")

    con.close()


if __name__ == "__main__":
    main()
    