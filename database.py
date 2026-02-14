import sqlite3
conn = sqlite3.connect("login.sqlite3")
cur = conn.cursor()
cur.execute('''
CREATE TABLE IF NOT EXISTS users (
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
)
''')
cur.execute("INSERT OR IGNORE INTO users(username,password) VALUES(?,?)",("stu1","pass1"))
conn.commit()
conn.close()
