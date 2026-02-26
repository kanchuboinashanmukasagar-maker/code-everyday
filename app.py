import os, json, re, psycopg2, bcrypt, requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
DATABASE_URL   = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

JUDGE0_URL = "https://ce.judge0.com/submissions?base64_encoded=false&wait=true"
LANG_IDS   = {"python3": 71, "c": 50, "cpp": 54, "java": 62, "javascript": 63}


def get_conn():
    url = DATABASE_URL or ""
    if "localhost" in url:
        return psycopg2.connect(url)
    return psycopg2.connect(url, sslmode="require")


def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL, password_hash TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_questions (
        id SERIAL PRIMARY KEY, qdate DATE UNIQUE NOT NULL,
        title TEXT, description TEXT, sample_input TEXT, sample_output TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS testcases (
        id SERIAL PRIMARY KEY, question_id INTEGER REFERENCES daily_questions(id) ON DELETE CASCADE,
        input TEXT, expected_output TEXT)""")
    conn.commit(); cur.close(); conn.close()


with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error (will retry): {e}")


@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"].strip()
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (u,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and bcrypt.checkpw(p.encode(), row[1].encode()):
            session["user_id"] = row[0]
            session["username"] = u
            return redirect(url_for("dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"].strip()
        if len(u) < 3 or len(p) < 6:
            flash("Username min 3 chars, password min 6 chars", "error")
            return render_template("register.html")
        h = bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
        try:
            conn = get_conn(); cur = conn.cursor()
            cur.execute("INSERT INTO users(username, password_hash) VALUES(%s,%s)", (u, h))
            conn.commit(); cur.close(); conn.close()
            flash("Account created! Please sign in.", "success")
            return redirect(url_for("login"))
        except psycopg2.errors.UniqueViolation:
            flash("Username already taken", "error")
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def ask_gemini():
    if not GEMINI_API_KEY:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model    = genai.GenerativeModel("gemini-1.5-flash")
        prompt   = """Generate ONE beginner DSA coding problem.
Output ONLY a JSON object, no markdown, no extra text.
Use this exact structure:
{"title":"...","description":"...","sample_input":"...","sample_output":"...","hidden_tests":[{"input":"...","output":"..."}]}
Include exactly 20 hidden_tests. Topic: arrays, strings, hash maps, or stacks."""
        response = model.generate_content(prompt, generation_config={"temperature": 0.9})
        raw      = re.sub(r"```json|```", "", response.text).strip()
        data     = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
        assert "title" in data and "hidden_tests" in data and len(data["hidden_tests"]) >= 5
        return data
    except Exception as e:
        print(f"Gemini error: {e}")
        return None


@app.route("/debug-gemini")
def debug_gemini():
    if not GEMINI_API_KEY:
        return "<h3 style='color:red'>GEMINI_API_KEY is not set in Render environment variables.</h3>"
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model    = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content("Say hello in one word")
        return f"<h3 style='color:green'>Gemini is working!</h3><p>Response: {response.text}</p>"
    except Exception as e:
        return f"<h3 style='color:red'>Gemini failed: {e}</h3>"


@app.route("/admin/refresh-question")
def admin_refresh():
    if request.args.get("token") != os.getenv("ADMIN_TOKEN", ""):
        return "Unauthorized", 401
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM daily_questions WHERE qdate=CURRENT_DATE")
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM daily_questions WHERE id=%s", (row[0],))
        conn.commit()
    cur.close(); conn.close()
    return redirect(url_for("dashboard"))


def run_code(lang, code, stdin):
    lang_id = LANG_IDS.get(lang)
    if not lang_id:
        return {"ok": False, "output": f"Language '{lang}' not supported."}
    payload = {"language_id": lang_id, "source_code": code, "stdin": stdin or ""}
    try:
        r = requests.post(JUDGE0_URL, json=payload, timeout=30)
        if r.status_code == 429:
            return {"ok": False, "output": "Too many requests. Please wait a moment and try again."}
        d           = r.json()
        stdout      = (d.get("stdout") or "").strip()
        stderr      = (d.get("stderr") or "").strip()
        compile_out = (d.get("compile_output") or "").strip()
        status_id   = d.get("status", {}).get("id", 0)
        if compile_out:                   return {"ok": False, "output": f"Compilation Error:\n{compile_out}"}
        if status_id == 5:                return {"ok": False, "output": "Time Limit Exceeded."}
        if stderr and not stdout:         return {"ok": False, "output": f"Runtime Error:\n{stderr}"}
        return {"ok": True, "output": stdout or "(no output)"}
    except Exception as e:
        return {"ok": False, "output": f"Error connecting to Judge0: {e}"}


def normalize(text):
    return "\n".join(l.strip() for l in (text or "").strip().splitlines() if l.strip())


@app.route("/api/run", methods=["POST"])
def api_run():
    if "user_id" not in session: return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    code = body.get("code", "").strip()
    if not code: return jsonify({"ok": False, "output": "Write some code first."})
    return jsonify(run_code(body.get("language", "python3"), code, body.get("custom_input", "")))


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if "user_id" not in session: return jsonify({"error": "Not logged in"}), 401
    body = request.get_json(force=True)
    code = body.get("code", "").strip()
    lang = body.get("language", "python3")
    if not code: return jsonify({"ok": False, "verdict": "No code submitted.", "passed": 0, "total": 0})
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM daily_questions WHERE qdate=CURRENT_DATE")
    row = cur.fetchone()
    if not row: return jsonify({"ok": False, "verdict": "No question today.", "passed": 0, "total": 0})
    cur.execute("SELECT input, expected_output FROM testcases WHERE question_id=%s", (row[0],))
    tests = cur.fetchall(); cur.close(); conn.close()
    passed = 0; errors = []
    for i, (inp, exp) in enumerate(tests):
        result = run_code(lang, code, inp)
        if normalize(result["output"]) == normalize(exp):
            passed += 1
        elif len(errors) < 3:
            errors.append({"test": i+1, "input": (inp or "")[:100],
                           "expected": normalize(exp)[:100], "got": normalize(result["output"])[:100]})
    return jsonify({"ok": passed == len(tests), "verdict": "Accepted" if passed == len(tests) else "Wrong Answer",
                    "passed": passed, "total": len(tests), "errors": errors})


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session: return redirect(url_for("login"))
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, title, description, sample_input, sample_output FROM daily_questions WHERE qdate=CURRENT_DATE")
    row = cur.fetchone()
    if not row:
        q = ask_gemini()
        if not q:
            cur.close(); conn.close()
            flash("Gemini failed to generate a question. Visit /debug-gemini to diagnose.", "error")
            return redirect(url_for("login"))
        cur.execute("INSERT INTO daily_questions(qdate,title,description,sample_input,sample_output) VALUES(CURRENT_DATE,%s,%s,%s,%s) RETURNING id",
                    (q["title"], q["description"], q["sample_input"], q["sample_output"]))
        qid = cur.fetchone()[0]
        for t in q["hidden_tests"]:
            cur.execute("INSERT INTO testcases(question_id,input,expected_output) VALUES(%s,%s,%s)", (qid, t["input"], t["output"]))
        conn.commit()
        cur.execute("SELECT id, title, description, sample_input, sample_output FROM daily_questions WHERE qdate=CURRENT_DATE")
        row = cur.fetchone()
    cur.close(); conn.close()
    return render_template("dashboard.html", username=session.get("username"),
                           question_title=row[1], question_desc=row[2],
                           sample_input=row[3], sample_output=row[4])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)