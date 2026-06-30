import sqlite3

conn = sqlite3.connect("complaints.db")

cursor = conn.cursor()

cursor.execute("SELECT * FROM complaints")

rows = cursor.fetchall()

for row in rows:
    print(row)

conn.close()