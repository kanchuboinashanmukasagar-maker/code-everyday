from flask import Flask, render_template, request, redirect, url_for, session, flash
import os, re, requests, psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date
import google.generativeai as genai

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
genai.configure(api_key=os.getenv("API_KEY"))
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def load_testcases(text):
    cases = []
    if not text:
        return cases
    inputs = re.findall(r'INPUT:\s*(.*?)\s*OUTPUT:', text, re.DOTALL)
    outputs = re.findall(r'OUTPUT:\s*(.*?)(?=INPUT:|$)', text, re.DOTALL)
    for a,b in zip(inputs, outputs):
        cases.append((a.strip(), b.strip()))
    return cases

def normalize(s):
    return " ".join((s or "").split())

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT password FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception:
        return "db error"
    if row and check_password_hash(row[0], password):
        session['username'] = username
        return redirect(url_for('dash'))
    return "invalid"

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = generate_password_hash(request.form.get('password'))
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, password))
            conn.commit()
            cur.close()
            conn.close()
            return redirect(url_for('home'))
        except psycopg2.IntegrityError:
            return "username exists"
        except Exception:
            return "db error"
    return render_template('register.html')

@app.route('/dash')
def dash():
    today = str(date.today())
    if session.get('last_date') == today and session.get('daily_q'):
        return render_template('index.html', question=session.get('daily_q'))
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = "Generate ONE DSA problem. Format: QUESTION: Title: Statement: TESTCASES INPUT: OUTPUT: (10 cases)"
        response = model.generate_content(prompt)
        text = getattr(response, "text", "") or ""
        parts = text.split("TESTCASES")
        question = parts[0].strip()
        testcases = parts[1].strip() if len(parts) > 1 else ""
        session['last_date'] = today
        session['daily_q'] = question
        session['testcases'] = testcases
        return render_template('index.html', question=question)
    except Exception:
        return render_template('index.html', question="(no question)")

@app.route('/submit_code', methods=['POST'])
def submit_code():
    code = request.form.get('code-input')
    language = request.form.get('language')
    stdin = request.form.get('stdin', "")
    action = request.form.get('action')
    if not code or not language:
        return redirect(url_for('dash'))
    payload = {"language": language, "version": "*", "stdin": stdin, "files": [{"name": "main", "content": code}]}
    try:
        r = requests.post("https://emkc.org/api/v2/piston/execute", json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return render_template('index.html', question=session.get('daily_q'), code=code, output="execution error")
    if action == "run":
        output = data.get("run", {}).get("stdout", "").strip()
        return render_template('index.html', question=session.get('daily_q'), code=code, output=output)
    testcases = load_testcases(session.get('testcases', ""))
    passed = 0
    total = len(testcases)
    for inp, expected in testcases:
        payload = {"language": language, "version": "*", "stdin": inp + "\n", "files": [{"name": "main", "content": code}]}
        try:
            r = requests.post("https://emkc.org/api/v2/piston/execute", json=payload, timeout=10)
            r.raise_for_status()
            res = r.json()
        except Exception:
            continue
        out = res.get("run", {}).get("stdout", "").strip()
        if normalize(out) == normalize(expected):
            passed += 1
    verdict = 'AC' if passed == total and total > 0 else 'WA'
    return render_template('index.html', question=session.get('daily_q'), code=code, verdict=verdict, passed=passed, total=total)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))