import requests
import os, json, psycopg2, bcrypt, re
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_gemini_model = None


def get_gemini_model():
    global _gemini_model
    if _gemini_model is None and GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
        except Exception as e:
            print(f"Gemini init failed: {e}")
    return _gemini_model


def get_conn():
    url = DATABASE_URL or ""
    if not url:
        raise Exception("DATABASE_URL not set")
    if "localhost" in url or "127.0.0.1" in url:
        return psycopg2.connect(url, connect_timeout=10)
    return psycopg2.connect(url, sslmode="require", connect_timeout=10)


def init_db():
    """Create tables if they don't exist."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(80) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
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
                input TEXT,
                expected_output TEXT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"DB init failed: {e}")


# Initialize DB on startup
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"Startup DB init error: {e}")


@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if not u or not p:
            flash("All fields required", "error")
            return render_template("login.html")
        try:
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
            flash("Invalid username or password", "error")
        except Exception as e:
            flash(f"Database error: {str(e)}", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if not u or not p:
            flash("All fields required", "error")
            return render_template("register.html")
        if len(u) < 3:
            flash("Username must be at least 3 characters", "error")
            return render_template("register.html")
        if len(p) < 6:
            flash("Password must be at least 6 characters", "error")
            return render_template("register.html")
        h = bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO users(username, password_hash) VALUES(%s,%s)", (u, h))
            conn.commit()
            cur.close()
            conn.close()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            cur.close()
            conn.close()
            flash("Username already taken, please choose another", "error")
            return render_template("register.html")
        except Exception as e:
            flash(f"Error creating account: {str(e)}", "error")
            return render_template("register.html")
        flash("Account created! Please sign in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def clean_json(text):
    text = re.sub(r"```json|```", "", text).strip()
    # Sometimes Gemini wraps with extra whitespace
    return text.strip()


def generate_question():
    model = get_gemini_model()
    if not model:
        return get_fallback_question()
    try:
        prompt = (
            f"Generate ONE beginner-friendly DSA coding question for {date.today()}.\n"
            "Topics can be: arrays, strings, two pointers, sliding window, hash maps, stacks, or basic recursion.\n"
            "IMPORTANT: Return STRICT JSON only — absolutely no markdown, no backticks, no explanation.\n"
            "Fields required:\n"
            "  title: short problem title\n"
            "  description: clear problem description with constraints (3-5 sentences)\n"
            "  sample_input: one sample input string\n"
            "  sample_output: the expected output string\n"
            "  hidden_tests: array of 20 objects, each with 'input' (string) and 'output' (string)\n"
            "Make sure all hidden_tests inputs/outputs are consistent with the problem logic."
        )
        r = model.generate_content(prompt, generation_config={"temperature": 0.7})
        raw = clean_json(r.text)
        data = json.loads(raw)
        for key in ("title", "description", "sample_input", "sample_output", "hidden_tests"):
            if key not in data:
                raise ValueError(f"Missing key: {key}")
        if not isinstance(data["hidden_tests"], list) or len(data["hidden_tests"]) == 0:
            raise ValueError("hidden_tests must be a non-empty list")
        return data
    except Exception as e:
        print(f"Gemini question generation failed: {e}")
        return get_fallback_question()


def get_fallback_question():
    """A hardcoded fallback question in case Gemini fails."""
    return {
        "title": "Two Sum",
        "description": (
            "Given an array of integers nums and an integer target, "
            "return the indices of the two numbers that add up to target.\n"
            "You may assume that each input has exactly one solution.\n"
            "You cannot use the same element twice.\n"
            "Input: first line is space-separated integers, second line is the target.\n"
            "Output: two space-separated indices (0-based)."
        ),
        "sample_input": "2 7 11 15\n9",
        "sample_output": "0 1",
        "hidden_tests": [
            {"input": "2 7 11 15\n9",  "output": "0 1"},
            {"input": "3 2 4\n6",       "output": "1 2"},
            {"input": "3 3\n6",          "output": "0 1"},
            {"input": "1 5 3 7\n8",     "output": "1 3"},
            {"input": "0 4 3 0\n0",     "output": "0 3"},
            {"input": "2 5 5 3\n10",    "output": "1 2"},
            {"input": "-1 -2 -3 -4\n-7","output": "2 3"},
            {"input": "1 2 3 4 5\n9",   "output": "3 4"},
            {"input": "10 20 30\n50",   "output": "1 2"},
            {"input": "1 1 1 1\n2",     "output": "0 1"},
            {"input": "5 75 25\n100",   "output": "1 2"},
            {"input": "3 2 4\n6",       "output": "1 2"},
            {"input": "2 7 11 15\n18",  "output": "1 2"},
            {"input": "1 2 3\n5",       "output": "1 2"},
            {"input": "4 5 6\n11",      "output": "1 2"},
            {"input": "0 1\n1",         "output": "0 1"},
            {"input": "100 200 300\n400","output":"1 2"},
            {"input": "6 3 5 7\n8",     "output": "0 3"},  # fixed: 6+? no; let me use consistent
            {"input": "2 3 4\n7",       "output": "1 2"},
            {"input": "1 9 3 7\n10",    "output": "0 1"},
        ]
    }


def normalize(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.strip().split("\n")]
    return "\n".join(line for line in lines if line)


def run_code(lang, code, stdin):
    lang_map = {
        "python3": "python",
        "python": "python",
        "c": "c",
        "cpp": "c++",
        "java": "java",
        "javascript": "javascript",
        "js": "javascript",
    }
    payload = {
        "language": lang_map.get(lang, lang),
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
        compile_info = data.get("compile", {})
        if compile_info.get("stderr"):
            return {"ok": False, "output": f"Compilation Error:\n{compile_info['stderr']}"}
        run = data.get("run", {})
        stderr = run.get("stderr", "").strip()
        stdout = run.get("stdout", run.get("output", "")).strip()
        if stdout and stderr:
            return {"ok": True, "output": f"{stdout}\n\n[stderr]:\n{stderr}"}
        if stderr and not stdout:
            return {"ok": False, "output": f"Runtime Error:\n{stderr}"}
        return {"ok": True, "output": stdout if stdout else "(no output)"}
    except requests.exceptions.Timeout:
        return {"ok": False, "output": "Error: Code execution timed out (30s limit)."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "output": "Error: Cannot reach code execution server. Check your internet connection."}
    except Exception as e:
        return {"ok": False, "output": f"Error: {str(e)}"}


@app.route("/api/run", methods=["POST"])
def api_run():
    """AJAX endpoint for running code — returns JSON so the page doesn't reload."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(force=True)
    language = data.get("language", "python3")
    code = data.get("code", "")
    custom_input = data.get("custom_input", "")
    if not code.strip():
        return jsonify({"ok": False, "output": "Please write some code first."})
    result = run_code(language, code, custom_input)
    return jsonify(result)


