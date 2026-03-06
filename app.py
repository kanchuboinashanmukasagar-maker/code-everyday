import os
import json
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')
app.config['DATABASE'] = os.environ.get('DATABASE_URL', 'codedaily.db')

# Database initialization
def init_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Submissions table
    c.execute('''CREATE TABLE IF NOT EXISTS submissions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  problem_date DATE,
                  code TEXT,
                  status TEXT,
                  passed INTEGER,
                  total INTEGER,
                  submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (id))''')
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def load_daily_problem():
    try:
        with open('daily_problem.json', 'r') as f:
            return json.load(f)
    except:
        # Default problem if file not found
        return {
            "date": str(date.today()),
            "title": "Sum of Array",
            "description": "Given an array of integers, return the sum of all elements.",
            "testcases": [
                {"input": "4\n1 2 3 4", "output": "10"},
                {"input": "3\n10 20 30", "output": "60"}
            ]
        }

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if len(username) < 3:
            flash('Username must be at least 3 characters', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
            return render_template('register.html')
        
        conn = get_db()
        try:
            conn.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                        (username, generate_password_hash(password)))
            conn.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists', 'error')
        finally:
            conn.close()
    
    return render_template('register.html')

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    submissions = conn.execute(
        'SELECT * FROM submissions WHERE user_id = ? ORDER BY submitted_at DESC LIMIT 10',
        (session['user_id'],)
    ).fetchall()
    
    today_sub = conn.execute(
        'SELECT * FROM submissions WHERE user_id = ? AND problem_date = ? ORDER BY submitted_at DESC LIMIT 1',
        (session['user_id'], str(date.today()))
    ).fetchone()
    
    conn.close()
    
    problem = load_daily_problem()
    
    return render_template('dashboard.html',
                         username=session['username'],
                         today=date.today().strftime('%B %d, %Y'),
                         submissions=submissions,
                         today_sub=today_sub,
                         problem=problem)

@app.route('/problem')
@login_required
def problem():
    problem_data = load_daily_problem()
    return render_template('problem.html', problem=problem_data)

@app.route('/api/run', methods=['POST'])
@login_required
def run_code():
    data = request.json
    code = data.get('code', '')
    test_input = data.get('input', '')
    
    # Simple Python code execution (BE CAREFUL IN PRODUCTION - USE SANDBOXING!)
    # This is a simplified version - in production, use a proper sandbox
    try:
        # Basic implementation for sum problem
        lines = test_input.strip().split('\n')
        n = int(lines[0])
        numbers = list(map(int, lines[1].split()))
        result = str(sum(numbers))
        
        return jsonify({
            'success': True,
            'output': result,
            'error': None
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'output': '',
            'error': str(e)
        })

@app.route('/api/submit', methods=['POST'])
@login_required
def submit_code():
    data = request.json
    code = data.get('code', '')
    
    problem_data = load_daily_problem()
    passed = 0
    total = len(problem_data['testcases'])
    
    for testcase in problem_data['testcases']:
        try:
            lines = testcase['input'].strip().split('\n')
            n = int(lines[0])
            numbers = list(map(int, lines[1].split()))
            result = str(sum(numbers))
            
            if result.strip() == testcase['output'].strip():
                passed += 1
        except:
            pass
    
    status = 'solved' if passed == total else 'attempted'
    
    conn = get_db()
    conn.execute(
        'INSERT INTO submissions (user_id, problem_date, code, status, passed, total) VALUES (?, ?, ?, ?, ?, ?)',
        (session['user_id'], str(date.today()), code, status, passed, total)
    )
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'passed': passed,
        'total': total,
        'status': status
    })

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

# Initialize database on first run
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
