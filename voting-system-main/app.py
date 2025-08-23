import os
import sqlite3
import csv
import io
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, send_file, abort, g)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

LOCAL_TZ = ZoneInfo("Asia/Kolkata") if ZoneInfo else None

def utc_now():
    try:
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

def parse_local_to_utc(dt_str: str) -> datetime:
    # dt_str like "YYYY-MM-DDTHH:MM" from <input type="datetime-local">
    naive_local = datetime.fromisoformat(dt_str)
    if LOCAL_TZ:
        aware_local = naive_local.replace(tzinfo=LOCAL_TZ)
    else:
        # Fallback: treat input as UTC if zoneinfo not available
        aware_local = naive_local.replace(tzinfo=timezone.utc)
    return aware_local.astimezone(timezone.utc)

def parse_utc_iso(s: str) -> datetime:
    # Accept ISO strings either naive (assume UTC) or with offset
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
# ---------- Configuration ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join("/tmp", "voting.db")
DB_PATH = os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)
try:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
except Exception as _e:
    print(f"[WARN] Could not create DB dir: {DB_PATH}: {_e}")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = "replace-this-with-a-strong-secret"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4MB

# ---------- Utility: DB ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def run_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    return cur.lastrowid

# ---------- Init DB ----------
def init_db():
    # BOOTSTRAP_DB_ON_START: create all tables if DB file is missing
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    # Users: stores name, email, username, password (hashed), role (admin/voter/candidate), id_number (for voter)
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        id_number TEXT
    );
    """)
    # Candidates
    c.execute("""
    CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        photo TEXT
    );
    """)
    # Elections
    c.execute("""
    CREATE TABLE IF NOT EXISTS elections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        start_time TEXT NOT NULL,  -- ISO format
        end_time TEXT NOT NULL     -- ISO format
    );
    """)
    # Votes
    c.execute("""
    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voter_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        election_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY (voter_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id),
        FOREIGN KEY (election_id) REFERENCES elections(id)
    );
    """)
    db.commit()
    # Create a default admin if none exists
    cur = c.execute("SELECT COUNT(*) as count FROM users WHERE role='admin'")
    res = cur.fetchone()
    if res[0] == 0:
        pw = generate_password_hash("admin123")
        c.execute("INSERT INTO users (name,email,username,password,role) VALUES (?,?,?,?,?)",
                  ("Administrator", "admin@example.com", "admin", pw, "admin"))
        db.commit()
        print("Created default admin -> username: admin  password: admin123")
    db.close()

# Run DB init at startup if DB does not exist
if not os.path.exists(DB_PATH):
# init_db() will be called on first request if needed
# ---------- Helpers ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(role=None):
    def wrapper(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                return abort(403)
            return fn(*args, **kwargs)
        return decorated
    return wrapper

def get_current_election():
    now = utc_now()
    rows = query_db("SELECT * FROM elections")
    for r in rows:
        st = parse_utc_iso(r["start_time"])
        et = parse_utc_iso(r["end_time"])
        if st <= now <= et:
            return r
    return None

# ---------- Routes ----------
@app.route("/")
def index():
    if "user_id" in session:
        role = session.get("role")
        if role == "admin":
            return redirect(url_for("admin_panel"))
        elif role == "voter":
            return redirect(url_for("voter_panel"))
        elif role == "candidate":
            return redirect(url_for("candidate_panel"))
    return redirect(url_for("login"))

# ----- Signup -----
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name").strip()
        email = request.form.get("email").strip()
        username = request.form.get("username").strip()
        password = request.form.get("password")
        role = "voter"  # force voter role; no candidate/admin via signup
        id_number = request.form.get("id_number") or None
        if not id_number:
            flash("ID number is required for voter registration.", "danger")
            return redirect(url_for("signup"))

        if role != "voter":
            flash("Invalid role", "danger")
            return redirect(url_for("signup"))

        hashed = generate_password_hash(password)
        try:
            run_db("INSERT INTO users (name,email,username,password,role,id_number) VALUES (?,?,?,?,?,?)",
                   (name, email, username, hashed, role, id_number))
            flash("Account created. You may login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username or email already exists.", "danger")
            return redirect(url_for("signup"))

    return render_template("signup.html")

# ----- Login -----
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        user = query_db("SELECT * FROM users WHERE username=?", (username,), one=True)
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome, {user['name']}", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")

# ----- Logout -----
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ----- Admin Panel -----
@app.route("/admin", methods=["GET", "POST"])
@login_required(role="admin")
def admin_panel():
    db = get_db()
    candidates = query_db("SELECT * FROM candidates")
    voters = query_db("SELECT * FROM users WHERE role='voter'")
    elections = query_db("SELECT * FROM elections")
    return render_template("admin.html", candidates=candidates, voters=voters, elections=elections)

@app.route("/admin/add_candidate", methods=["POST"])
@login_required(role="admin")
def add_candidate():
    name = request.form.get("name").strip()
    category = request.form.get("category").strip()

    photo = None
    if "photo" in request.files:
        file = request.files["photo"]
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{int(datetime.utcnow().timestamp())}_{file.filename}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo = f"uploads/{filename}"

    run_db("INSERT INTO candidates (name,category,photo) VALUES (?,?,?)", (name, category, photo))
    flash("Candidate added", "success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/add_voter", methods=["POST"])
@login_required(role="admin")
def add_voter():
    name = request.form.get("name").strip()
    email = request.form.get("email").strip()
    username = request.form.get("username").strip()
    id_number = request.form.get("id_number").strip()
    password = request.form.get("password") or "voter123"
    hashed = generate_password_hash(password)
    try:
        run_db("INSERT INTO users (name,email,username,password,role,id_number) VALUES (?,?,?,?,?,?)",
               (name, email, username, hashed, "voter", id_number))
        flash("Voter added", "success")
    except sqlite3.IntegrityError:
        flash("Email or username already exists", "danger")
    return redirect(url_for("admin_panel"))

@app.route("/admin/add_election", methods=["POST"])
@login_required(role="admin")
def add_election():
    category = request.form.get("category").strip()
    start_time = request.form.get("start_time").strip()  # expect ISO or "YYYY-MM-DDTHH:MM"
    end_time = request.form.get("end_time").strip()
    # Normalize to ISO format
    # flask form input type="datetime-local" returns 'YYYY-MM-DDTHH:MM'
    try:
        # store as ISO strings
        st = parse_local_to_utc(start_time)
        et = parse_local_to_utc(end_time)
        if et <= st:
            flash("End time must be after start time", "danger")
            return redirect(url_for("admin_panel"))
        run_db("INSERT INTO elections (category,start_time,end_time) VALUES (?,?,?)",
               (category, st.isoformat(), et.isoformat()))
        flash("Election scheduled", "success")
    except Exception as e:
        flash("Invalid date/time format. Use the picker.", "danger")
    return redirect(url_for("admin_panel"))

@app.route("/admin/download_results/<int:election_id>")
@login_required(role="admin")
def download_results(election_id):
    # CSV of candidate, votes for given election
    rows = query_db("""
        SELECT c.name, COUNT(v.id) AS votes FROM candidates c
        LEFT JOIN votes v ON c.id = v.candidate_id AND v.election_id = ?
        WHERE c.category = (SELECT category FROM elections WHERE id = ?)
        GROUP BY c.id
    """, (election_id, election_id))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Candidate", "Votes"])
    for r in rows:
        writer.writerow([r["name"], r["votes"]])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True, download_name=f"results_election_{election_id}.csv")

# ----- Voter Panel -----
@app.route("/voter", methods=["GET", "POST"])
@login_required(role="voter")
def voter_panel():
    user_id = session["user_id"]
    # current active election
    election = get_current_election()
    if not election:
        return render_template("voter.html", election=None, voted=False, candidates=[])
    # get candidates in that category
    candidates = query_db("SELECT * FROM candidates WHERE category=?", (election["category"],))
    # check if voter has voted in this election
    voted = query_db("SELECT * FROM votes WHERE voter_id=? AND election_id=?", (user_id, election["id"]), one=True)
    return render_template("voter.html", election=election, voted=bool(voted), candidates=candidates)

@app.route("/voter/vote/<int:candidate_id>", methods=["POST"])
@login_required(role="voter")
def cast_vote(candidate_id):
    user_id = session["user_id"]
    election = get_current_election()
    now = utc_now()
    if not election:
        flash("No active election right now.", "danger")
        return redirect(url_for("voter_panel"))
    st = parse_utc_iso(election["start_time"])
    et = parse_utc_iso(election["end_time"])
    if not (st <= now <= et):
        flash("Voting is closed for this election.", "danger")
        return redirect(url_for("voter_panel"))
    # ensure voter hasn't already voted in this election
    existing = query_db("SELECT * FROM votes WHERE voter_id=? AND election_id=?", (user_id, election["id"]), one=True)
    if existing:
        flash("You have already voted in this election.", "warning")
        return redirect(url_for("voter_panel"))
    # insert vote
    run_db("INSERT INTO votes (voter_id,candidate_id,election_id,timestamp) VALUES (?,?,?,?)",
           (user_id, candidate_id, election["id"], now.isoformat()))
    flash("Vote recorded. Thank you!", "success")
    return redirect(url_for("voter_panel"))

# ----- Candidate Panel -----
@app.route("/candidate")
@login_required(role="candidate")
def candidate_panel():
    uid = session["user_id"]
    user = query_db("SELECT * FROM users WHERE id=?", (uid,), one=True)
    # candidate record by name match (simple approach: candidate name == username or name)
    candidate = query_db("SELECT * FROM candidates WHERE name=?", (user["name"],), one=True)
    # show live results for candidate's category (or overall)
    election = get_current_election()
    results = []
    if election:
        results = query_db("""
            SELECT c.id, c.name, COUNT(v.id) AS votes
            FROM candidates c
            LEFT JOIN votes v ON c.id = v.candidate_id AND v.election_id = ?
            WHERE c.category = ?
            GROUP BY c.id
        """, (election["id"], election["category"]))
    return render_template("candidate.html", user=user, candidate=candidate, election=election, results=results)

# ----- Results (Admin view) -----
@app.route("/results")
@login_required(role="admin")
def results():
    # allow admin to choose an election via query param ?election_id=#
    election_id = request.args.get("election_id", type=int)
    if election_id:
        election = query_db("SELECT * FROM elections WHERE id=?", (election_id,), one=True)
    else:
        election = get_current_election()
    results = []
    if election:
        results = query_db("""
            SELECT c.id, c.name, COUNT(v.id) AS votes
            FROM candidates c
            LEFT JOIN votes v ON c.id = v.candidate_id AND v.election_id = ?
            WHERE c.category = ?
            GROUP BY c.id
        """, (election["id"], election["category"]))
    elections = query_db("SELECT * FROM elections")
    return render_template("result.html", election=election, results=results, elections=elections)

# ---------- Run ----------
if __name__ == "__main__":
    # Ensure database exists on first boot
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass
    if (not os.path.exists(DB_PATH)):
        try:
# init_db() will be called on first request if needed
        except Exception as e:
            print(f"[WARN] init_db failed: {e}")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

@app.before_first_request
def _ensure_db_on_first_request():
    try:
        init_db()
    except Exception as e:
        print(f"[WARN] init_db on first request failed: {e}")
