import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "complaints.db")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    severity INTEGER,
    priority TEXT,
    status TEXT,
    description TEXT,
    image_path TEXT,
    latitude REAL,
    longitude REAL
)
""")

conn.commit()
conn.close()

print("Database Created Successfully!")
