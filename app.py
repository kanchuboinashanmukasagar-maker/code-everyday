from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3, os, re, requests
from dotenv import load_dotenv
import google.generativeai as genai
from datetime import date
import psycopg2
load_dotenv()
genai.configure(api_key=os.getenv("API_KEY"))

app = Flask(__name__, static_folder='static', template_folder='templates')
FILE = "daily.txt"
app.secret_key = os.environ.get("SECRET_KEY", "fallbacksecret")

def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

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
    cur.execute("SELECT username, password FROM users WHERE username=%s AND password=%s", (username, password))
    data = cur.fetchone()
    conn.close()
    if data:
        session['data'] = username
        return redirect(url_for('dash'))
    else:
        return "invalid"

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
        except sqlite3.IntegrityError:
            return render_template('error.html', message="Username already exists")
        finally:
            conn.close()
        return redirect(url_for('home'))
    return render_template('register.html')

@app.route('/dash')
def dash():
    today = str(date.today())
    if os.path.exists(FILE):
        with open(FILE, "r") as f:
            saved_date = f.readline().strip()
            saved_question = f.read()
        if saved_date == today:
            return render_template("index.html", question=saved_question)
    model = genai.GenerativeModel("models/gemini-2.5-flash")
    response = model.generate_content(
        "You are a competitive programming problem setter. Generate exactly ONE DSA coding problem followed by test cases. Rules: Difficulty Easy to Medium. No solution or explanation. Plain text only. No special characters. Format exactly like this: QUESTION: Problem Title:  Problem Statement:  Input Format:  Output Format:  Constraints:  TESTCASES  INPUT:  OUTPUT:      INPUT:  OUTPUT:      INPUT:  OUTPUT:      Generate at least 10 test cases."
    )
    full_question = response.text
    question = full_question.split("TESTCASES")[0]
    testcases_part = full_question.split("TESTCASES")[1]
    with open("daily.txt", "w") as f:
        f.write(today + "\n")
        f.write(question)
    with open("testcases.txt", "w") as f:
        f.write(testcases_part.strip())
    return render_template('index.html', question=question)

@app.route('/submit_code', methods=['POST'])
def submit_code():
    code = request.form.get('code-input')
    language = request.form.get('language')
    stdin = request.form.get('stdin', "")
    action = request.form.get('action')

    if action == "run":
        payload = {
            "language": language,
            "version": "*",
            "stdin": stdin,
            "files": [{"name": "main", "content": code}]
        }
        response = requests.post("https://emkc.org/api/v2/piston/execute", json=payload).json()
        output = response.get("run", {}).get("stdout", "").strip()
        return render_template('index.html', question=open(FILE).read().split("\n",1)[1], code=code, output=output)

    # Otherwise, do full testcase evaluation
    with open(FILE, "r") as f:
        f.readline()
        question = f.read()
    if os.path.exists("testcases.txt"):
        with open("testcases.txt", "r") as f:
            testcases_text = f.read()
    else:
        testcases_text = ""
    testcases = load_testcases(testcases_text) if testcases_text else load_testcases(question)

    passed = 0
    total = len(testcases)
    for inp, expected_output in testcases:
        payload = {
            "language": language,
            "version": "*",
            "stdin": inp + "\n",
            "files": [{"name": "main", "content": code}]
        }
        response = requests.post("https://emkc.org/api/v2/piston/execute", json=payload).json()
        output = response.get("run", {}).get("stdout", "").strip()
        if normalize(output) == normalize(expected_output):
            passed += 1
    verdict = 'AC' if passed == total else 'WA'
    return render_template('index.html', question=question, code=code, verdict=verdict, passed=passed, total=total)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)