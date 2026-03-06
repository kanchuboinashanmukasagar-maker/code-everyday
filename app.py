import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import re
import traceback
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
from dotenv import load_dotenv

# ─── Bootstrap ────────────────────────────────────────────────────────────────
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DAILY_PROBLEM_FILE = "daily_problem.json"
DATABASE = "database.db"
CODE_TIMEOUT = 5  # seconds


# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT    UNIQUE NOT NULL,
                email     TEXT    UNIQUE NOT NULL,
                password  TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                problem_date  TEXT    NOT NULL,
                code          TEXT    NOT NULL,
                passed        INTEGER NOT NULL DEFAULT 0,
                total         INTEGER NOT NULL DEFAULT 0,
                status        TEXT    NOT NULL DEFAULT 'attempted',
                submitted_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS daily_problems (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_date TEXT UNIQUE NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL,
                testcases    TEXT NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)


init_db()


# ─── Auth Decorator ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─── Daily Problem Logic ──────────────────────────────────────────────────────
def get_today_str():
    return date.today().isoformat()


def load_problem_from_db(today_str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_problems WHERE problem_date = ?", (today_str,)
        ).fetchone()
    if row:
        return {
            "date": row["problem_date"],
            "title": row["title"],
            "description": row["description"],
            "testcases": json.loads(row["testcases"])
        }
    return None


def save_problem_to_db(today_str, problem):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO daily_problems
               (problem_date, title, description, testcases)
               VALUES (?, ?, ?, ?)""",
            (
                today_str,
                problem["title"],
                problem["description"],
                json.dumps(problem["testcases"])
            )
        )


def load_problem_from_json():
    if not os.path.exists(DAILY_PROBLEM_FILE):
        return None
    try:
        with open(DAILY_PROBLEM_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == get_today_str():
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_problem_to_json(today_str, problem):
    payload = {
        "date": today_str,
        "title": problem["title"],
        "description": problem["description"],
        "testcases": problem["testcases"]
    }
    with open(DAILY_PROBLEM_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def generate_problem_with_gemini():
    if not GEMINI_API_KEY:
        # Fallback problem when no API key is configured
        return {
            "title": "Sum of Array",
            "description": (
                "Given an array of integers, return the sum of all elements.\n\n"
                "**Input Format:**\n"
                "- First line: integer `n` (number of elements)\n"
                "- Second line: `n` space-separated integers\n\n"
                "**Output Format:**\n"
                "- A single integer — the sum of all elements\n\n"
                "**Example:**\n"
                "```\nInput:\n4\n1 2 3 4\n\nOutput:\n10\n```"
            ),
            "testcases": [
                {"input": "4\n1 2 3 4", "output": "10"},
                {"input": "3\n10 20 30", "output": "60"},
                {"input": "1\n7", "output": "7"},
                {"input": "5\n-1 -2 3 4 5", "output": "9"},
            ]
        }

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = """
You are a competitive programming problem setter.
Generate a beginner-to-intermediate Python coding challenge for today.

Return ONLY valid JSON — no markdown fences, no extra text.
The JSON must follow this exact schema:

{
  "title": "Problem Title",
  "description": "Full problem description with input/output format and at least one example. Use plain text only.",
  "testcases": [
    {"input": "...", "output": "..."},
    {"input": "...", "output": "..."},
    {"input": "...", "output": "..."},
    {"input": "...", "output": "..."}
  ]
}

