from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "instance"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE = DATA_DIR / "exam_portal.db"
ALLOWED_EXTENSIONS = {"txt", "pdf", "docx"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(24)),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    APP_TIMEZONE=os.environ.get("APP_TIMEZONE", "Asia/Kolkata"),
    MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
    MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
    MAIL_FROM=os.environ.get("MAIL_FROM", ""),
    MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower() == "true",
    TEACHER_INVITE_CODE=os.environ.get("TEACHER_INVITE_CODE", ""),
)
logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def now() -> str:
    return datetime.now(ZoneInfo(app.config["APP_TIMEZONE"])).strftime("%Y-%m-%d %H:%M")


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180_000).hex()
        return secrets.compare_digest(candidate, digest)
    except ValueError:
        return False


def current_user():
    workspace = request.args.get("workspace")
    workspaces = session.get("workspaces", {})
    user_id = workspaces.get(workspace) if workspace else session.get("user_id")
    return db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None


def workspace_url(endpoint: str, **values) -> str:
    """Keep the signed-in workspace ID in links and form actions for this tab."""
    workspace = request.args.get("workspace")
    if workspace:
        values["workspace"] = workspace
    return url_for(endpoint, **values)


def require_role(role: str):
    user = current_user()
    if not user:
        flash("Please sign in to continue.", "warning")
        return None
    if user["role"] != role:
        flash(f"Please sign in with a {role} account to open that workspace.", "warning")
        return None
    return user


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            subject TEXT NOT NULL, scheduled_at TEXT NOT NULL,
            duration INTEGER NOT NULL, questions_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled', created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0, class_id INTEGER,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            join_code TEXT NOT NULL UNIQUE, teacher_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, FOREIGN KEY(teacher_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS enrollments (
            class_id INTEGER NOT NULL, student_id INTEGER NOT NULL, enrolled_at TEXT NOT NULL,
            PRIMARY KEY(class_id, student_id), FOREIGN KEY(class_id) REFERENCES classes(id),
            FOREIGN KEY(student_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
            exam_id INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            UNIQUE(student_id, exam_id), FOREIGN KEY(student_id) REFERENCES users(id),
            FOREIGN KEY(exam_id) REFERENCES exams(id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL, answers_json TEXT NOT NULL,
            score REAL NOT NULL, submitted_at TEXT NOT NULL,
            UNIQUE(exam_id, student_id), FOREIGN KEY(exam_id) REFERENCES exams(id),
            FOREIGN KEY(student_id) REFERENCES users(id)
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(exams)")}
    if "reminder_sent_at" not in columns:
        conn.execute("ALTER TABLE exams ADD COLUMN reminder_sent_at TEXT")
    if "class_id" not in columns:
        conn.execute("ALTER TABLE exams ADD COLUMN class_id INTEGER")
    conn.commit()
    conn.close()


def exam_start_label(scheduled_at: str) -> str:
    exam_time = datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(app.config["APP_TIMEZONE"]))
    return exam_time.strftime("%A, %d %B %Y at %I:%M %p %Z")


def send_email(recipient: str, subject: str, body: str) -> bool:
    """Send through the SMTP server configured in the deployment environment."""
    required = (app.config["MAIL_SERVER"], app.config["MAIL_FROM"])
    if not all(required):
        logger.warning("Email was not sent: MAIL_SERVER and MAIL_FROM are not configured.")
        return False


def email_is_configured() -> bool:
    return bool(app.config["MAIL_SERVER"] and app.config["MAIL_FROM"] and app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = recipient
    message.set_content(body)
    try:
        with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"], timeout=20) as smtp:
            if app.config["MAIL_USE_TLS"]:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception("Could not send email to %s: %s", recipient, exc)
        return False


def student_recipients(conn: sqlite3.Connection, class_id: int) -> list[str]:
    return [row[0] for row in conn.execute("SELECT u.email FROM users u JOIN enrollments e ON e.student_id=u.id WHERE e.class_id=?", (class_id,)).fetchall()]


def notify_exam_scheduled(exam_id: int) -> tuple[int, int]:
    conn = sqlite3.connect(DATABASE)
    exam = conn.execute("SELECT title,subject,scheduled_at,duration,class_id FROM exams WHERE id=?", (exam_id,)).fetchone()
    recipients = student_recipients(conn, exam[4]) if exam else []
    conn.close()
    if not exam:
        return 0, 0
    subject = f"New assessment scheduled: {exam[0]}"
    body = (f"A new assessment has been scheduled.\n\nTitle: {exam[0]}\nSubject: {exam[1]}\n"
            f"Starts: {exam_start_label(exam[2])}\nDuration: {exam[3]} minutes\n\n"
            "Sign in to ExamFlow to view and take the assessment.")
    sent = sum(send_email(email, subject, body) for email in recipients)
    return sent, len(recipients)


def create_exam_notifications(exam_id: int) -> int:
    """Persistent in-app notifications for students enrolled in the assigned class."""
    conn = sqlite3.connect(DATABASE)
    exam = conn.execute("SELECT title,scheduled_at,class_id FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam or not exam[2]:
        conn.close()
        return 0
    students = conn.execute("SELECT student_id FROM enrollments WHERE class_id=?", (exam[2],)).fetchall()
    message = f"New assessment: {exam[0]} — {exam_start_label(exam[1])}."
    for student in students:
        conn.execute("INSERT OR IGNORE INTO notifications(student_id,exam_id,message,created_at) VALUES(?,?,?,?)", (student[0], exam_id, message, now()))
    conn.commit()
    conn.close()
    return len(students)


def send_due_reminders():
    """Runs every minute and sends each scheduled exam's one-hour reminder once."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    exams = conn.execute("SELECT * FROM exams WHERE reminder_sent_at IS NULL").fetchall()
    current_time = datetime.now(ZoneInfo(app.config["APP_TIMEZONE"]))
    for exam in exams:
        if not exam["class_id"]:
            continue
        recipients = student_recipients(conn, exam["class_id"])
        try:
            starts_at = datetime.strptime(exam["scheduled_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(app.config["APP_TIMEZONE"]))
        except ValueError:
            logger.warning("Exam %s has an invalid scheduled time.", exam["id"])
            continue
        remaining = starts_at - current_time
        if not timedelta(0) < remaining <= timedelta(hours=1):
            continue
        subject = f"Reminder: {exam['title']} starts in less than one hour"
        body = (f"Your assessment starts soon.\n\nTitle: {exam['title']}\nSubject: {exam['subject']}\n"
                f"Starts: {exam_start_label(exam['scheduled_at'])}\nDuration: {exam['duration']} minutes\n\n"
                "Please sign in to ExamFlow and be ready before the start time.")
        sent = sum(send_email(email, subject, body) for email in recipients)
        if recipients and sent == len(recipients):
            conn.execute("UPDATE exams SET reminder_sent_at=? WHERE id=?", (now(), exam["id"]))
            conn.commit()
    conn.close()


def start_reminder_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(send_due_reminders, "interval", minutes=1, id="exam-reminders", replace_existing=True)
    scheduler.start()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            return " ".join(page.extract_text() or "" for page in PdfReader(file_path).pages)
        except ImportError:
            raise ValueError("PDF support needs pypdf. Install the project requirements first.")
    if suffix == ".docx":
        try:
            from docx import Document
            return " ".join(p.text for p in Document(file_path).paragraphs)
        except ImportError:
            raise ValueError("DOCX support needs python-docx. Install the project requirements first.")
    raise ValueError("Unsupported file type.")


def clean_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip().split()) >= 7]


STOP_WORDS = {"about", "after", "again", "against", "also", "among", "because", "being", "between", "could", "does", "each", "from", "have", "into", "more", "most", "other", "over", "that", "their", "there", "these", "they", "this", "through", "under", "used", "using", "when", "where", "which", "while", "with", "would"}


def key_terms(text: str) -> list[str]:
    """Return material-specific terms for answers and MCQ distractors."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for word in words:
        normal = word.casefold()
        if len(word) >= 4 and normal not in STOP_WORDS:
            counts[normal] = counts.get(normal, 0) + 1
            display.setdefault(normal, word)
    return [display[word] for word, _ in sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))]


def sentence_keyword(sentence: str, terms: list[str]) -> str:
    sentence_words = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z'-]+", sentence)}
    for term in terms:
        if term.casefold() in sentence_words:
            return term
    return max(re.findall(r"[A-Za-z][A-Za-z'-]+", sentence), key=len, default="concept")


def question_from_sentence(sentence: str, index: int, kind: str, terms: list[str]) -> dict:
    answer = sentence_keyword(sentence, terms)
    if kind == "MCQ":
        distractors = [term for term in terms if term.casefold() != answer.casefold()]
        fallback = ["process", "system", "method", "principle", "framework", "analysis"]
        distractors.extend(word for word in fallback if word.casefold() != answer.casefold() and word not in distractors)
        options = [answer] + distractors[:3]
        # A stable shuffle based on the question avoids storing display-only randomness.
        options = sorted(options, key=lambda item: hashlib.sha1(f"{index}{item}".encode()).hexdigest())
        prompt = re.sub(rf"\b{re.escape(answer)}\b", "_____", sentence, count=1, flags=re.I)
        return {"type": "MCQ", "prompt": f"Complete the statement from the study material: {prompt}", "options": options, "answer": answer}
    if kind == "Fill in the Blank":
        prompt = re.sub(rf"\b{re.escape(answer)}\b", "_____", sentence, count=1, flags=re.I)
        return {"type": kind, "prompt": prompt, "options": [], "answer": answer}
    if kind == "True / False":
        return {"type": kind, "prompt": sentence, "options": ["True", "False"], "answer": "True"}
    return {"type": "Short Answer", "prompt": f"Based on the study material, explain this idea in your own words: {sentence}", "options": [], "answer": ""}


def build_questions(text: str, count: int, question_type: str) -> list[dict]:
    sentences = clean_sentences(text)
    if not sentences:
        raise ValueError("The uploaded material needs a few complete, meaningful sentences.")
    types = ["MCQ", "Fill in the Blank", "True / False", "Short Answer"]
    chosen = (sentences * ((count // len(sentences)) + 1))[:count]
    terms = key_terms(text)
    return [question_from_sentence(sentence, i, types[i % len(types)] if question_type == "Mixed" else question_type, terms)
            for i, sentence in enumerate(chosen)]


@app.context_processor
def inject_globals():
    return {"user": current_user(), "year": datetime.now().year, "workspace_url": workspace_url, "email_configured": email_is_configured()}


@app.template_filter("fromjson")
def fromjson_filter(value: str):
    return json.loads(value)


@app.route("/")
def home():
    # The shared entry link always opens the role picker.  A dashboard is tied
    # to its workspace URL, so another tab can sign in with the other role.
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        user = db().execute("SELECT * FROM users WHERE email=? AND role=?", (email, role)).fetchone()
        if user and verify_password(password, user["password_hash"]):
            workspace = secrets.token_urlsafe(12)
            workspaces = dict(session.get("workspaces", {}))
            workspaces[workspace] = user["id"]
            session["workspaces"] = workspaces
            session["user_id"] = user["id"]  # fallback for older direct links
            destination = request.form.get("next", "")
            if destination and urlparse(destination).netloc == "" and destination.startswith("/"):
                return redirect(f"{destination}{'&' if '?' in destination else '?'}workspace={workspace}")
            return redirect(url_for("teacher_dashboard" if role == "teacher" else "student_dashboard", workspace=workspace))
        flash("Email, password, or selected role is incorrect.", "error")
    requested_role = request.args.get("role", "student")
    if requested_role not in {"teacher", "student"}:
        requested_role = "student"
    return render_template("login.html", selected_role=requested_role, next_url=request.args.get("next", ""))


@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "student")
    teacher_code = request.form.get("teacher_code", "")
    valid_email = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
    if len(name) < 2 or not valid_email or len(password) < 8 or role not in {"teacher", "student"}:
        flash("Use your full name, a valid email address, a role, and an 8-character password.", "error")
        return redirect(url_for("login"))
    invite_code = app.config["TEACHER_INVITE_CODE"]
    # A portal administrator can set TEACHER_INVITE_CODE in production to
    # restrict teacher registration. Leaving it unset keeps initial setup
    # straightforward for a new deployment with multiple teachers.
    if role == "teacher" and invite_code and not secrets.compare_digest(teacher_code, invite_code):
        flash("A valid teacher invite code is required to create a teacher account.", "error")
        return redirect(url_for("login"))
    try:
        db().execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                     (name, email, password_hash(password), role, now()))
        db().commit()
        flash("Account created. You can sign in now.", "success")
    except sqlite3.IntegrityError:
        flash("That email is already registered.", "error")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    workspace = request.args.get("workspace")
    if workspace:
        workspaces = dict(session.get("workspaces", {}))
        workspaces.pop(workspace, None)
        session["workspaces"] = workspaces
    else:
        session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/teacher")
def teacher_dashboard():
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    conn = db()
    exams = conn.execute("SELECT e.*, c.name AS class_name, COUNT(s.id) AS submissions FROM exams e JOIN classes c ON c.id=e.class_id LEFT JOIN submissions s ON s.exam_id=e.id WHERE e.created_by=? GROUP BY e.id ORDER BY e.scheduled_at DESC", (teacher["id"],)).fetchall()
    classes = conn.execute("SELECT c.*, COUNT(e.student_id) AS student_count FROM classes c LEFT JOIN enrollments e ON e.class_id=c.id WHERE c.teacher_id=? GROUP BY c.id ORDER BY c.created_at DESC", (teacher["id"],)).fetchall()
    students = sum(row["student_count"] for row in classes)
    return render_template("teacher_dashboard.html", exams=exams, classes=classes, students=students, draft=session.get("draft_questions", []))


@app.route("/teacher/classes", methods=["POST"])
def create_class():
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    name = request.form.get("name", "").strip()
    if len(name) < 2:
        flash("Enter a class name.", "error")
        return redirect(workspace_url("teacher_dashboard"))
    code = ""
    while not code:
        candidate = secrets.token_hex(3).upper()
        exists = db().execute("SELECT 1 FROM classes WHERE join_code=?", (candidate,)).fetchone()
        if not exists: code = candidate
    db().execute("INSERT INTO classes(name,join_code,teacher_id,created_at) VALUES(?,?,?,?)", (name, code, teacher["id"], now()))
    db().commit()
    flash(f"Class created. Share join code {code} with its students.", "success")
    return redirect(workspace_url("teacher_dashboard") + "#classes")


@app.route("/teacher/test-email", methods=["POST"])
def test_email():
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    if send_email(teacher["email"], "ExamFlow email test", "This confirms that ExamFlow can send notification emails through your configured SMTP provider."):
        flash(f"Test email sent to {teacher['email']}. Check inbox and spam.", "success")
    else:
        flash("Test email could not be sent. Configure MAIL_SERVER, MAIL_USERNAME, MAIL_PASSWORD, and MAIL_FROM, then restart the app.", "error")
    return redirect(workspace_url("teacher_dashboard"))


@app.route("/teacher/generate", methods=["POST"])
def generate_questions():
    if not require_role("teacher"): return redirect(url_for("login", role="teacher", next=request.path))
    file = request.files.get("material")
    count = max(1, min(20, int(request.form.get("count", 5))))
    qtype = request.form.get("question_type", "Mixed")
    if not file or not file.filename or not allowed_file(file.filename):
        flash("Upload a TXT, PDF, or DOCX study file.", "error"); return redirect(workspace_url("teacher_dashboard"))
    path = UPLOAD_DIR / f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
    file.save(path)
    try:
        session["draft_questions"] = build_questions(extract_text(path), count, qtype)
        session["draft_subject"] = Path(file.filename).stem.replace("_", " ").title()
        flash(f"{count} questions are ready to review and schedule.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    finally:
        path.unlink(missing_ok=True)
    return redirect(workspace_url("teacher_dashboard") + "#generator")


@app.route("/teacher/schedule", methods=["POST"])
def schedule_exam():
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    questions = session.get("draft_questions", [])
    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    try: class_id = int(request.form.get("class_id", "0"))
    except ValueError: class_id = 0
    scheduled = request.form.get("scheduled_at", "").strip()
    try: duration = int(request.form.get("duration", 30))
    except ValueError: duration = 0
    assigned_class = db().execute("SELECT id FROM classes WHERE id=? AND teacher_id=?", (class_id, teacher["id"])).fetchone()
    if not questions or not title or not subject or not scheduled or not assigned_class or not 5 <= duration <= 240:
        flash("Choose a class and add a title, subject, date, and valid duration.", "error"); return redirect(workspace_url("teacher_dashboard"))
    db().execute("INSERT INTO exams(title,subject,scheduled_at,duration,questions_json,created_by,created_at,class_id) VALUES(?,?,?,?,?,?,?,?)",
                 (title, subject, scheduled, duration, json.dumps(questions), teacher["id"], now(), class_id))
    exam_id = db().execute("SELECT last_insert_rowid()").fetchone()[0]
    db().commit(); session.pop("draft_questions", None)
    notified = create_exam_notifications(exam_id)
    sent, total = notify_exam_scheduled(exam_id)
    if total and sent == total:
        flash(f"Examination scheduled. {notified} student(s) received an in-app notification and {sent} email(s) were sent.", "success")
    elif total:
        flash(f"Examination scheduled. {notified} student(s) received an in-app notification. Email delivery needs SMTP configuration ({sent}/{total} sent).", "warning")
    else:
        flash("Examination scheduled. No students are enrolled in this class to notify yet.", "success")
    return redirect(workspace_url("teacher_dashboard"))


@app.route("/teacher/exam/<int:exam_id>", methods=["GET", "POST"])
def teacher_exam(exam_id):
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    conn = db(); exam = conn.execute("SELECT * FROM exams WHERE id=? AND created_by=?", (exam_id, teacher["id"])).fetchone()
    if not exam: abort(404)
    if request.method == "POST":
        conn.execute("UPDATE exams SET published=1 WHERE id=?", (exam_id,)); conn.commit()
        for submission in conn.execute("SELECT u.email FROM submissions s JOIN users u ON u.id=s.student_id WHERE s.exam_id=?", (exam_id,)):
            send_email(submission["email"], f"Results published: {exam['title']}", "Your assessment result has been published. Sign in to ExamFlow to view your score.")
        flash("Results are now published for students.", "success")
        return redirect(workspace_url("teacher_exam", exam_id=exam_id))
    submissions = conn.execute("SELECT s.*,u.name,u.email FROM submissions s JOIN users u ON u.id=s.student_id WHERE s.exam_id=? ORDER BY s.submitted_at DESC", (exam_id,)).fetchall()
    return render_template("exam_review.html", exam=exam, questions=json.loads(exam["questions_json"]), submissions=submissions)


@app.route("/teacher/exam/<int:exam_id>/grade/<int:submission_id>", methods=["POST"])
def grade_submission(exam_id, submission_id):
    teacher = require_role("teacher")
    if not teacher: return redirect(url_for("login", role="teacher", next=request.path))
    exam = db().execute("SELECT * FROM exams WHERE id=? AND created_by=?", (exam_id, teacher["id"])).fetchone()
    submission = db().execute("SELECT id FROM submissions WHERE id=? AND exam_id=?", (submission_id, exam_id)).fetchone()
    if not exam or not submission: abort(404)
    try:
        score = float(request.form.get("score", ""))
    except ValueError:
        score = -1
    maximum = len(json.loads(exam["questions_json"]))
    if score < 0 or score > maximum:
        flash(f"Enter a score from 0 to {maximum}.", "error")
    else:
        db().execute("UPDATE submissions SET score=? WHERE id=?", (score, submission_id)); db().commit()
        flash("Score saved. Publish results when grading is complete.", "success")
    return redirect(workspace_url("teacher_exam", exam_id=exam_id))


@app.route("/student")
def student_dashboard():
    student = require_role("student")
    if not student: return redirect(url_for("login", role="student", next=request.path))
    conn = db()
    exams = conn.execute("SELECT e.*,c.name AS class_name,s.id AS submission_id,s.score FROM exams e JOIN enrollments en ON en.class_id=e.class_id JOIN classes c ON c.id=e.class_id LEFT JOIN submissions s ON s.exam_id=e.id AND s.student_id=? WHERE en.student_id=? ORDER BY e.scheduled_at ASC", (student["id"], student["id"])).fetchall()
    classes = conn.execute("SELECT c.*,u.name AS teacher_name FROM classes c JOIN enrollments e ON e.class_id=c.id JOIN users u ON u.id=c.teacher_id WHERE e.student_id=? ORDER BY c.name", (student["id"],)).fetchall()
    notifications = conn.execute("SELECT n.*,e.title FROM notifications n JOIN exams e ON e.id=n.exam_id WHERE n.student_id=? ORDER BY n.created_at DESC LIMIT 8", (student["id"],)).fetchall()
    return render_template("student_dashboard.html", exams=exams, classes=classes, notifications=notifications)


@app.route("/student/notifications/read", methods=["POST"])
def mark_notifications_read():
    student = require_role("student")
    if not student: return redirect(url_for("login", role="student", next=request.path))
    db().execute("UPDATE notifications SET is_read=1 WHERE student_id=?", (student["id"],)); db().commit()
    return redirect(workspace_url("student_dashboard") + "#notifications")


@app.route("/student/join-class", methods=["POST"])
def join_class():
    student = require_role("student")
    if not student: return redirect(url_for("login", role="student", next=request.path))
    code = request.form.get("join_code", "").strip().upper()
    classroom = db().execute("SELECT id,name FROM classes WHERE join_code=?", (code,)).fetchone()
    if not classroom:
        flash("That class code was not found. Ask your teacher to confirm it.", "error")
    else:
        db().execute("INSERT OR IGNORE INTO enrollments(class_id,student_id,enrolled_at) VALUES(?,?,?)", (classroom["id"], student["id"], now()))
        db().commit()
        flash(f"You joined {classroom['name']}. Its assessments are now visible.", "success")
    return redirect(workspace_url("student_dashboard"))


@app.route("/student/exam/<int:exam_id>", methods=["GET", "POST"])
def take_exam(exam_id):
    student = require_role("student")
    if not student: return redirect(url_for("login", role="student", next=request.path))
    conn = db(); exam = conn.execute("SELECT e.* FROM exams e JOIN enrollments en ON en.class_id=e.class_id WHERE e.id=? AND en.student_id=?", (exam_id, student["id"])).fetchone()
    if not exam: abort(404)
    existing = conn.execute("SELECT id FROM submissions WHERE exam_id=? AND student_id=?", (exam_id, student["id"])).fetchone()
    if existing:
        flash("You have already submitted this examination.", "warning"); return redirect(workspace_url("student_dashboard"))
    questions = json.loads(exam["questions_json"])
    if request.method == "POST":
        answers = {}; score = 0
        for i, question in enumerate(questions):
            answer = request.form.get(f"answer_{i}", "").strip(); answers[str(i)] = answer
            if question["type"] != "Short Answer" and answer.casefold() == question["answer"].casefold(): score += 1
        conn.execute("INSERT INTO submissions(exam_id,student_id,answers_json,score,submitted_at) VALUES(?,?,?,?,?)", (exam_id, student["id"], json.dumps(answers), score, now()))
        conn.commit(); flash("Your answers have been submitted.", "success")
        return redirect(workspace_url("student_dashboard"))
    return render_template("take_exam.html", exam=exam, questions=questions)


@app.route("/download/template")
def download_template():
    path = DATA_DIR / "study_material_template.txt"
    path.write_text("Artificial intelligence is the simulation of human intelligence by machines. Machine learning is a branch of artificial intelligence that learns patterns from data. Natural language processing enables systems to understand and generate human language. Ethical AI focuses on fairness, transparency, and accountability.", encoding="utf-8")
    return send_file(path, as_attachment=True, download_name="study_material_template.txt")


init_db()
if os.environ.get("START_SCHEDULER", "true").lower() == "true":
    start_reminder_scheduler()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true", host="127.0.0.1", port=5000)
