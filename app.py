import requests
import os,json,psycopg2,bcrypt,re
from datetime import date
from flask import Flask,render_template,request,redirect,url_for,flash,session
import google.generativeai as genai

app=Flask(__name__)
app.secret_key=os.getenv("SECRET_KEY","dev-secret")
DATABASE_URL=os.getenv("DATABASE_URL")
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model=genai.GenerativeModel("gemini-1.5-flash")

def get_conn():
    return psycopg2.connect(DATABASE_URL,sslmode="require")

@app.route("/",methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=request.form.get("username","").strip()
        p=request.form.get("password","").strip()
        if not u or not p:
            flash("All fields required","error")
            return render_template("login.html")
        conn=get_conn();cur=conn.cursor()
        cur.execute("SELECT id,password_hash FROM users WHERE username=%s",(u,))
        row=cur.fetchone()
        cur.close();conn.close()
        if row and bcrypt.checkpw(p.encode(),row[1].encode()):
            session["user_id"]=row[0]
            return redirect(url_for("dashboard"))
        flash("Invalid credentials","error")
    return render_template("login.html")

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method=="POST":
        u=request.form.get("username","").strip()
        p=request.form.get("password","").strip()
        if not u or not p:
            flash("All fields required","error")
            return render_template("register.html")
        h=bcrypt.hashpw(p.encode(),bcrypt.gensalt()).decode()
        conn=get_conn();cur=conn.cursor()
        try:
            cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s)",(u,h))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            flash("Username already exists","error")
            cur.close();conn.close()
            return render_template("register.html")
        cur.close();conn.close()
        return redirect(url_for("login"))
    return render_template("register.html")

def clean_json(text):
    text=re.sub(r"```json|```","",text)
    return text.strip()

def generate_question():
    placeholder={
        "title":"Sum of Two Numbers",
        "description":"Read two integers and print their sum.",
        "sample_input":"2 3",
        "sample_output":"5",
        "hidden_tests":[{"input":"2 3","output":"5"},{"input":"10 15","output":"25"}]
    }
    if not GEMINI_API_KEY:
        return placeholder
    try:
        prompt=(f"Generate ONE beginner Data Structures and Algorithms coding question for {date.today()}.\n"
                "Topic must be strictly DSA (arrays, strings, recursion, sorting, searching, etc).\n"
                "Return STRICT JSON only.\n"
                "Fields: title, description, sample_input, sample_output, hidden_tests.\n"
                "hidden_tests must contain at least 20 test cases.\n"
                "No markdown. Only JSON.")
        r=model.generate_content(prompt,generation_config={"temperature":0.9})
        text=clean_json(r.text)
        return json.loads(text)
    except Exception:
        return placeholder

def run_code(lang,code,stdin):
    payload={"language":lang,"version":"*","files":[{"content":code}],"stdin":stdin}
    try:
        r=requests.post("https://emkc.org/api/v2/piston/execute",json=payload,timeout=20)
        run=r.json().get("run")
        return run.get("output","") if run else ""
    except Exception:
        return ""

@app.route("/dashboard",methods=["GET","POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn=get_conn();cur=conn.cursor()
    cur.execute("SELECT id,title,description,sample_input,sample_output,hidden_tests FROM daily_questions WHERE qdate=%s",(date.today(),))
    row=cur.fetchone()
    if not row:
        q=generate_question()
        cur.execute("INSERT INTO daily_questions(qdate,title,description,sample_input,sample_output,hidden_tests) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                    (date.today(),q["title"],q["description"],q["sample_input"],q["sample_output"],json.dumps(q["hidden_tests"])))
        conn.commit()
        qid=cur.fetchone()[0]
        cur.execute("SELECT id,title,description,sample_input,sample_output,hidden_tests FROM daily_questions WHERE id=%s",(qid,))
        row=cur.fetchone()
    question_id=row[0]
    tests=row[5] if isinstance(row[5],list) else json.loads(row[5])
    output=None;verdict=None;passed=0;total=len(tests)
    if request.method=="POST":
        code=request.form.get("code-input","")
        lang=request.form.get("language","")
        action=request.form.get("action")
        if action=="run":
            output=run_code(lang,code,row[3])
        elif action=="submit":
            for t in tests:
                result=run_code(lang,code,str(t["input"]))
                if result.strip()==str(t["output"]).strip():
                    passed+=1
                else:
                    verdict="Wrong Answer"
                    break
            if passed==total:
                verdict="Accepted"
            cur.execute("INSERT INTO submissions(user_id,question_id,language,code,status,passed,total) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (session["user_id"],question_id,lang,code,verdict,passed,total))
            conn.commit()
            output=f"{passed}/{total} test cases passed"
    cur.close();conn.close()
    return render_template("dashboard.html",
                           question_title=row[1],
                           question_desc=row[2],
                           sample_input=row[3],
                           sample_output=row[4],
                           output=output,
                           verdict=verdict,
                           passed=passed,
                           total=total)

@app.route("/logout")
def logout():
    session.pop("user_id",None)
    return redirect(url_for("login"))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))