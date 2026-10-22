import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATABASE = DATA_DIR / "tombola.sqlite3"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
PRICE_CENTS = 200
LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60
login_attempts = {}

if len(ADMIN_PASSWORD) < 16:
    raise RuntimeError("ADMIN_PASSWORD must contain at least 16 characters")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must contain at least 32 characters")

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=SESSION_SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
    MAX_CONTENT_LENGTH=16 * 1024,
)


def connect_db():
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_db():
    with connect_db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS raffles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                year INTEGER NOT NULL DEFAULT 2026,
                description TEXT NOT NULL DEFAULT '',
                primary_color TEXT NOT NULL DEFAULT '#285641',
                accent_color TEXT NOT NULL DEFAULT '#e87a4b',
                logo_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        raffle_columns = {row["name"] for row in connection.execute("PRAGMA table_info(raffles)")}
        if "year" not in raffle_columns:
            connection.execute("ALTER TABLE raffles ADD COLUMN year INTEGER NOT NULL DEFAULT 2026")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                raffle_id INTEGER NOT NULL DEFAULT 1 REFERENCES raffles(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (raffle_id, date)
            )"""
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(bookings)")}
        if "raffle_id" not in columns:
            connection.execute("ALTER TABLE bookings RENAME TO legacy_bookings")
            connection.execute(
                """CREATE TABLE bookings (
                    raffle_id INTEGER NOT NULL REFERENCES raffles(id) ON DELETE CASCADE,
                    date TEXT NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (raffle_id, date)
                )"""
            )
        connection.execute(
            "INSERT OR IGNORE INTO raffles (id, slug, title, year, description) VALUES (1, 'tombola-2026', '365 Chances', 2026, 'Chaque date est un ticket pour le grand tirage.')"
        )
        connection.execute(
            "UPDATE raffles SET title = '365 Chances', description = 'Chaque date est un ticket pour le grand tirage.' WHERE id = 1 AND title = 'Les jours chanceux'"
        )
        if "legacy_bookings" in {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            connection.execute(
                "INSERT OR IGNORE INTO bookings (raffle_id, date, first_name, last_name, phone, created_at) SELECT 1, date, first_name, last_name, phone, created_at FROM legacy_bookings"
            )
            connection.execute("DROP TABLE legacy_bookings")


def raffle_payload(row):
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "year": row["year"],
        "description": row["description"],
        "primaryColor": row["primary_color"],
        "accentColor": row["accent_color"],
        "logoUrl": row["logo_url"],
    }


def get_raffle(raffle_id):
    with connect_db() as connection:
        row = connection.execute("SELECT * FROM raffles WHERE id = ?", (raffle_id,)).fetchone()
    return row


def validated_raffle_fields(body):
    title = body.get("title")
    year = body.get("year", 2026)
    description = body.get("description", "")
    primary_color = body.get("primaryColor", "#285641")
    accent_color = body.get("accentColor", "#e87a4b")
    logo_url = body.get("logoUrl", "")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        return None, "Le titre est obligatoire et limité à 100 caractères."
    if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
        return None, "L’année doit être comprise entre 1900 et 2100."
    if not isinstance(description, str) or len(description.strip()) > 500:
        return None, "La description est limitée à 500 caractères."
    if not all(isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in (primary_color, accent_color)):
        return None, "Les couleurs doivent être au format hexadécimal #RRGGBB."
    if not isinstance(logo_url, str) or len(logo_url.strip()) > 500 or (logo_url.strip() and not (logo_url.strip().startswith("/") and not logo_url.strip().startswith("//") or logo_url.strip().startswith("https://"))):
        return None, "Le logo doit être une URL HTTPS ou un chemin local."
    return (title.strip(), year, description.strip(), primary_color.lower(), accent_color.lower(), logo_url.strip()), None


def require_raffle(raffle_id):
    raffle = get_raffle(raffle_id)
    if not raffle:
        return None, (jsonify(error="Cette tombola n’existe pas."), 404)
    return raffle, None


def valid_slug(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return slug or secrets.token_urlsafe(8).lower()


def booking_payload(row):
    return {
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "phone": row["phone"],
    }


def is_valid_date(value, year):
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.year == year and parsed.isoformat() == value


def require_admin():
    if not session.get("admin"):
        return jsonify(error="Connexion administrateur requise."), 401
    supplied_token = request.headers.get("X-CSRF-Token", "")
    expected_token = session.get("csrf_token", "")
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify(error="Jeton de sécurité invalide. Rechargez la page."), 403
    return None


def login_rate_limited():
    now = time.monotonic()
    address = request.remote_addr or "unknown"
    attempts = [stamp for stamp in login_attempts.get(address, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    login_attempts[address] = attempts
    if len(attempts) >= LOGIN_LIMIT:
        return True
    attempts.append(now)
    return False


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "tombola.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.get("/api/state")
def public_state():
    raffle_id = request.args.get("raffle", type=int)
    with connect_db() as connection:
        if raffle_id is None:
            raffle = connection.execute("SELECT * FROM raffles ORDER BY id LIMIT 1").fetchone()
        else:
            raffle = connection.execute("SELECT * FROM raffles WHERE id = ?", (raffle_id,)).fetchone()
        if not raffle:
            return jsonify(error="Cette tombola n’existe pas."), 404
        dates = [row["date"] for row in connection.execute("SELECT date FROM bookings WHERE raffle_id = ? ORDER BY date", (raffle["id"],))]
    return jsonify(year=raffle["year"], priceCents=PRICE_CENTS, dates=dates, raffle=raffle_payload(raffle))


@app.get("/api/raffles")
def public_raffles():
    with connect_db() as connection:
        raffles = [raffle_payload(row) for row in connection.execute("SELECT * FROM raffles ORDER BY id")]
    return jsonify(raffles=raffles)


@app.get("/api/admin/raffles")
def admin_raffles():
    if not session.get("admin"):
        return jsonify(error="Connexion administrateur requise."), 401
    with connect_db() as connection:
        raffles = [raffle_payload(row) for row in connection.execute("SELECT * FROM raffles ORDER BY id")]
    return jsonify(raffles=raffles)


@app.post("/api/admin/raffles")
def create_raffle():
    denied = require_admin()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    fields, error = validated_raffle_fields(body)
    if error:
        return jsonify(error=error), 400
    title, year, description, primary_color, accent_color, logo_url = fields
    base_slug = valid_slug(title)
    slug = base_slug
    with connect_db() as connection:
        suffix = 2
        while connection.execute("SELECT 1 FROM raffles WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug[:55]}-{suffix}"
            suffix += 1
        cursor = connection.execute(
            "INSERT INTO raffles (slug, title, year, description, primary_color, accent_color, logo_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, title, year, description, primary_color, accent_color, logo_url),
        )
        raffle = connection.execute("SELECT * FROM raffles WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(raffle=raffle_payload(raffle)), 201


@app.put("/api/admin/raffles/<int:raffle_id>")
def update_raffle(raffle_id):
    denied = require_admin()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    fields, error = validated_raffle_fields(body)
    if error:
        return jsonify(error=error), 400
    title, year, description, primary_color, accent_color, logo_url = fields
    current_raffle = get_raffle(raffle_id)
    if not current_raffle:
        return jsonify(error="Cette tombola n’existe pas."), 404
    if year != current_raffle["year"]:
        with connect_db() as connection:
            booking_count = connection.execute("SELECT COUNT(*) FROM bookings WHERE raffle_id = ?", (raffle_id,)).fetchone()[0]
        if booking_count:
            return jsonify(error="Supprimez ou libérez les réservations avant de changer l’année de cette tombola."), 409
    with connect_db() as connection:
        connection.execute(
            "UPDATE raffles SET title = ?, year = ?, description = ?, primary_color = ?, accent_color = ?, logo_url = ? WHERE id = ?",
            (*fields, raffle_id),
        )
        raffle = connection.execute("SELECT * FROM raffles WHERE id = ?", (raffle_id,)).fetchone()
    return jsonify(raffle=raffle_payload(raffle))


@app.delete("/api/admin/raffles/<int:raffle_id>")
def delete_raffle(raffle_id):
    denied = require_admin()
    if denied:
        return denied
    if raffle_id == 1:
        return jsonify(error="La tombola principale ne peut pas être supprimée."), 400
    with connect_db() as connection:
        cursor = connection.execute("DELETE FROM raffles WHERE id = ?", (raffle_id,))
    if cursor.rowcount == 0:
        return jsonify(error="Cette tombola n’existe pas."), 404
    return jsonify(deleted=True)


@app.get("/api/auth")
def auth_state():
    authenticated = bool(session.get("admin"))
    if authenticated:
        session.permanent = True
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return jsonify(authenticated=authenticated, csrfToken=session.get("csrf_token") if authenticated else None)


@app.post("/api/login")
def login():
    if login_rate_limited():
        return jsonify(error="Trop de tentatives. Réessayez dans quelques minutes."), 429
    body = request.get_json(silent=True) or {}
    password = body.get("password", "")
    if not isinstance(password, str) or not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return jsonify(error="Mot de passe incorrect."), 401
    session.clear()
    session.permanent = True
    session["admin"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify(authenticated=True, csrfToken=session["csrf_token"])


@app.post("/api/logout")
def logout():
    denied = require_admin()
    if denied:
        return denied
    session.clear()
    return jsonify(authenticated=False)


@app.get("/api/admin/entries")
def admin_entries():
    if not session.get("admin"):
        return jsonify(error="Connexion administrateur requise."), 401
    raffle_id = request.args.get("raffle", type=int, default=1)
    raffle, denied = require_raffle(raffle_id)
    if denied:
        return denied
    with connect_db() as connection:
        rows = connection.execute("SELECT date, first_name, last_name, phone FROM bookings WHERE raffle_id = ? ORDER BY date", (raffle_id,)).fetchall()
    bookings = {row["date"]: booking_payload(row) for row in rows}
    return jsonify(bookings=bookings, priceCents=PRICE_CENTS, totalCents=len(rows) * PRICE_CENTS, raffle=raffle_payload(raffle))


@app.post("/api/admin/bookings")
def create_booking():
    denied = require_admin()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    raffle_id = body.get("raffleId", 1)
    raffle = get_raffle(raffle_id) if isinstance(raffle_id, int) else None
    if not raffle:
        return jsonify(error="Cette tombola n’existe pas."), 404
    date_value = body.get("date")
    first_name = body.get("firstName")
    last_name = body.get("lastName")
    phone = body.get("phone")
    values = (first_name, last_name, phone)
    if not is_valid_date(date_value, raffle["year"]):
        return jsonify(error=f"Date invalide : sélectionnez une date de {raffle['year']}."), 400
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return jsonify(error="Le prénom, le nom et le téléphone sont obligatoires."), 400
    if len(first_name.strip()) > 60 or len(last_name.strip()) > 60 or len(phone.strip()) > 25:
        return jsonify(error="Un des champs dépasse la longueur autorisée."), 400
    if sum(character.isdigit() for character in phone) < 8:
        return jsonify(error="Numéro de téléphone invalide."), 400
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO bookings (raffle_id, date, first_name, last_name, phone) VALUES (?, ?, ?, ?, ?)",
                (raffle_id, date_value, first_name.strip(), last_name.strip(), phone.strip()),
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Cette date est déjà réservée."), 409
    return jsonify(date=date_value, priceCents=PRICE_CENTS), 201


@app.put("/api/admin/bookings/<date_value>")
def update_booking(date_value):
    denied = require_admin()
    if denied:
        return denied
    raffle_id = request.args.get("raffle", type=int, default=1)
    raffle = get_raffle(raffle_id)
    if not raffle:
        return jsonify(error="Cette tombola n’existe pas."), 404
    if not is_valid_date(date_value, raffle["year"]):
        return jsonify(error="Date invalide."), 400
    body = request.get_json(silent=True) or {}
    first_name = body.get("firstName")
    last_name = body.get("lastName")
    phone = body.get("phone")
    values = (first_name, last_name, phone)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return jsonify(error="Le prénom, le nom et le téléphone sont obligatoires."), 400
    if len(first_name.strip()) > 60 or len(last_name.strip()) > 60 or len(phone.strip()) > 25:
        return jsonify(error="Un des champs dépasse la longueur autorisée."), 400
    if sum(character.isdigit() for character in phone) < 8:
        return jsonify(error="Numéro de téléphone invalide."), 400
    with connect_db() as connection:
        cursor = connection.execute(
            "UPDATE bookings SET first_name = ?, last_name = ?, phone = ? WHERE raffle_id = ? AND date = ?",
            (first_name.strip(), last_name.strip(), phone.strip(), raffle_id, date_value),
        )
    if cursor.rowcount == 0:
        return jsonify(error="Cette date n'est pas réservée."), 404
    return jsonify(date=date_value, priceCents=PRICE_CENTS)


@app.delete("/api/admin/bookings/<date_value>")
def delete_booking(date_value):
    denied = require_admin()
    if denied:
        return denied
    raffle_id = request.args.get("raffle", type=int, default=1)
    raffle = get_raffle(raffle_id)
    if not raffle:
        return jsonify(error="Cette tombola n’existe pas."), 404
    if not is_valid_date(date_value, raffle["year"]):
        return jsonify(error="Date invalide."), 400
    with connect_db() as connection:
        cursor = connection.execute("DELETE FROM bookings WHERE raffle_id = ? AND date = ?", (raffle_id, date_value))
    if cursor.rowcount == 0:
        return jsonify(error="Cette date n'est pas réservée."), 404
    return jsonify(date=date_value, deleted=True)


@app.post("/api/admin/draw")
def draw_winner():
    denied = require_admin()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    raffle_id = body.get("raffleId", 1)
    if not isinstance(raffle_id, int) or not get_raffle(raffle_id):
        return jsonify(error="Cette tombola n’existe pas."), 404
    with connect_db() as connection:
        rows = connection.execute("SELECT date, first_name, last_name, phone FROM bookings WHERE raffle_id = ? ORDER BY date", (raffle_id,)).fetchall()
    if not rows:
        return jsonify(error="Aucune date réservée pour le tirage."), 400
    winner = secrets.choice(rows)
    return jsonify(date=winner["date"], winner=booking_payload(winner))


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error="Requête trop volumineuse."), 413


initialize_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
