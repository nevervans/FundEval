import duckdb

con = duckdb.connect("mf_nav_full.duckdb", read_only=True)

plan_expr = "CASE WHEN upper(cast(plan AS VARCHAR)) LIKE '%DIRECT%' THEN 'DIRECT' ELSE 'REGULAR' END"
opt_expr = "CASE WHEN upper(cast(option AS VARCHAR)) LIKE '%GROWTH%' THEN 'GROWTH' ELSE 'IDCW' END"

for plan in ["REGULAR", "DIRECT"]:
    print(f"\n=== {plan} + GROWTH, via the real classification logic ===")
    total = con.execute(f"""
        SELECT count(*) FROM scheme
        WHERE {plan_expr} = '{plan}' AND {opt_expr} = 'GROWTH'
    """).fetchone()[0]
    with_cat = con.execute(f"""
        SELECT count(*) FROM scheme
        WHERE {plan_expr} = '{plan}' AND {opt_expr} = 'GROWTH' AND category IS NOT NULL
    """).fetchone()[0]
    print(f"total: {total}   category IS NOT NULL: {with_cat}   "
          f"NULL: {total - with_cat}  ({100 * (total - with_cat) / total:.1f}% NULL)" if total else "no rows")

    # Of the NULL-category ones, how old/dead are they? (last_nav_date distribution)
    null_ages = con.execute(f"""
        SELECT
            CASE
                WHEN last_nav_date >= DATE '2025-01-01' THEN 'active (2025+)'
                WHEN last_nav_date >= DATE '2020-01-01' THEN '2020-2024'
                WHEN last_nav_date >= DATE '2013-01-01' THEN '2013-2019'
                ELSE 'pre-2013 or null'
            END AS bucket,
            count(*) n
        FROM scheme
        WHERE {plan_expr} = '{plan}' AND {opt_expr} = 'GROWTH' AND category IS NULL
        GROUP BY 1 ORDER BY 1
    """).df()
    print("NULL-category rows by last_nav_date:")
    print(null_ages.to_string())