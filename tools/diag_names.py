import duckdb

con = duckdb.connect("fundeval_analysis.duckdb", read_only=True)
for term in ["birla", "axis large cap", "bajaj finserv large cap"]:
    print(f"\n--- matches for '{term}' ---")
    df = con.execute(
        "SELECT fund_id, scheme_name, category FROM fund_map "
        "WHERE lower(scheme_name) LIKE '%' || ? || '%' ORDER BY scheme_name",
        [term],
    ).df()
    print(df.to_string())