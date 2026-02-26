import requests
import os, json, psycopg2, bcrypt, re, base64
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
DATABASE_URL   = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
JUDGE0_API_KEY = os.getenv("JUDGE0_API_KEY", "")
JUDGE0_HOST    = "judge0-ce.p.rapidapi.com"
JUDGE0_URL     = "https://judge0-ce.p.rapidapi.com/submissions"

JUDGE0_LANG_IDS = {
    "python3":    71,
    "python":     71,
    "c":          50,
    "cpp":        54,
    "java":       62,
    "javascript": 63,
}

# ── Gemini ────────────────────────────────────────────────────
_gemini_model = None

def get_gemini_model():
    global _gemini_model
    if _gemini_model is None and GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
        except Exception as e:
            print(f"Gemini init error: {e}")
    return _gemini_model


# ── Database ──────────────────────────────────────────────────
def get_conn():
    url = DATABASE_URL or ""
    if not url:
        raise Exception("DATABASE_URL is not set")
    if "localhost" in url or "127.0.0.1" in url:
        return psycopg2.connect(url, connect_timeout=10)
    return psycopg2.connect(url, sslmode="require", connect_timeout=10)


def init_db():
    try:
        conn = get_conn(); cur = conn.cursor()
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
                question_id INTEGER REFERENCES daily_questions(id) ON DELETE CASCADE,
                input TEXT,
                expected_output TEXT
            )
        """)
        conn.commit(); cur.close(); conn.close()
        print("DB ready")
    except Exception as e:
        print(f"DB init error: {e}")


with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"Startup error: {e}")


# ── Auth ──────────────────────────────────────────────────────
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
            conn = get_conn(); cur = conn.cursor()
            cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (u,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row and bcrypt.checkpw(p.encode(), row[1].encode()):
                session["user_id"]  = row[0]
                session["username"] = u
                return redirect(url_for("dashboard"))
            flash("Invalid username or password", "error")
        except Exception as e:
            flash(f"Database error: {e}", "error")
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
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO users(username, password_hash) VALUES(%s,%s)", (u, h))
            conn.commit(); cur.close(); conn.close()
        except psycopg2.errors.UniqueViolation:
            conn.rollback(); cur.close(); conn.close()
            flash("Username already taken", "error")
            return render_template("register.html")
        except Exception as e:
            flash(f"Error: {e}", "error")
            return render_template("register.html")
        flash("Account created! Please sign in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Gemini Question Generation ────────────────────────────────
def clean_json(text):
    """Strip markdown fences Gemini sometimes adds and extract the JSON object."""
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*",     "", text)
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1].strip()
    return text.strip()


def generate_question():
    """Ask Gemini to create today's DSA question. Returns dict or None on failure."""
    model = get_gemini_model()
    if not model:
        return None

    prompt = """You are a coding challenge generator for a daily DSA practice app.

Generate ONE beginner-to-intermediate DSA problem. 

STRICT RULES:
- Output ONLY a valid JSON object. Nothing else.
- No markdown. No backticks. No explanation before or after.
- The JSON must follow this EXACT structure:

{
  "title": "Problem title here",
  "description": "Clear description of the problem. State the input format and output format. 3 to 5 sentences.",
  "sample_input": "one example input",
  "sample_output": "the correct output for that input",
  "hidden_tests": [
    {"input": "test input", "output": "expected output"},
    ... exactly 20 test cases total
  ]
}

Topic must be one of: arrays, strings, hash maps, stacks, two pointers, sliding window, or basic sorting.
Make sure all 20 hidden test inputs and outputs are logically correct.
"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.9, "max_output_tokens": 3000}
        )
        raw = response.text
        print(f"Gemini raw (first 300 chars): {raw[:300]}")

        data = json.loads(clean_json(raw))

        # Validate fields
        for key in ("title", "description", "sample_input", "sample_output", "hidden_tests"):
            if key not in data:
                raise ValueError(f"Missing field: {key}")
        if not isinstance(data["hidden_tests"], list) or len(data["hidden_tests"]) < 5:
            raise ValueError(f"Too few test cases: {len(data.get('hidden_tests', []))}")
        for i, t in enumerate(data["hidden_tests"]):
            if "input" not in t or "output" not in t:
                raise ValueError(f"Test case {i+1} missing input/output")

        print(f"Gemini question OK: '{data['title']}' — {len(data['hidden_tests'])} tests")
        return data

    except json.JSONDecodeError as e:
        print(f"Gemini returned invalid JSON: {e}\nRaw: {response.text[:500]}")
        return None
    except Exception as e:
        print(f"Gemini generation failed: {e}")
        return None


# ── Admin: force refresh today's question ─────────────────────
# Visit /admin/refresh-question?token=YOUR_ADMIN_TOKEN
# Set ADMIN_TOKEN in Render environment variables
@app.route("/admin/refresh-question")
def admin_refresh_question():
    admin_token = os.getenv("ADMIN_TOKEN", "")
    if not admin_token or request.args.get("token") != admin_token:
        return "<h3>401 Unauthorized</h3><p>Set ADMIN_TOKEN in Render env variables and pass ?token= in the URL</p>", 401
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id FROM daily_questions WHERE qdate = CURRENT_DATE")
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM testcases       WHERE question_id = %s", (row[0],))
            cur.execute("DELETE FROM daily_questions WHERE id = %s",          (row[0],))
            conn.commit()
            print(f"Deleted today's question id={row[0]} for refresh")
        cur.close(); conn.close()
    except Exception as e:
        return f"<h3>DB Error</h3><p>{e}</p>", 500
    flash("Question refreshed! Gemini is generating a new one.", "success")
    return redirect(url_for("dashboard"))


# ── Debug: check if Gemini is working ─────────────────────────
# Visit /debug-gemini in your browser to test your Gemini API key
# You can remove this route once everything is confirmed working
@app.route("/debug-gemini")
def debug_gemini():
    html = ["<style>body{font-family:monospace;background:#0d0f14;color:#e2e8f0;padding:30px}"
            "pre{background:#1a2033;padding:14px;border-radius:6px;color:#4ade80;overflow-x:auto}"
            ".ok{color:#4ade80} .err{color:#f87171} .warn{color:#fbbf24}"
            "a{color:#38bdf8}</style>",
            "<h2>🔍 Gemini API Debug</h2>"]

    # Step 1: Check key
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        html.append("<p class='err'>❌ GEMINI_API_KEY is NOT set in your environment variables.</p>")
        html.append("<p>Go to <strong>Render → Your Service → Environment</strong> and add:<br>"
                    "<code>GEMINI_API_KEY = AIza...your_key</code></p>")
        html.append("<p>Get a free key at <a href='https://aistudio.google.com' target='_blank'>"
                    "aistudio.google.com</a> → Get API Key → Create API Key</p>")
        return "".join(html)

    html.append(f"<p class='ok'>✅ GEMINI_API_KEY is set (starts with: {key[:10]}...)</p>")

    # Step 2: Init model
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        html.append("<p class='ok'>✅ Gemini model initialized successfully</p>")
    except Exception as e:
        html.append(f"<p class='err'>❌ Gemini model init failed: {e}</p>")
        html.append("<p>Make sure <code>google-generativeai</code> is in your requirements.txt</p>")
        return "".join(html)

    # Step 3: Make a real API call
    try:
        test_prompt = (
            "Return ONLY this exact JSON, nothing else, no markdown:\n"
            '{"title":"Test","description":"Test desc",'
            '"sample_input":"1","sample_output":"1",'
            '"hidden_tests":[{"input":"1","output":"1"}]}'
        )
        resp     = model.generate_content(test_prompt, generation_config={"temperature": 0})
        raw_text = resp.text
        html.append("<p class='ok'>✅ Gemini API call succeeded!</p>")
        html.append(f"<p>Raw response from Gemini:</p><pre>{raw_text[:600]}</pre>")
    except Exception as e:
        html.append(f"<p class='err'>❌ Gemini API call failed: {e}</p>")
        html.append("<p class='warn'>This usually means the API key is invalid or expired.</p>")
        return "".join(html)

    # Step 4: Parse JSON
    try:
        cleaned = clean_json(raw_text)
        data    = json.loads(cleaned)
        html.append(f"<p class='ok'>✅ JSON parsed successfully — title: <strong>{data.get('title')}</strong></p>")
    except Exception as e:
        html.append(f"<p class='warn'>⚠️ JSON parse failed: {e}</p>")
        html.append("<p>Gemini API is working but returned extra text. "
                    "The app's clean_json() will handle this in most cases.</p>")

    html.append("<hr>")
    html.append("<p class='ok'><strong>Gemini is working! ✅</strong></p>")
    html.append("<p>Next steps:<br>"
                "1. Set <code>ADMIN_TOKEN=somepassword</code> in Render env variables<br>"
                "2. Visit <a href='/admin/refresh-question?token=somepassword'>"
                "/admin/refresh-question?token=somepassword</a> to delete the old question "
                "and let Gemini generate a fresh one.<br>"
                "3. Once confirmed working, you can delete the <code>/debug-gemini</code> route from app.py.</p>")
    return "".join(html)


# ── Judge0 code execution ─────────────────────────────────────
def _b64_encode(s):
    return base64.b64encode((s or "").encode()).decode()


def _b64_decode(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s).decode(errors="replace").strip()
    except Exception:
        return str(s).strip()


def run_code(lang, code, stdin):
    if not JUDGE0_API_KEY:
        return {
            "ok": False,
            "output": (
                "Code execution is not configured.\n\n"
                "Fix:\n"
                "1. Go to https://rapidapi.com/judge0-official/api/judge0-ce\n"
                "2. Subscribe to the FREE Basic plan\n"
                "3. Copy your X-RapidAPI-Key\n"
                "4. In Render → Environment → add:  JUDGE0_API_KEY = <your_key>"
            )
        }

    lang_id = JUDGE0_LANG_IDS.get(lang)
    if not lang_id:
        return {"ok": False, "output": f"Language '{lang}' is not supported."}

    headers = {
        "Content-Type":    "application/json",
        "X-RapidAPI-Key":  JUDGE0_API_KEY,
        "X-RapidAPI-Host": JUDGE0_HOST,
    }
    payload = {
        "language_id":    lang_id,
        "source_code":    _b64_encode(code),
        "stdin":          _b64_encode(stdin or ""),
        "base64_encoded": True,
    }

    try:
        resp = requests.post(
            JUDGE0_URL + "?base64_encoded=true&wait=true",
            json=payload, headers=headers, timeout=30,
        )
        if resp.status_code == 401:
            return {"ok": False, "output": "Error 401: Invalid JUDGE0_API_KEY. Check Render env variables."}
        if resp.status_code == 429:
            return {"ok": False, "output": "Rate limit reached. Please wait a moment and try again."}
        resp.raise_for_status()
        data = resp.json()

        stdout         = _b64_decode(data.get("stdout",         ""))
        stderr         = _b64_decode(data.get("stderr",         ""))
        compile_output = _b64_decode(data.get("compile_output", ""))
        message        = _b64_decode(data.get("message",        ""))
        status_id      = data.get("status", {}).get("id", 0)

        if compile_output:
            return {"ok": False, "output": f"Compilation Error:\n{compile_output}"}
        if status_id == 5:
            return {"ok": False, "output": "Time Limit Exceeded — your code is too slow."}
        if stderr and not stdout:
            return {"ok": False, "output": f"Runtime Error:\n{stderr}"}
        if stdout and stderr:
            return {"ok": True,  "output": f"{stdout}\n\n[stderr]:\n{stderr}"}
        if stdout:
            return {"ok": True,  "output": stdout}
        if message:
            return {"ok": False, "output": f"Error: {message}"}
        return {"ok": True, "output": "(no output)"}

    except requests.exceptions.Timeout:
        return {"ok": False, "output": "Request timed out. Try again."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "output": "Cannot connect to Judge0. Check internet."}
    except Exception as e:
        return {"ok": False, "output": f"Unexpected error: {e}"}


def normalize(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(l.strip() for l in text.strip().split("\n") if l.strip())


# ── AJAX endpoints ────────────────────────────────────────────
@app.route("/api/run", methods=["POST"])
def api_run():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "output": "Please write some code first."})
    return jsonify(run_code(body.get("language", "python3"), code, body.get("custom_input", "")))


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    body     = request.get_json(force=True)
    language = body.get("language", "python3")
    code     = body.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "verdict": "No code submitted.", "passed": 0, "total": 0})

    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id FROM daily_questions WHERE qdate = CURRENT_DATE")
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"ok": False, "verdict": "No question for today.", "passed": 0, "total": 0})
        cur.execute("SELECT input, expected_output FROM testcases WHERE question_id = %s", (row[0],))
        tests = cur.fetchall()
        cur.close(); conn.close()
    except Exception as e:
        return jsonify({"ok": False, "verdict": f"DB error: {e}", "passed": 0, "total": 0})

    total = len(tests)
    if total == 0:
        return jsonify({"ok": False, "verdict": "No test cases found.", "passed": 0, "total": 0})

    passed = 0
    errors = []
    for i, (inp, exp) in enumerate(tests):
        result   = run_code(language, code, inp)
        actual   = normalize(result["output"])
        expected = normalize(exp)
        if actual == expected:
            passed += 1
        elif len(errors) < 3:
            errors.append({
                "test":     i + 1,
                "input":    (inp or "")[:150],
                "expected": expected[:150],
                "got":      actual[:150],
            })

    return jsonify({
        "ok":      passed == total,
        "verdict": "Accepted" if passed == total else "Wrong Answer",
        "passed":  passed,
        "total":   total,
        "errors":  errors,
    })


# ── Dashboard ─────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            "SELECT id, title, description, sample_input, sample_output "
            "FROM daily_questions WHERE qdate = CURRENT_DATE"
        )
        row = cur.fetchone()

        if not row:
            q = generate_question()
            if not q:
                cur.close(); conn.close()
                flash("Could not generate today's question. Check GEMINI_API_KEY in Render env variables, then visit /debug-gemini to diagnose.", "error")
                return redirect(url_for("login"))

            cur.execute(
                "INSERT INTO daily_questions(qdate, title, description, sample_input, sample_output) "
                "VALUES(CURRENT_DATE, %s, %s, %s, %s) RETURNING id",
                (q["title"], q["description"], q["sample_input"], q["sample_output"])
            )
            qid = cur.fetchone()[0]
            for t in q.get("hidden_tests", []):
                cur.execute(
                    "INSERT INTO testcases(question_id, input, expected_output) VALUES(%s, %s, %s)",
                    (qid, t["input"], t["output"])
                )
            conn.commit()
            cur.execute(
                "SELECT id, title, description, sample_input, sample_output "
                "FROM daily_questions WHERE qdate = CURRENT_DATE"
            )
            row = cur.fetchone()

        cur.close(); conn.close()

    except Exception as e:
        flash(f"Database error: {e}", "error")
        return render_template(
            "dashboard.html",
            username="Coder", question_title="Error",
            question_desc=str(e), sample_input="", sample_output="",
            judge0_configured=bool(JUDGE0_API_KEY)
        )

    return render_template(
        "dashboard.html",
        username          = session.get("username", "Coder"),
        question_title    = row[1],
        question_desc     = row[2],
        sample_input      = row[3],
        sample_output     = row[4],
        judge0_configured = bool(JUDGE0_API_KEY),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)