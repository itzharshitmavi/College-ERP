# College ERP

A Django-based college ERP covering the full student/teacher/administrator lifecycle: registration and approval, attendance, timetables, subjects, exams and marks, assignments, notes, documents, leave, counselling, academic progression, chat, PDF reports and a student fee ledger with Razorpay online payments. A token-authenticated REST API is included for a mobile client.

## Table of contents

1. [Features](#features)
2. [Tech stack](#tech-stack)
3. [Project structure](#project-structure)
4. [Quick start](#quick-start)
5. [Configuration](#configuration)
6. [User roles](#user-roles)
7. [Academic-year and semester rules](#academic-year-and-semester-rules)
8. [Student fee workflow](#student-fee-workflow)
9. [REST API](#rest-api)
10. [Testing](#testing)
11. [Production notes](#production-notes)
12. [Known limitations](#known-limitations)

## Features

**Administrator**
- Approve or reject student and teacher registrations (single or bulk), with optional decision emails via SendPulse
- Manage departments, courses, subjects, sessions and HOD assignments
- Academic Session & Semester Management: set the active session, promote semesters, repair cohort links, graduate students
- Fee Management: fee heads, semester invoice generation, offline receipts
- Review teacher leave requests; student document vault

**Teacher**
- Mark and edit attendance, schedule extra classes, view attendance history
- Timetable, exams, marks entry, student and class reports (PDF)
- Assignments, notes, shared documents, notices
- Review student leave and counselling requests; apply for own leave

**Student**
- Attendance, marks, timetable, subjects, teachers and exam schedule
- Assignment submission, notes, personal document vault
- Leave application, counselling support requests
- Fee payment (Razorpay) and downloadable report PDF

**Shared**: chatroom, notices, light/dark theme, password change.

## Tech stack

- Python 3.11+ (tested on 3.13), Django 5.2, Django REST Framework 3.16, Djoser
- SQLite (default; PostgreSQL recommended for production)
- Pillow (photos), ReportLab (PDF reports), python-dotenv, pysendpulse (email)
- Bootstrap 4 (SB Admin) front end, bundled under `info/static`

## Project structure

```
ERP/
├── manage.py
├── requirements.txt
├── .env.example            # copy to .env
├── CollegeERP/             # project settings, root URLs, WSGI, shared forms
├── info/                   # main app: models, views, templates, static, migrations, tests
│   ├── models.py           # User, Student, Teacher, Course, Session, Subject, Exam, Fee* ...
│   ├── views.py            # all web views (student, teacher, admin, payments)
│   ├── urls.py
│   ├── context_processors.py
│   ├── templatetags/
│   └── templates/info/     # HTML templates
├── apis/                   # REST API (serializers, views, urls)
├── media/                  # uploaded photos, documents (not for version control)
└── db.sqlite3              # bundled database (application data)
```

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your environment file
cp .env.example .env                 # Windows: Copy-Item .env.example .env

# 4. Apply migrations and verify
python manage.py migrate
python manage.py check

# 5. (Only with a new/empty database) create an administrator
python manage.py createsuperuser

# 6. Run
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Django admin is at `/admin/`.

> The archive ships with a populated `db.sqlite3`. **Back it up before running `migrate`.** Use the administrator account already in that database, or create a new one in an empty database.

## Configuration

All settings are read from `.env` (see `.env.example`).

| Variable | Purpose | Default |
|---|---|---|
| `DJANGO_DEBUG` | Debug mode | `True` |
| `DJANGO_SECRET_KEY` | Secret key (**required** when debug is off) | dev-only key |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | `localhost,127.0.0.1,0.0.0.0` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusted origins for HTTPS domains | empty |
| `DJANGO_TIME_ZONE` | Time zone | `Asia/Kolkata` |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirect to HTTPS (when debug off) | `True` |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS duration (when debug off) | `0` |
| `DEFAULT_FROM_EMAIL` | Sender address | `noreply@example.com` |
| `SENDPULSE_API_USER_ID`, `SENDPULSE_API_SECRET`, `SENDPULSE_TOKEN_STORAGE_PATH` | Email via SendPulse (optional) | empty |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_CURRENCY` | Online payments (optional) | empty / `INR` |

Generate a secret key with:

```bash
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```

Email and Razorpay are optional. Without email credentials, approvals still work. Without both Razorpay keys, online payment stays disabled.

## User roles

| Role | How it is identified | Home |
|---|---|---|
| Administrator | Django superuser | `/index/` → admin dashboard |
| Teacher | Linked `Teacher` record | `/teacher/home/` |
| Student | Linked `Student` record (username = roll number) | `/index/` → student home |

New students and teachers register at `/register_student/` and `/register_teacher/`; an administrator approves them under **Approve Requests** before they can sign in.

## Academic-year and semester rules

- `Session.is_current` is the college-wide active academic calendar. It is not a student's batch assignment.
- A student's session is derived from admission year and semester: semesters 1–2 belong to the admission-year session, 3–4 to the next, 5–6 to the following, 7–8 to the one after.
- Course duration sets the maximum semester count. A missing duration is an error; the ERP never assumes four years.
- Set the active session explicitly under **Admin → Academic Session & Semester Management** before **Promote Semester**. A student advances only if the next semester belongs to the active session, and a repeated submit cannot advance them twice in one session.
- **Repair Cohort Sessions** rebuilds cohort session links and current subjects without changing semesters.
- **Set Semester for Selected Cohort** is an explicit correction that derives the session from each student's own admission year.
- Graduation is a separate, confirmed action: the student must be in the final semester and their final cohort session must have started.

## Student fee workflow

1. Configure a fee head for the exact course, department, cohort session and semester in **Admin → Fee Management** (tuition, exam, library, lab, and so on, each separately).
2. Use **Generate Semester Invoices** with the same course, department, session and semester. One invoice is created per approved, enrolled student in the matching cohort. Items are snapshots; later fee edits do not change issued invoices.
3. Students open **Pay Fees** to see heads, totals, successful payments and the outstanding balance.
4. Online payments use Razorpay Checkout. The server creates the order, verifies the HMAC signature, and confirms status, amount and currency with Razorpay before marking the payment successful.
5. Staff can record cash, UPI, bank-transfer or cheque receipts. A reference is mandatory; duplicate references, overpayments and invoices with unresolved online orders are rejected.

**Razorpay setup**: put the keys in `.env` (use test keys first), then register the webhook `https://YOUR-DOMAIN/payments/razorpay/webhook/` for `payment.captured` and `payment.failed`. Keep secrets server-side; a frontend success callback is never treated as proof of payment.

## REST API

Base path `/api/`. Authentication is by token (`Authorization: Token <key>`) or session. Djoser endpoints (token login, user management) are mounted at `/info/api/auth/`.

| Endpoint | Who | Description |
|---|---|---|
| `GET /api/details/` | Student | Profile details |
| `GET /api/attendance/` | Student | Attendance summary |
| `GET /api/marks/` | Student | Marks |
| `GET /api/timetable/` | Student | Timetable |
| `GET /api/teacher/attendance/history/` | Teacher | Attendance history |
| `GET/POST/DELETE /api/teacher/subjects/` | Teacher | Manage taught subjects |
| `GET /api/meta/registration/` | Public | Departments/courses for registration |
| `POST /api/register/student/`, `/api/register/teacher/` | Public | Submit registration request |

## Testing

```bash
python manage.py check
python manage.py test info apis
```

The suite covers cohort-session mapping, repeat-safe promotion, current-subject synchronisation and fee-ledger reconciliation.

## Production notes

- Set `DJANGO_DEBUG=False`, a strong `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Serve over HTTPS, run `python manage.py collectstatic`, and use a production WSGI/ASGI server (for example gunicorn) behind a reverse proxy. Configure static and media serving yourself; Django does not serve `/media/` when debug is off.
- Move from SQLite to PostgreSQL for multi-user deployments, after taking a verified backup.
- Keep `.env`, `db.sqlite3`, `media/` and payment credentials private and out of version control (already in `.gitignore`).
- Run `python manage.py check --deploy` before launch.

## Known limitations

- Test coverage is small (5 tests in `info/tests.py`; `apis/tests.py` is empty).
- Most business logic lives in a single large `info/views.py`; splitting it into modules would ease maintenance.
- The `info` URLs are mounted both at `/` and `/info/`, so every page has two addresses.
