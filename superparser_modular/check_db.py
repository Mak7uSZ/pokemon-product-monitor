import sqlite3

conn = sqlite3.connect("multi_site_monitor_modular.db")

rows = conn.execute("""
SELECT site, title, price_value, is_available, url
FROM products
WHERE site='pocketgames'
""").fetchall()

print("TOTAL:", len(rows))
print("----")

for r in rows[:50]:
    print(r)