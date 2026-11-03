from flask import Flask, render_template, request, abort, redirect, session, url_for
import csv
import re
import secrets
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
import pyotp

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

admin_password = "2626"
totp = pyotp.TOTP(pyotp.random_base32(), interval=10)

active_class = None
submitted_students = set()
current_attendance = []
attendance_lock = Lock()

folder = Path(__file__).resolve().parent
students_file = folder / "students.csv"
attendance_file = folder / "attendance.csv"

columns = [
    "subject",
    "student_id",
    "name",
    "time_utc",
    "browser_id",
]

students = {}

with students_file.open(newline="", encoding="utf-8-sig") as file:
    for row in csv.DictReader(file):
        student_id = row["student_id"].strip()
        students[student_id] = row["name"].strip()

if attendance_file.exists() and attendance_file.stat().st_size > 0:
    with attendance_file.open(newline="", encoding="utf-8-sig") as file:
        header = next(csv.reader(file), None)

        if header != columns:
            raise RuntimeError(
                "The attendance CSV has different columns. "
                "Rename it to attendance_old.csv before starting."
            )


def get_browser_id():
    if "browser_id" not in session:
        session["browser_id"] = secrets.token_hex(16)

    return session["browser_id"]


def clear_login():
    browser_id = get_browser_id()
    session.clear()
    session["browser_id"] = browser_id


def admin_required(view):
    @wraps(view)
    def check_login(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))

        return view(*args, **kwargs)

    return check_login


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)

    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def check_csrf():
    saved_token = session.get("csrf_token", "")
    form_token = request.form.get("csrf_token", "")

    if not saved_token or not secrets.compare_digest(
        saved_token.encode(), form_token.encode()
    ):
        abort(400, "Refresh the page and try again.")


def csv_safe(value):
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value

    return value


def save_attendance(student_id):
    record = {
        "subject": active_class["name"],
        "student_id": student_id,
        "name": students[student_id],
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "browser_id": get_browser_id(),
    }

    needs_header = (
        not attendance_file.exists()
        or attendance_file.stat().st_size == 0
    )

    with attendance_file.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)

        if needs_header:
            writer.writeheader()

        writer.writerow({
            key: csv_safe(value)
            for key, value in record.items()
        })

    current_attendance.append({
        **record,
        "class_name": record["subject"],
    })


def show_message(message, category):
    return render_template(
        "index.html",
        message=message,
        category=category,
    )


@app.after_request
def prevent_admin_caching(response):
    if request.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index():
    get_browser_id()
    return render_template("index.html")


@app.route("/attend", methods=["POST"])
def attend():
    student_id = request.form.get("student_id", "").strip()
    otp = request.form.get("otp", "").strip()

    with attendance_lock:
        if active_class is None:
            return show_message(
                "No class is currently open.",
                "warning",
            )

        if student_id not in students:
            return show_message(
                "Student ID not found.",
                "danger",
            )

        if not re.fullmatch(r"[0-9]{6}", otp) or not totp.verify(otp):
            return show_message(
                "Invalid or expired code.",
                "danger",
            )

        if student_id in submitted_students:
            return show_message(
                "You have already submitted attendance for this class.",
                "warning",
            )

        save_attendance(student_id)
        submitted_students.add(student_id)

    return show_message(
        f"Attendance saved for {students[student_id]}.",
        "success",
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin"))

    error = None

    if request.method == "POST":
        check_csrf()
        password = request.form.get("password", "")

        if secrets.compare_digest(
            password.encode(), admin_password.encode()
        ):
            clear_login()
            session["admin"] = True
            return redirect(url_for("admin"))

        error = "Incorrect password."

    return render_template("admin_login.html", error=error)


@app.route("/admin")
@admin_required
def admin():
    with attendance_lock:
        now = int(time.time())
        current_class = active_class
        code = totp.at(now) if current_class else None
        remaining = totp.interval - now % totp.interval

        browser_students = {}

        for record in current_attendance:
            browser_id = record["browser_id"]

            browser_students.setdefault(browser_id, set()).add(
                record["student_id"]
            )

        records = []

        for record in current_attendance:
            suspicious = len(
                browser_students[record["browser_id"]]
            ) > 1

            records.append({
                **record,
                "suspicious": suspicious,
            })

        flagged_count = sum(
            record["suspicious"] for record in records
        )

    return render_template(
        "admin.html",
        active_class=current_class,
        code=code,
        remaining=remaining,
        records=records,
        flagged_count=flagged_count,
    )


@app.route("/admin/class", methods=["POST"])
@admin_required
def start_class():
    global active_class, totp

    check_csrf()

    subject = request.form.get("class_name", "").strip()

    if not 1 <= len(subject) <= 100:
        abort(400, "Enter a subject between 1 and 100 characters.")

    with attendance_lock:
        if active_class is not None:
            abort(
                409,
                "Close the current class before starting another.",
            )

        active_class = {"name": subject}
        submitted_students.clear()
        current_attendance.clear()

        totp = pyotp.TOTP(pyotp.random_base32(), interval=15)

    return redirect(url_for("admin"))


@app.route("/admin/class/close", methods=["POST"])
@admin_required
def close_class():
    global active_class
    check_csrf()

    with attendance_lock:
        active_class = None
        submitted_students.clear()
    return redirect(url_for("admin"))


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    check_csrf()
    clear_login()

    return redirect(url_for("admin_login"))


app.run(port=5002)