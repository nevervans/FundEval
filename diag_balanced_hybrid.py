import sys
import duckdb

sys.path.insert(0, "tools")
from windows import fund_window, category_snapshot, HORIZONS  # noqa: E402

con = duckdb.connect("fundeval_analysis.duckdb", read_only=True)
asof = con.execute("SELECT max(d) FROM nav_fund").fetchone()[0]
print("as of:", asof)

try:
    n = con.execute("SELECT count(*) FROM category_map").fetchone()[0]
    print(f"\ncategory_map: {n} rows")
    print(con.execute("SELECT * FROM category_map LIMIT 20").df().to_string())
except Exception as e:
    print(f"\ncategory_map check failed: {e}")

labels = ["Hybrid Scheme - Balanced Hybrid Fund", "Hybrid Schemes - Balanced Hybrid Fund"]

for cat in labels:
    funds = con.execute(
        "SELECT DISTINCT fund_id, scheme_name FROM fund_map WHERE category = ?", [cat]
    ).df()
    print(f"\n--- fund_map raw label '{cat}': {len(funds)} funds ---")
    for _, row in funds.iterrows():
        rng = con.execute(
            "SELECT min(d), max(d) FROM nav_fund WHERE fund_id = ?", [row["fund_id"]]
        ).fetchone()
        w10 = fund_window(con, row["fund_id"], asof, HORIZONS["10y"])
        status = "None (no anchor within tolerance)" if w10 is None else (
            f"is_dead={w10['is_dead']}" if w10["is_dead"] else f"cagr={w10['cagr']:.4f}"
        )
        print(f"  {row['fund_id']}  {row['scheme_name'][:55]:55s}  "
              f"history {rng[0]} to {rng[1]}  10y: {status}")

print("\n--- what category_snapshot actually returns per raw label, 10y ---")
for cat in labels:
    peers, meta = category_snapshot(con, cat, asof, HORIZONS["10y"])
    print(f"  '{cat}': alive={len(peers)}  meta={meta}")