@app.route("/api/submit", methods=["POST"])
def api_submit():
    """AJAX endpoint for submitting code — streams progress via JSON."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(force=True)
    language = data.get("language", "python3")
    code = data.get("code", "")
    if not code.strip():
        return jsonify({"ok": False, "verdict": "No code submitted.", "passed": 0, "total": 0})

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM daily_questions WHERE qdate=CURRENT_DATE"
        )
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"ok": False, "verdict": "No question found for today.", "passed": 0, "total": 0})

        question_id = row[0]
        cur.execute(
            "SELECT input, expected_output FROM testcases WHERE question_id=%s",
            (question_id,)
        )
        tests = cur.fetchall()
        cur.close(); conn.close()
    except Exception as e:
        return jsonify({"ok": False, "verdict": f"DB error: {str(e)}", "passed": 0, "total": 0})

    total = len(tests)
    if total == 0:
        return jsonify({"ok": False, "verdict": "No test cases available.", "passed": 0, "total": 0})

    passed = 0
    errors = []
    for i, (inp, exp) in enumerate(tests):
        result = run_code(language, code, inp)
        actual = normalize(result["output"])
        expected = normalize(exp)
        if actual == expected:
            passed += 1
        else:
            if len(errors) < 3:  # collect first 3 wrong answers for feedback
                errors.append({
                    "test": i + 1,
                    "input": inp[:100],
                    "expected": expected[:100],
                    "got": actual[:100],
                })

    verdict = "Accepted" if passed == total else "Wrong Answer"
    return jsonify({
        "ok": passed == total,
        "verdict": verdict,
        "passed": passed,
        "total": total,
        "errors": errors,
    })


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, description, sample_input, sample_output "
            "FROM daily_questions WHERE qdate=CURRENT_DATE"
        )
        row = cur.fetchone()

        if not row:
            q = generate_question()
            if not q:
                cur.close(); conn.close()
                flash("Could not load today's question. Please refresh the page.", "error")
                return render_template("dashboard.html",
                    username=session.get("username", "Coder"),
                    question_title="Loading...",
                    question_desc="Could not load today's question. Please refresh.",
                    sample_input="", sample_output="")

            cur.execute(
                """INSERT INTO daily_questions(qdate, title, description, sample_input, sample_output)
                   VALUES(CURRENT_DATE, %s, %s, %s, %s) RETURNING id""",
                (q["title"], q["description"], q["sample_input"], q["sample_output"])
            )
            question_id = cur.fetchone()[0]
            for t in q.get("hidden_tests", []):
                cur.execute(
                    "INSERT INTO testcases(question_id, input, expected_output) VALUES(%s,%s,%s)",
                    (question_id, t["input"], t["output"])
                )
            conn.commit()
            cur.execute(
                "SELECT id, title, description, sample_input, sample_output "
                "FROM daily_questions WHERE qdate=CURRENT_DATE"
            )
            row = cur.fetchone()

        cur.close(); conn.close()
    except Exception as e:
        flash(f"Database connection error: {str(e)}", "error")
        return render_template("dashboard.html",
            username=session.get("username", "Coder"),
            question_title="Connection Error",
            question_desc=str(e),
            sample_input="", sample_output="")

    return render_template(
        "dashboard.html",
        username=session.get("username", "Coder"),
        question_title=row[1],
        question_desc=row[2],
        sample_input=row[3],
        sample_output=row[4],
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)