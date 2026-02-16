from flask import Flask, render_template, request, redirect, url_for, session
import os, re, requests, psycopg2
from dotenv import load_dotenv
import google.generativeai as genai
from datetime import date
from werkzeug.security import generate_password_hash, check_password_hash
load_dotenv()
genai.configure(api_key=os.getenv("API_KEY"))
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get("SECRET_KEY", "prod-secret-123")
def get_conn():
    return psycopg2.conct(os.environ.get("DATABASE_URL"))
def load_testcases(question):
    cases = []
    inputs = re.findall(r'INPUT:\s*(.*?)\s*OUTPUT:', question, re.DOTALL)
    outputs = re.findall(r'OUTPUT:\s*(.*?)(?=INPUT:|$)', question, re.DOTALL)
    for inp, out in zip(inputs, outputs):
        cases.append((inp.strip(), out.strip()))
    return cases
def normalize(text):
    return " ".join(text.split())
@app.route('/')
def home():
    return render_template('home.html')
@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT password FROM users WHERE username=%s", (username,))
    data = cur.fetchone()
    cur.close()
    conn.close()
    if data and check_password_hash(data[0], password):
        session['username'] = username
        return redirect(url_for('dash'))
    return "invalid"
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = generate_password_hash(request.form.get('password'))
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, password))
            conn.commit()
        except psycopg2.IntegrityError:
            return "Username already exists"
        finally:
            cur.close()
            conn.close()
        return redirect(url_for('home'))
    return render_template('register.html')
@app.route('/dash')
def dash():
    today = str(date.today())
    if session.get('last_date') == today:
        return render_template("index.html", question=session.get('daily_q'))
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = "Generate ONE DSA problem. Format: QUESTION: Title: Statement: TESTCASES INPUT: OUTPUT: (10 cases)"
    response = model.generate_content(prompt)
    parts = response.text.split("TESTCASES")
    question = parts[0]
    testcases = parts[1] if len(parts) > 1 else ""
    session['last_date'] = today
    session['daily_q'] = question
    session['testcases'] = testcases
    return render_template('index.html', question=question)
@app.route('/submit_code', methods=['POST'])
def submit_code():
    code = request.form.get('code-input')
    language = request.form.get('language')
    stdin = request.form.get('stdin', "")
    action = request.form.get('action')
    if action == "run":
        payload = {"language": language, "version": "*", "stdin": stdin, "files": [{"name": "main", "content": code}]}
        response = requests.post("https://emkc.org/api/v2/piston/execute", json=payload).json()
        output = response.get("run", {}).get("stdout", "").strip()
        return render_template('index.html', question=session.get('daily_q'), code=code, output=output)
    testcases = load_testcases(session.get('testcases', ""))
    passed = 0
    total = len(testcases)
    for inp, expected_output in testcases:
        payload = {"language": language, "version": "*", "stdin": inp + "\n", "files": [{"name": "main", "content": code}]}
        res = requests.post("https://emkc.org/api/v2/piston/execute", json=payload).json()
        output = res.get("run", {}).get("stdout", "").strip()
        if normalize(output) == normalize(expected_output):
            passed += 1
    verdict = 'AC' if passed == total and total > 0 else 'WA'
    return render_template('index.html', question=session.get('daily_q'), code=code, verdict=verdict, passed=passed, total=total)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))