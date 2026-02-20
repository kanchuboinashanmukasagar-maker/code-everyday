import os,json,psycopg2,bcrypt,requests
from datetime import date
from flask import Flask,render_template,request,redirect,url_for,flash,session
import google.generativeai as genai

app=Flask(__name__)
app.secret_key=os.getenv("SECRET_KEY","dev-secret")
DATABASE_URL=os.getenv("DATABASE_URL")
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model=genai.GenerativeModel("gemini-1.5-flash")

def get_conn():
    return psycopg2.connect(DATABASE_URL,sslmode="require")

@app.route("/",methods=["GET","POST"])
def login():
    if request.method=="POST":
        username=request.form["username"]
        password_raw=request.form["password"]
        if not username or not password_raw:
            flash("All fields required","error")
            return render_template("login.html")
        password=password_raw.encode("utf-8")
        conn=get_conn();cur=conn.cursor()
        cur.execute("SELECT id,password_hash FROM users WHERE username=%s",(username,))
        row=cur.fetchone()
        cur.close();conn.close()
        if row and bcrypt.checkpw(password,row[1].encode("utf-8")):
            session["user_id"]=row[0]
            return redirect(url_for("dashboard"))
        flash("Invalid credentials","error")
    return render_template("login.html")

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method=="POST":
        username=request.form["username"]
        password_raw=request.form["password"]
        if not username or not password_raw:
            flash("All fields required","error")
            return render_template("register.html")
        password=bcrypt.hashpw(password_raw.encode("utf-8"),bcrypt.gensalt()).decode("utf-8")
        conn=get_conn();cur=conn.cursor()
        try:
            cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s)",(username,password))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            flash("Username already exists","error")
            cur.close();conn.close()
            return render_template("register.html")
        cur.close();conn.close()
        return redirect(url_for("login"))
    return render_template("register.html")

def generate_question():
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY not set")
    prompt=("Generate ONE beginner DSA coding question in STRICT JSON.\n"
            "Return fields:\n"
            "title, description, sample_input, sample_output, hidden_tests\n"
            "hidden_tests must contain at least 10 test cases.\n"
            "No markdown. Only JSON.")
    try:
        response=model.generate_content(prompt)
        text=response.text.strip()
        return json.loads(text)
    except Exception as e:
        raise Exception(f"Gemini parsing failed: {e}")

def run_code(language,code,stdin):
    payload={"language":language,"version":"*","files":[{"content":code}],"stdin":stdin}
    r=requests.post("https://emkc.org/api/v2/piston/execute",json=payload,timeout=20)
    result=r.json()
    run=result.get("run")
    if not run:
        return ""
    return run.get("output","")

@app.route("/dashboard",methods=["GET","POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn=get_conn();cur=conn.cursor()
    cur.execute("SELECT id,title,description,sample_input,sample_output,hidden_tests FROM daily_questions WHERE qdate=%s",(date.today(),))
    row=cur.fetchone()
    if not row:
        q=generate_question()
        cur.execute("INSERT INTO daily_questions(qdate,title,description,sample_input,sample_output,hidden_tests) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",(date.today(),q["title"],q["description"],q["sample_input"],q["sample_output"],json.dumps(q["hidden_tests"])))
        conn.commit()
        qid=cur.fetchone()[0]
        cur.execute("SELECT id,title,description,sample_input,sample_output,hidden_tests FROM daily_questions WHERE id=%s",(qid,))
        row=cur.fetchone()
    output=None;verdict=None;passed=0;total=0
    if request.method=="POST":
        code=request.form["code-input"]
        language=request.form["language"]
        hidden_tests=row[5]
        tests=hidden_tests if isinstance(hidden_tests,list) else json.loads(hidden_tests)
        total=len(tests);passed=0
        for test in tests:
            try:
                inp=str(test.get("input","")).replace(",", " ")
                out=test.get("output","")
                result=run_code(language,code,inp)
                if result.strip()==out.strip():
                    passed+=1
                else:
                    verdict="Wrong Answer";break
            except Exception:
                verdict="Runtime Error";break
        if passed==total:
            verdict="Accepted"
        cur.execute("INSERT INTO submissions(user_id,question_id,language,code,status,passed,total) VALUES(%s,%s,%s,%s,%s,%s,%s)",(session["user_id"],row[0],language,code,verdict,passed,total))
        conn.commit()
        output=f"{passed}/{total} test cases passed"
    cur.close();conn.close()
    return render_template("dashboard.html",question_title=row[1],question_desc=row[2],sample_input=row[3],sample_output=row[4],output=output,verdict=verdict,passed=passed,total=total)

@app.route("/logout")
def logout():
    session.pop("user_id",None)
    return redirect(url_for("login"))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))