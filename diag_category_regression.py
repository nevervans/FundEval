import duckdb

con = duckdb.connect("mf_nav_full.duckdb", read_only=True)

print("=== updated_at distribution for category IS NULL rows ===")
print(con.execute("""
    SELECT date_trunc('day', updated_at) AS day, count(*) n
    FROM scheme WHERE category IS NULL
    GROUP BY 1 ORDER BY 1 DESC LIMIT 10
""").df().to_string())

print("\n=== updated_at distribution for category IS NOT NULL rows ===")
print(con.execute("""
    SELECT date_trunc('day', updated_at) AS day, count(*) n
    FROM scheme WHERE category IS NOT NULL
    GROUP BY 1 ORDER BY 1 DESC LIMIT 10
""").df().to_string())

print("\n=== source column breakdown for NULL-category rows updated today ===")
print(con.execute("""
    SELECT source, count(*) n
    FROM scheme
    WHERE category IS NULL AND date_trunc('day', updated_at) = current_date
    GROUP BY 1
""").df().to_string())

print("\n=== total scheme count and overall NULL rate, for reference ===")
row = con.execute("""
    SELECT count(*) total, sum((category IS NULL)::INT) null_cat
    FROM scheme
""").fetchone()
print(f"total: {row[0]}   category NULL: {row[1]}  ({100*row[1]/row[0]:.1f}%)")
