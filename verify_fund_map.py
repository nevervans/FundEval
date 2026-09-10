import sys
import duckdb

sys.path.insert(0, "analysis")
from returns_panel import CONFIG, build_fund_map  # noqa: E402

for plan in ["DIRECT", "REGULAR"]:
    out_path = f"/tmp/verify_{plan.lower()}.duckdb"
    con = duckdb.connect(out_path)
    con.execute(f"ATTACH 'mf_nav_full.duckdb' AS src (READ_ONLY)")
    codes, funds, no_isin = build_fund_map(con, CONFIG, plan)
    print(f"{plan}: {codes} codes -> {funds} funds  ({no_isin} lacking ISIN)")
    con.close()