Rules:
- 3 to 5 test cases
- Problems must be solvable in Python 3
- Inputs/outputs should be plain text (newlines allowed)
- Description must be clear and self-contained
- Vary the topic: arrays, strings, math, sorting, etc.
- The output for each test case must be exact (no trailing spaces)
"""

        response = model.generate_content(prompt)
        raw = response.text.strip()

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE)

        problem = json.loads(raw)

        # Validate structure
        assert "title" in problem
        assert "description" in problem
        assert "testcases" in problem
        assert isinstance(problem["testcases"], list)
        assert len(problem["testcases"]) >= 1

        return problem

    except Exception as e:
        app.logger.error(f"Gemini generation failed: {e}")
        # Return a safe fallback
        return {
            "title": "Count Vowels",
            "description": (
                "Given a string, count the number of vowels (a, e, i, o, u — case-insensitive).\n\n"
                "**Input Format:**\n"
                "- A single line containing a string\n\n"
                "**Output Format:**\n"
                "- A single integer — number of vowels\n\n"
                "**Example:**\n"
                "```\nInput:\nHello World\n\nOutput:\n3\n```"
            ),
            "testcases": [
                {"input": "Hello World", "output": "3"},
                {"input": "aeiou", "output": "5"},
                {"input": "rhythm", "output": "0"},
                {"input": "Python Programming", "output": "5"},
            ]
        }


def get_or_create_daily_problem():
    today_str = get_today_str()

    # 1. Try database first
    problem = load_problem_from_db(today_str)
    if problem:
        return problem

    # 2. Try JSON cache
    problem = load_problem_from_json()
    if problem:
        save_problem_to_db(today_str, problem)
        return problem

    # 3. Generate fresh
    raw_problem = generate_problem_with_gemini()
    save_problem_to_json(today_str, raw_problem)
    save_problem_to_db(today_str, raw_problem)

    return {
        "date": today_str,
        "title": raw_problem["title"],
        "description": raw_problem["description"],
        "testcases": raw_problem["testcases"]
    }


# ─── Code Execution Engine ────────────────────────────────────────────────────
def clean_error(stderr_output: str) -> str:
    """Convert raw Python tracebacks into friendly error messages."""
    if not stderr_output:
        return ""

    lines = stderr_output.strip().splitlines()

    # Find the actual error line (last non-empty line)
    error_line = ""
    for line in reversed(lines):
        line = line.strip()
        if line:
            error_line = line
            break

    if not error_line:
        return "Runtime Error: Unknown error occurred."

    # Map common exceptions to friendly messages
    patterns = [
        (r"NameError: name '(\w+)' is not defined", lambda m: f"NameError: variable '{m.group(1)}' is not defined"),
        (r"ZeroDivisionError",                      lambda m: "ZeroDivisionError: division by zero"),
        (r"IndexError: list index out of range",    lambda m: "IndexError: list index is out of range"),
        (r"ValueError: (.+)",                       lambda m: f"ValueError: {m.group(1)}"),
        (r"TypeError: (.+)",                        lambda m: f"TypeError: {m.group(1)}"),
        (r"KeyError: (.+)",                         lambda m: f"KeyError: key {m.group(1)} not found"),
        (r"AttributeError: (.+)",                   lambda m: f"AttributeError: {m.group(1)}"),
        (r"SyntaxError: (.+)",                      lambda m: f"SyntaxError: {m.group(1)}"),
        (r"IndentationError: (.+)",                 lambda m: f"IndentationError: {m.group(1)}"),
        (r"ImportError: (.+)",                      lambda m: f"ImportError: {m.group(1)}"),
        (r"ModuleNotFoundError: (.+)",              lambda m: f"ModuleNotFoundError: {m.group(1)}"),
        (r"RecursionError",                         lambda m: "RecursionError: maximum recursion depth exceeded"),
        (r"MemoryError",                            lambda m: "MemoryError: program used too much memory"),
        (r"OverflowError: (.+)",                    lambda m: f"OverflowError: {m.group(1)}"),
        (r"StopIteration",                          lambda m: "StopIteration: iterator exhausted unexpectedly"),
    ]

    for pattern, formatter in patterns:
        match = re.search(pattern, error_line)
        if match:
            return formatter(match)

    # Generic fallback — strip file paths
    clean = re.sub(r'File ".*?", line \d+, in .+', "", error_line).strip()
    return f"Runtime Error: {clean}" if clean else "Runtime Error: An unexpected error occurred."


def execute_code(code: str, stdin_input: str = "") -> dict:
    """
    Execute Python code safely in a subprocess.
    Returns dict with keys: stdout, stderr, timed_out, exit_code
    """
    # Write code to a temporary file
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=CODE_TIMEOUT,
            encoding="utf-8",
            errors="replace"
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "exit_code": -1
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "timed_out": False,
            "exit_code": -1
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def judge_code(code: str, testcases: list) -> dict:
    """
    Run code against all test cases.
    Returns: { passed, total, first_failure }
    """
    passed = 0
    total = len(testcases)
    first_failure = None

    for tc in testcases:
        stdin = tc.get("input", "")
        expected = tc.get("output", "").strip()

        result = execute_code(code, stdin)

        if result["timed_out"]:
            actual = "[Time Limit Exceeded]"
        elif result["exit_code"] != 0:
            actual = clean_error(result["stderr"]) or "[Runtime Error]"
        else:
            actual = result["stdout"].strip()

        if actual == expected:
            passed += 1
        else:
            if first_failure is None:
                first_failure = {
                    "input": stdin,
                    "expected": expected,
                    "actual": actual
                }

    return {
        "passed": passed,
        "total": total,
        "first_failure": first_failure
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

# ---------- Auth ----------
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        # Validation
        errors = []
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not email or "@" not in email:
            errors.append("Enter a valid email address.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html",
                                   username=username, email=email)

        hashed = generate_password_hash(password)
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                    (username, email, hashed)
                )
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError as e:
            if "username" in str(e):
                flash("Username already taken.", "error")
            elif "email" in str(e):
                flash("Email already registered.", "error")
            else:
                flash("Registration failed. Please try again.", "error")
            return render_template("register.html",
                                   username=username, email=email)

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password   = request.form.get("password", "")

        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ? OR email = ?",
                (identifier, identifier.lower())
            ).fetchone()

        if not user or not check_password_hash(user["password"], password):
            flash("Invalid credentials. Please try again.", "error")
            return render_template("login.html", identifier=identifier)

        session["user_id"]  = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------- Dashboard ----------
@app.route("/dashboard")
@login_required
def dashboard():
    today_str = get_today_str()
    user_id   = session["user_id"]

    with get_db() as conn:
        submissions = conn.execute(
            """SELECT * FROM submissions
               WHERE user_id = ?
               ORDER BY submitted_at DESC
               LIMIT 20""",
            (user_id,)
        ).fetchall()

        today_sub = conn.execute(
            """SELECT * FROM submissions
               WHERE user_id = ? AND problem_date = ?
               ORDER BY submitted_at DESC LIMIT 1""",
            (user_id, today_str)
        ).fetchone()

    problem = get_or_create_daily_problem()

    return render_template(
        "dashboard.html",
        username=session["username"],
        problem=problem,
        submissions=submissions,
        today_sub=today_sub,
        today=today_str,
        now_hour=datetime.now().hour
    )


# ---------- Problem Page ----------
@app.route("/problem")
@login_required
def problem():
    p = get_or_create_daily_problem()
    return render_template("problem.html",
                           problem=p,
                           username=session["username"])


# ---------- API: Run Code ----------
@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    data  = request.get_json(force=True)
    code  = data.get("code", "")
    stdin = data.get("input", "")

    if not code.strip():
        return jsonify({"error": "No code provided."}), 400

    result = execute_code(code, stdin)

    if result["timed_out"]:
        return jsonify({
            "output": "",
            "error":  "Time Limit Exceeded: Your code ran for more than 5 seconds."
        })

    if result["exit_code"] != 0:
        return jsonify({
            "output": result["stdout"],
            "error":  clean_error(result["stderr"])
        })

    return jsonify({
        "output": result["stdout"],
        "error":  ""
    })


# ---------- API: Submit Code ----------
@app.route("/api/submit", methods=["POST"])
@login_required
def api_submit():
    data    = request.get_json(force=True)
    code    = data.get("code", "")
    user_id = session["user_id"]
    today   = get_today_str()

    if not code.strip():
        return jsonify({"error": "No code provided."}), 400

    problem   = get_or_create_daily_problem()
    testcases = problem["testcases"]

    verdict = judge_code(code, testcases)

    status = "solved" if verdict["passed"] == verdict["total"] else "attempted"

    with get_db() as conn:
        conn.execute(
            """INSERT INTO submissions
               (user_id, problem_date, code, passed, total, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, today, code,
             verdict["passed"], verdict["total"], status)
        )

    response = {
        "passed": verdict["passed"],
        "total":  verdict["total"],
        "status": status
    }

    if verdict["first_failure"]:
        response["first_failure"] = verdict["first_failure"]

    return jsonify(response)


# ---------- API: Get Problem ----------
@app.route("/api/problem")
@login_required
def api_problem():
    problem = get_or_create_daily_problem()
    # Never expose testcases through API
    return jsonify({
        "date":        problem["date"],
        "title":       problem["title"],
        "description": problem["description"]
    })


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
