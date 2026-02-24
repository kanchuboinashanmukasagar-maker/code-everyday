import requests
import os, json, psycopg2, bcrypt, re
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
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
    if "localhost" in url or "127.0.0.1" in url or not url:
        return psycopg2.connect(url, connect_timeout=10)
    return psycopg2.connect(url, sslmode="require", connect_timeout=10)


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
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id,password_hash FROM users WHERE username=%s", (u,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and bcrypt.checkpw(p.encode(), row[1].encode()):
            session["user_id"] = row[0]
            session["username"] = u
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if not u or not p:
            flash("All fields required", "error")
            return render_template("register.html")
        h = bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
        conn = get_conn(); cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s)", (u, h))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            flash("Username already exists", "error")
            cur.close(); conn.close()
            return render_template("register.html")
        cur.close(); conn.close()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def clean_json(text):
    return re.sub(r"```json|```", "", text).strip()


def generate_question():
    model = get_gemini_model()
    if not model:
        return None
    try:
        prompt = (
            f"Generate ONE beginner DSA coding question for {date.today()}.\n"
            "Return STRICT JSON only — no markdown, no backticks.\n"
            "Fields: title, description, sample_input, sample_output, "
            "hidden_tests (array of 20+ objects each with 'input' and 'output')."
        )
        r = model.generate_content(prompt, generation_config={"temperature": 0.9})
        data = json.loads(clean_json(r.text))
        for key in ("title", "description", "sample_input", "sample_output", "hidden_tests"):
            if key not in data:
                raise ValueError(f"Missing key: {key}")
        return data
    except Exception as e:
        print(f"Gemini failed: {e}")
        return None


def normalize(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.strip().split("\n")]
    return "\n".join(line for line in lines if line)


def run_code(lang, code, stdin):
    lang_map = {
        "python3": "python", "python": "python",
        "c": "c", "cpp": "c++",
        "java": "java", "javascript": "javascript",
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
        r = requests.post("https://emkc.org/api/v2/piston/execute", json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        compile_info = data.get("compile", {})
        if compile_info.get("stderr"):
            return f"Compilation Error:\n{compile_info['stderr']}"
        run = data.get("run", {})
        stderr = run.get("stderr", "").strip()
        stdout = run.get("stdout", run.get("output", "")).strip()
        if stdout and stderr:
            return f"{stdout}\n\n[stderr]:\n{stderr}"
        if stderr and not stdout:
            return f"Runtime Error:\n{stderr}"
        return stdout if stdout else "(no output)"
    except requests.exceptions.Timeout:
        return "Error: Execution timed out."
    except requests.exceptions.ConnectionError:
        return "Error: Cannot reach code execution server."
    except Exception as e:
        return f"Error: {str(e)}"


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

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
            flash("Could not generate today's question. Please try again later.", "error")
            return redirect(url_for("login"))
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

    # use sentinel string so template can distinguish "not run yet" vs "ran but empty"
    output            = ""
    show_output       = False
    verdict           = ""
    show_verdict      = False
    passed            = 0
    total             = 0
    last_code         = ""
    last_language     = "python3"
    last_custom_input = ""

    if request.method == "POST":
        language     = request.form.get("language", "python3")
        code         = request.form.get("code-input", "")
        custom_input = request.form.get("custom-input", "")
        action       = request.form.get("action")

        last_code         = code
        last_language     = language
        last_custom_input = custom_input

        if action == "run":
            output      = run_code(language, code, custom_input)
            show_output = True

        elif action == "submit":
            cur.execute(
                "SELECT input, expected_output FROM testcases WHERE question_id=%s", (row[0],)
            )
            tests = cur.fetchall()
            total = len(tests)
            if total == 0:
                verdict      = "No test cases found."
                show_verdict = True
            else:
                for inp, exp in tests:
                    if normalize(run_code(language, code, inp)) == normalize(exp):
                        passed += 1
                verdict      = "Accepted" if passed == total else "Wrong Answer"
                show_verdict = True

    cur.close()
    conn.close()

    return render_template(
        "dashboard.html",
        username          = session.get("username", "Coder"),
        question_title    = row[1],
        question_desc     = row[2],
        sample_input      = row[3],
        sample_output     = row[4],
        output            = output,
        show_output       = show_output,
        verdict           = verdict,
        show_verdict      = show_verdict,
        passed            = passed,
        total             = total,
        last_code         = last_code,
        last_language     = last_language,
        last_custom_input = last_custom_input,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)