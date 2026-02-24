import requests
import os, json, psycopg2, bcrypt, re
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash, session
import google.generativeai as genai

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Create tables if they don't exist yet."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_questions (
            id SERIAL PRIMARY KEY,
            qdate DATE UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            sample_input TEXT,
            sample_output TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS testcases (
            id SERIAL PRIMARY KEY,
            question_id INTEGER REFERENCES daily_questions(id),
            input TEXT NOT NULL,
            expected_output TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# Run DB init on startup
try:
    init_db()
except Exception as e:
    print(f"DB init warning: {e}")


# ─── Auth Routes ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if not u or not p:
            flash("All fields are required.", "error")
            return render_template("login.html")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (u,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and bcrypt.checkpw(p.encode(), row[1].encode()):
            session["user_id"] = row[0]
            session["username"] = u
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if not u or not p:
            flash("All fields are required.", "error")
            return render_template("register.html")
        if len(u) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")
        if len(p) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        h = bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users(username, password_hash) VALUES(%s,%s)", (u, h))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            flash("Username already exists. Please choose another.", "error")
            cur.close()
            conn.close()
            return render_template("register.html")
        cur.close()
        conn.close()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Helpers ────────────────────────────────────────────────────────────────

def clean_json(text):
    text = re.sub(r"```json|```", "", text)
    return text.strip()


PLACEHOLDER_QUESTION = {
    "title": "Sum of Two Numbers",
    "description": (
        "Given two integers A and B, print their sum.\n\n"
        "**Input Format**\nA single line containing two space-separated integers A and B.\n\n"
        "**Output Format**\nPrint a single integer — the sum of A and B.\n\n"
        "**Constraints**\n-10^9 ≤ A, B ≤ 10^9"
    ),
    "sample_input": "2 3",
    "sample_output": "5",
    "hidden_tests": [
        {"input": "2 3",       "output": "5"},
        {"input": "10 15",     "output": "25"},
        {"input": "100 200",   "output": "300"},
        {"input": "0 0",       "output": "0"},
        {"input": "-5 5",      "output": "0"},
        {"input": "-10 -20",   "output": "-30"},
        {"input": "999 1",     "output": "1000"},
        {"input": "1000000000 -1000000000", "output": "0"},
        {"input": "7 8",       "output": "15"},
        {"input": "50 50",     "output": "100"},
        {"input": "1 1",       "output": "2"},
        {"input": "-1 -1",     "output": "-2"},
        {"input": "123 456",   "output": "579"},
        {"input": "0 1",       "output": "1"},
        {"input": "1 0",       "output": "1"},
        {"input": "-100 100",  "output": "0"},
        {"input": "500 500",   "output": "1000"},
        {"input": "999999999 1","output": "1000000000"},
        {"input": "-999 1000", "output": "1"},
        {"input": "42 58",     "output": "100"},
    ]
}


def generate_question():
    if not GEMINI_API_KEY:
        return PLACEHOLDER_QUESTION
    try:
        prompt = (
            f"Generate ONE beginner-to-intermediate DSA coding question for {date.today()}.\n"
            "The question should test a data structure or algorithm concept (arrays, strings, "
            "sorting, searching, stacks, queues, linked lists, recursion, etc.).\n"
            "Return STRICT JSON only — no markdown, no explanation, just the JSON object.\n"
            "Fields required:\n"
            "  title: string\n"
            "  description: string (include Input Format, Output Format, Constraints sections)\n"
            "  sample_input: string\n"
            "  sample_output: string\n"
            "  hidden_tests: array of at least 20 objects each with 'input' and 'output' keys\n"
            "Only output the raw JSON."
        )
        r = model.generate_content(prompt, generation_config={"temperature": 0.85})
        text = clean_json(r.text)
        data = json.loads(text)
        # Validate required keys
        for key in ("title", "description", "sample_input", "sample_output", "hidden_tests"):
            if key not in data:
                raise ValueError(f"Missing key: {key}")
        return data
    except Exception as e:
        print(f"Gemini question generation failed: {e}")
        return PLACEHOLDER_QUESTION


def normalize(text):
    """
    Normalize output for comparison:
    - Strip leading/trailing whitespace from the whole output
    - Strip each line individually
    - Remove blank lines
    - Normalize \r\n to \n
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.strip().split("\n")]
    lines = [line for line in lines if line != ""]
    return "\n".join(lines)


def run_code(lang, code, stdin):
    """Run code via Piston API and return stdout output."""
    # Map friendly language names to Piston language IDs
    lang_map = {
        "python3": "python",
        "python":  "python",
        "c":       "c",
        "cpp":     "c++",
        "java":    "java",
        "javascript": "javascript",
        "js":      "javascript",
    }
    piston_lang = lang_map.get(lang, lang)

    payload = {
        "language": piston_lang,
        "version": "*",
        "files": [{"name": "main", "content": code}],
        "stdin": stdin or "",
        "compile_timeout": 10000,
        "run_timeout": 5000,
    }
    try:
        r = requests.post(
            "https://emkc.org/api/v2/piston/execute",
            json=payload,
            timeout=30
        )
        r.raise_for_status()
        data = r.json()

        # Check for compile errors
        compile_info = data.get("compile", {})
        if compile_info.get("stderr"):
            return f"Compilation Error:\n{compile_info['stderr']}"

        run = data.get("run", {})
        stderr = run.get("stderr", "").strip()
        stdout = run.get("stdout", run.get("output", "")).strip()

        if stdout and stderr:
            return f"{stdout}\n\nStderr:\n{stderr}"
        if stderr and not stdout:
            return f"Runtime Error:\n{stderr}"
        return stdout if stdout else "(no output)"

    except requests.exceptions.Timeout:
        return "Error: Code execution timed out (>30s). Try a simpler test case."
    except requests.exceptions.ConnectionError:
        return "Error: Could not connect to code execution server. Check your internet connection."
    except Exception as e:
        return f"Error: {str(e)}"


@app.route("/test-piston")
def test_piston():
    """Debug route to check if Piston API is reachable."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    result = run_code("python3", "print('hello from piston')", "")
    return f"<pre>Piston test result: {result!r}</pre>"


# ─── Dashboard ──────────────────────────────────────────────────────────────

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_conn()
    cur = conn.cursor()

    # Fetch or generate today's question
    cur.execute(
        "SELECT id, title, description, sample_input, sample_output "
        "FROM daily_questions WHERE qdate=CURRENT_DATE"
    )
    row = cur.fetchone()

    if not row:
        q = generate_question()
        cur.execute(
            """INSERT INTO daily_questions(qdate, title, description, sample_input, sample_output)
               VALUES(CURRENT_DATE, %s, %s, %s, %s) RETURNING id""",
            (q["title"], q["description"], q["sample_input"], q["sample_output"])
        )
        question_id = cur.fetchone()[0]
        for t in q.get("hidden_tests", []):
            cur.execute(
                "INSERT INTO testcases(question_id, input, expected_output) VALUES(%s, %s, %s)",
                (question_id, t["input"], t["output"])
            )
        conn.commit()
        cur.execute(
            "SELECT id, title, description, sample_input, sample_output "
            "FROM daily_questions WHERE qdate=CURRENT_DATE"
        )
        row = cur.fetchone()

    output = None
    verdict = None
    passed = 0
    total = 0
    last_code = ""
    last_language = "python3"
    last_custom_input = ""

    if request.method == "POST":
        language = request.form.get("language", "python3")
        code = request.form.get("code-input", "")
        custom_input = request.form.get("custom-input", "")
        action = request.form.get("action")

        # Preserve user's code and settings on postback
        last_code = code
        last_language = language
        last_custom_input = custom_input

        if action == "run":
            output = run_code(language, code, custom_input)
            if not output:
                output = "(no output)"

        elif action == "submit":
            cur.execute(
                "SELECT input, expected_output FROM testcases WHERE question_id=%s",
                (row[0],)
            )
            tests = cur.fetchall()
            total = len(tests)
            if total == 0:
                verdict = "No test cases found for this question."
            else:
                for inp, exp in tests:
                    result = (run_code(language, code, inp) or "").strip()
                    if result == exp.strip():
                        passed += 1
                verdict = "Accepted ✓" if passed == total else "Wrong Answer ✗"

    cur.close()
    conn.close()

    return render_template(
        "dashboard.html",
        username=session.get("username", "Coder"),
        question_id=row[0],
        question_title=row[1],
        question_desc=row[2],
        sample_input=row[3],
        sample_output=row[4],
        output=output,
        verdict=verdict,
        passed=passed,
        total=total,
        last_code=last_code,
        last_language=last_language,
        last_custom_input=last_custom_input,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))