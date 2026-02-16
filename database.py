import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
def init_db():
    conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL)''')
    conn.commit()
    cur.close()
    conn.close()
if __name__ == "__main__":
    init_db()