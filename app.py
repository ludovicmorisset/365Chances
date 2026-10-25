import base64
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATABASE = DATA_DIR / "tombola.sqlite3"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
ALLOW_ADMIN_SIGNUP = os.environ.get("ALLOW_ADMIN_SIGNUP", "false").lower() == "true"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_ENVIRONMENT = os.environ.get("PAYPAL_ENVIRONMENT", "sandbox").lower()
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
if any((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER)) and not all((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER)):
    raise RuntimeError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER must all be configured")
if PAYPAL_ENVIRONMENT not in ("sandbox", "live"):
    raise RuntimeError("PAYPAL_ENVIRONMENT must be 'sandbox' or 'live'")
PAYPAL_BASE_URL = "https://api-m.paypal.com" if PAYPAL_ENVIRONMENT == "live" else "https://api-m.sandbox.paypal.com"
PRICE_CENTS = 200
BOOKING_HOLD_MINUTES = 15
PAYMENT_RATE_LIMIT = 20
payment_attempts = {}
LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60
VERIFICATION_TOKEN_HOURS = 24
login_attempts = {}

if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 16:
    raise RuntimeError("ADMIN_PASSWORD must contain at least 16 characters")
if ALLOW_ADMIN_SIGNUP and not all((SMTP_HOST, SMTP_FROM, PUBLIC_URL)):
    raise RuntimeError("SMTP_HOST, SMTP_FROM and PUBLIC_URL are required when ALLOW_ADMIN_SIGNUP is enabled")

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
            """CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                verified_at TEXT,
                verification_token_hash TEXT,
                verification_expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        if ADMIN_PASSWORD:
            connection.execute(
                "INSERT OR IGNORE INTO admin_users (username, email, password_hash, verified_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (ADMIN_USERNAME, os.environ.get("ADMIN_EMAIL", "admin@localhost"), generate_password_hash(ADMIN_PASSWORD, method="pbkdf2:sha256")),
            )
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
        if "legacy_bookings" in {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            connection.execute(
                "INSERT OR IGNORE INTO bookings (raffle_id, date, first_name, last_name, phone, created_at) SELECT 1, date, first_name, last_name, phone, created_at FROM legacy_bookings"
            )
            connection.execute("DROP TABLE legacy_bookings")
        connection.execute(
            "DELETE FROM raffles WHERE id = 1 AND slug = 'tombola-2026' AND title = '365 Chances' AND description = 'Chaque date est un ticket pour le grand tirage.' AND NOT EXISTS (SELECT 1 FROM bookings WHERE raffle_id = 1)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS payment_orders (
                id TEXT PRIMARY KEY,
                raffle_id INTEGER NOT NULL REFERENCES raffles(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                paypal_order_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS payment_orders_hold_idx ON payment_orders (raffle_id, date, status, expires_at)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS payment_order_dates (
                payment_order_id TEXT NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                PRIMARY KEY (payment_order_id, date)
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS payment_order_dates_date_idx ON payment_order_dates (date, payment_order_id)")


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
    if not session.get("admin_user_id"):
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


def paypal_request(path, payload=None, access_token=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers["Prefer"] = "return=representation"
    request_data = urllib.request.Request(f"{PAYPAL_BASE_URL}{path}", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request_data, timeout=20) as response:
        return json.loads(response.read())


def paypal_get(path, access_token):
    request_data = urllib.request.Request(f"{PAYPAL_BASE_URL}{path}", headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request_data, timeout=20) as response:
        return json.loads(response.read())


def payment_order_dates(connection, payment_order):
    rows = connection.execute(
        "SELECT date FROM payment_order_dates WHERE payment_order_id = ? ORDER BY date",
        (payment_order["id"],),
    ).fetchall()
    return [row["date"] for row in rows] or [payment_order["date"]]


def validate_paypal_order(paypal_order, payment_order):
    if paypal_order.get("id") != payment_order["paypal_order_id"] or paypal_order.get("status") != "COMPLETED":
        return False
    purchase_units = paypal_order.get("purchase_units", [])
    if len(purchase_units) != 1 or purchase_units[0].get("custom_id") != payment_order["id"]:
        return False
    captures = purchase_units[0].get("payments", {}).get("captures", [])
    if len(captures) != 1 or captures[0].get("status") != "COMPLETED":
        return False
    with connect_db() as connection:
        total_dates = len(payment_order_dates(connection, payment_order))
    amount = captures[0].get("amount", {})
    expected_amount = f"{PRICE_CENTS * total_dates / 100:.2f}"
    return amount.get("currency_code") == "EUR" and amount.get("value") == expected_amount


def complete_paypal_booking(payment_order_id):
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute("SELECT * FROM payment_orders WHERE id = ?", (payment_order_id,)).fetchone()
        if not order:
            return False
        if order["status"] == "completed":
            return True
        dates = payment_order_dates(connection, order)
        connection.executemany(
            "INSERT INTO bookings (raffle_id, date, first_name, last_name, phone) VALUES (?, ?, ?, ?, ?)",
            [(order["raffle_id"], booking_date, order["first_name"], order["last_name"], order["phone"]) for booking_date in dates],
        )
        connection.execute("UPDATE payment_orders SET status = 'completed' WHERE id = ?", (payment_order_id,))
        raffle = connection.execute("SELECT title FROM raffles WHERE id = ?", (order["raffle_id"],)).fetchone()
    for booking_date in dates:
        send_booking_confirmation_sms(order["phone"], order["first_name"], booking_date, raffle["title"])
    return True


def send_booking_confirmation_sms(phone, first_name, booking_date, raffle_title):
    if not all((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER)):
        return
    message = (
        f"Bonjour {first_name}, votre réservation du {booking_date} pour la tombola "
        f"« {raffle_title} » est confirmée. Merci pour votre participation !"
    )
    payload = urllib.parse.urlencode({"From": TWILIO_FROM_NUMBER, "To": phone, "Body": message}).encode()
    credentials = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    sms_request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(TWILIO_ACCOUNT_SID)}/Messages.json",
        data=payload,
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(sms_request, timeout=20) as response:
            response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        app.logger.error("Échec de l’envoi du SMS de confirmation : %s", error)


def paypal_access_token():
    credentials = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    request_data = urllib.request.Request(
        f"{PAYPAL_BASE_URL}/v1/oauth2/token",
        data=credentials,
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request_data, timeout=20) as response:
        return json.loads(response.read())["access_token"]


def payments_rate_limited():
    now = time.monotonic()
    address = request.remote_addr or "unknown"
    attempts = [stamp for stamp in payment_attempts.get(address, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    payment_attempts[address] = attempts
    if len(attempts) >= PAYMENT_RATE_LIMIT:
        return True
    attempts.append(now)
    return False


def send_verification_email(email, token):
    verification_url = f"{PUBLIC_URL.rstrip('/')}/api/verify-email?token={token}"
    message = EmailMessage()
    message["Subject"] = "Validez votre compte administrateur"
    message["From"] = SMTP_FROM
    message["To"] = email
    message.set_content(f"Pour valider votre compte administrateur, ouvrez ce lien dans les prochaines 24 heures : {verification_url}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        if SMTP_USE_TLS:
            server.starttls(context=ssl.create_default_context())
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)


def store_verification_token(connection, user_id):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_HOURS)).isoformat()
    connection.execute(
        "UPDATE admin_users SET verification_token_hash = ?, verification_expires_at = ? WHERE id = ?",
        (hashlib.sha256(token.encode()).hexdigest(), expires_at, user_id),
    )
    return token


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
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(BASE_DIR / "assets", filename)


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
        dates = [row["date"] for row in connection.execute(
            "SELECT date FROM bookings WHERE raffle_id = ? UNION SELECT COALESCE(payment_order_dates.date, payment_orders.date) FROM payment_orders LEFT JOIN payment_order_dates ON payment_order_dates.payment_order_id = payment_orders.id WHERE payment_orders.raffle_id = ? AND payment_orders.status = 'pending' AND payment_orders.expires_at > ? ORDER BY date",
            (raffle["id"], raffle["id"], datetime.now(timezone.utc).isoformat()),
        )]
    return jsonify(year=raffle["year"], priceCents=PRICE_CENTS, dates=dates, raffle=raffle_payload(raffle), paymentEnabled=bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET))


@app.post("/api/payments/paypal/orders")
def create_paypal_order():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return jsonify(error="Le paiement en ligne n’est pas encore configuré."), 503
    if payments_rate_limited():
        return jsonify(error="Trop de tentatives. Réessayez dans quelques minutes."), 429
    body = request.get_json(silent=True) or {}
    raffle_id = body.get("raffleId")
    raffle = get_raffle(raffle_id) if isinstance(raffle_id, int) and not isinstance(raffle_id, bool) else None
    if not raffle:
        return jsonify(error="Cette tombola n’existe pas."), 404
    date_values = body.get("dates")
    if date_values is None:
        date_values = [body.get("date")]
    first_name = body.get("firstName")
    last_name = body.get("lastName")
    phone = body.get("phone")
    if not isinstance(date_values, list) or not date_values or len(date_values) > 366 or any(not isinstance(value, str) for value in date_values) or len(set(date_values)) != len(date_values):
        return jsonify(error="Sélectionnez une ou plusieurs dates différentes."), 400
    if any(not is_valid_date(date_value, raffle["year"]) for date_value in date_values):
        return jsonify(error=f"Date invalide : sélectionnez des dates de {raffle['year']}."), 400
    date_values = sorted(date_values)
    if any(not isinstance(value, str) or not value.strip() for value in (first_name, last_name, phone)):
        return jsonify(error="Le prénom, le nom et le téléphone sont obligatoires."), 400
    if len(first_name.strip()) > 60 or len(last_name.strip()) > 60 or len(phone.strip()) > 25 or sum(character.isdigit() for character in phone.strip()) < 8:
        return jsonify(error="Vérifiez le prénom, le nom et le numéro de téléphone."), 400
    order_id = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=BOOKING_HOLD_MINUTES)).isoformat()
    try:
        with connect_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            for date_value in date_values:
                if connection.execute("SELECT 1 FROM bookings WHERE raffle_id = ? AND date = ?", (raffle_id, date_value)).fetchone() or connection.execute(
                    "SELECT 1 FROM payment_orders p LEFT JOIN payment_order_dates d ON d.payment_order_id = p.id WHERE p.raffle_id = ? AND COALESCE(d.date, p.date) = ? AND p.status = 'pending' AND p.expires_at > ?",
                    (raffle_id, date_value, now),
                ).fetchone():
                    return jsonify(error="Une des dates vient d’être réservée ou est en cours de paiement. Actualisez le calendrier."), 409
            connection.execute(
                "INSERT INTO payment_orders (id, raffle_id, date, first_name, last_name, phone, status, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (order_id, raffle_id, date_values[0], first_name.strip(), last_name.strip(), phone.strip(), expires_at),
            )
            connection.executemany(
                "INSERT INTO payment_order_dates (payment_order_id, date) VALUES (?, ?)",
                [(order_id, date_value) for date_value in date_values],
            )
        access_token = paypal_access_token()
        origin = PUBLIC_URL.rstrip("/") or request.url_root.rstrip("/")
        order = paypal_request("/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": order_id,
                "custom_id": order_id,
                "description": f"Tombola {raffle['title']} — {len(date_values)} date(s)",
                "amount": {"currency_code": "EUR", "value": f"{PRICE_CENTS * len(date_values) / 100:.2f}"},
            }],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": "365 Chances",
                        "user_action": "PAY_NOW",
                        "return_url": f"{origin}/?paypal=success&raffle={raffle_id}",
                        "cancel_url": f"{origin}/?paypal=cancel&raffle={raffle_id}&hold={order_id}",
                    },
                },
            },
        }, access_token)
        approval_url = next((link["href"] for link in order.get("links", []) if link.get("rel") in ("approve", "payer-action")), None)
        if not approval_url:
            raise RuntimeError("PayPal n’a pas fourni de lien de paiement.")
        with connect_db() as connection:
            connection.execute("UPDATE payment_orders SET paypal_order_id = ? WHERE id = ? AND status = 'pending'", (order["id"], order_id))
        return jsonify(approvalUrl=approval_url, orderId=order["id"], expiresInMinutes=BOOKING_HOLD_MINUTES), 201
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, KeyError, RuntimeError, json.JSONDecodeError, ValueError) as error:
        with connect_db() as connection:
            connection.execute("DELETE FROM payment_orders WHERE id = ? AND status = 'pending' AND paypal_order_id IS NULL", (order_id,))
        app.logger.error("Échec de création de commande PayPal : %s", error)
        return jsonify(error="Impossible de créer le paiement PayPal. Réessayez."), 503


@app.post("/api/payments/paypal/capture")
def capture_paypal_order():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return jsonify(error="Le paiement en ligne n’est pas encore configuré."), 503
    body = request.get_json(silent=True) or {}
    paypal_order_id = body.get("orderId")
    if not isinstance(paypal_order_id, str) or len(paypal_order_id) > 100:
        return jsonify(error="Commande de paiement invalide."), 400
    with connect_db() as connection:
        order_row = connection.execute("SELECT * FROM payment_orders WHERE paypal_order_id = ?", (paypal_order_id,)).fetchone()
    if not order_row:
        return jsonify(error="Cette commande de paiement est inconnue ou expirée."), 404
    if order_row["status"] == "completed":
        with connect_db() as connection:
            dates = payment_order_dates(connection, order_row)
        return jsonify(dates=dates, raffleId=order_row["raffle_id"], completed=True)
    if order_row["expires_at"] <= datetime.now(timezone.utc).isoformat():
        return jsonify(error="Le délai de paiement est dépassé. Sélectionnez à nouveau votre date."), 410
    try:
        access_token = paypal_access_token()
        result = paypal_get(f"/v2/checkout/orders/{urllib.parse.quote(paypal_order_id)}", access_token)
        if result.get("status") == "APPROVED":
            result = paypal_request(f"/v2/checkout/orders/{urllib.parse.quote(paypal_order_id)}/capture", {}, access_token)
        if not validate_paypal_order(result, order_row):
            return jsonify(error="Le paiement n’a pas pu être confirmé."), 402
    except urllib.error.HTTPError as error:
        if error.code == 422:
            return jsonify(error="Le paiement PayPal n’est pas validé."), 402
        app.logger.error("Échec de confirmation PayPal : %s", error)
        return jsonify(error="Impossible de confirmer le paiement. Contactez l’organisateur avant de réessayer."), 503
    except (OSError, urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as error:
        app.logger.error("Réponse PayPal invalide : %s", error)
        return jsonify(error="Impossible de confirmer le paiement. Contactez l’organisateur avant de réessayer."), 503
    try:
        completed = complete_paypal_booking(order_row["id"])
        if not completed:
            return jsonify(error="Cette réservation de paiement a expiré."), 410
    except sqlite3.IntegrityError:
        app.logger.error("Paiement confirmé mais date déjà réservée, commande PayPal %s", paypal_order_id)
        return jsonify(error="Paiement reçu, mais la date a été prise entre-temps. Contactez l’organisateur pour un remboursement."), 409
    with connect_db() as connection:
        dates = payment_order_dates(connection, order_row)
    return jsonify(dates=dates, raffleId=order_row["raffle_id"], completed=True)


@app.post("/api/payments/paypal/webhook")
def paypal_webhook():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return jsonify(error="Le paiement en ligne n’est pas configuré."), 503
    body = request.get_json(silent=True) or {}
    headers = {
        "auth_algo": request.headers.get("PayPal-Auth-Algo", ""),
        "cert_url": request.headers.get("PayPal-Cert-Url", ""),
        "transmission_id": request.headers.get("PayPal-Transmission-Id", ""),
        "transmission_sig": request.headers.get("PayPal-Transmission-Sig", ""),
        "transmission_time": request.headers.get("PayPal-Transmission-Time", ""),
        "webhook_id": os.environ.get("PAYPAL_WEBHOOK_ID", ""),
        "webhook_event": body,
    }
    if not headers["webhook_id"]:
        return jsonify(error="Le webhook PayPal n’est pas configuré."), 503
    try:
        result = paypal_request("/v1/notifications/verify-webhook-signature", headers, paypal_access_token())
        if result.get("verification_status") != "SUCCESS":
            return jsonify(error="Signature de webhook invalide."), 400
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as error:
        app.logger.error("Échec de validation du webhook PayPal : %s", error)
        return jsonify(error="Validation PayPal indisponible."), 503
    event = body.get("event_type")
    resource = body.get("resource", {})
    related_ids = resource.get("supplementary_data", {}).get("related_ids", {})
    paypal_order_id = related_ids.get("order_id")
    if event == "PAYMENT.CAPTURE.COMPLETED" and isinstance(paypal_order_id, str):
        try:
            paypal_order = paypal_get(f"/v2/checkout/orders/{urllib.parse.quote(paypal_order_id)}", paypal_access_token())
            custom_id = paypal_order.get("purchase_units", [{}])[0].get("custom_id")
            with connect_db() as connection:
                order = connection.execute("SELECT * FROM payment_orders WHERE id = ?", (custom_id,)).fetchone()
            if order and order["paypal_order_id"] == paypal_order_id and validate_paypal_order(paypal_order, order):
                complete_paypal_booking(order["id"])
        except (OSError, sqlite3.IntegrityError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, IndexError) as error:
            app.logger.error("Échec de finalisation du paiement webhook %s : %s", paypal_order_id, error)
            return jsonify(error="Impossible de finaliser la réservation."), 503
    return jsonify(received=True)


@app.post("/api/payments/paypal/cancel")
def cancel_paypal_order():
    body = request.get_json(silent=True) or {}
    order_id = body.get("orderId")
    if not isinstance(order_id, str):
        return jsonify(error="Commande invalide."), 400
    with connect_db() as connection:
        connection.execute("DELETE FROM payment_orders WHERE (id = ? OR paypal_order_id = ?) AND status = 'pending'", (order_id, order_id))
    return jsonify(cancelled=True)


@app.get("/api/raffles")
def public_raffles():
    with connect_db() as connection:
        raffles = [raffle_payload(row) for row in connection.execute("SELECT * FROM raffles ORDER BY id")]
    return jsonify(raffles=raffles)


@app.get("/api/admin/raffles")
def admin_raffles():
    if not session.get("admin_user_id"):
        return jsonify(error="Connexion administrateur requise."), 401
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT raffles.*, (SELECT COUNT(*) FROM bookings WHERE bookings.raffle_id = raffles.id) AS booking_count FROM raffles ORDER BY id"
        ).fetchall()
        raffles = [{**raffle_payload(row), "bookingCount": row["booking_count"]} for row in rows]
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
            booking_count = connection.execute(
                "SELECT (SELECT COUNT(*) FROM bookings WHERE raffle_id = ?) + (SELECT COUNT(*) FROM payment_orders WHERE raffle_id = ? AND status = 'pending' AND expires_at > ?)",
                (raffle_id, raffle_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()[0]
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
    with connect_db() as connection:
        cursor = connection.execute("DELETE FROM raffles WHERE id = ?", (raffle_id,))
    if cursor.rowcount == 0:
        return jsonify(error="Cette tombola n’existe pas."), 404
    return jsonify(deleted=True)


@app.get("/api/auth")
def auth_state():
    authenticated = bool(session.get("admin_user_id"))
    if authenticated:
        session.permanent = True
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return jsonify(authenticated=authenticated, signupEnabled=ALLOW_ADMIN_SIGNUP, csrfToken=session.get("csrf_token") if authenticated else None)


@app.post("/api/register")
def register():
    if not ALLOW_ADMIN_SIGNUP:
        return jsonify(error="La création de compte est désactivée."), 403
    if login_rate_limited():
        return jsonify(error="Trop de tentatives. Réessayez dans quelques minutes."), 429
    body = request.get_json(silent=True) or {}
    username = body.get("username", "")
    email = body.get("email", "")
    password = body.get("password", "")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
        return jsonify(error="L’identifiant doit contenir de 3 à 40 caractères (lettres, chiffres, ., _ ou -)."), 400
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(error="Adresse email invalide."), 400
    if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
        return jsonify(error="Le mot de passe doit contenir au moins 12 caractères."), 400
    try:
        with connect_db() as connection:
            existing = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? OR email = ?",
                (username, email),
            ).fetchone()
            if existing and existing["verified_at"]:
                return jsonify(error="Cet identifiant ou cette adresse email est déjà utilisé."), 409
            if existing:
                user_id = existing["id"]
                connection.execute(
                    "UPDATE admin_users SET username = ?, email = ?, password_hash = ? WHERE id = ?",
                    (username, email, generate_password_hash(password, method="pbkdf2:sha256"), user_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO admin_users (username, email, password_hash) VALUES (?, ?, ?)",
                    (username, email, generate_password_hash(password, method="pbkdf2:sha256")),
                )
                user_id = cursor.lastrowid
            token = store_verification_token(connection, user_id)
        send_verification_email(email, token)
    except sqlite3.IntegrityError:
        return jsonify(error="Cet identifiant ou cette adresse email est déjà utilisé."), 409
    except (OSError, smtplib.SMTPException) as error:
        app.logger.error("Échec de l’envoi du mail de validation : %s", error)
        return jsonify(error="Impossible d’envoyer le mail de validation. Vérifiez la configuration SMTP."), 503
    return jsonify(message="Un lien de validation vient d’être envoyé par email."), 201


@app.get("/api/verify-email")
def verify_email():
    token = request.args.get("token", "")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as connection:
        user = connection.execute(
            "SELECT id, verification_expires_at FROM admin_users WHERE verification_token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not user or not user["verification_expires_at"] or user["verification_expires_at"] < datetime.now(timezone.utc).isoformat():
            return "Lien invalide ou expiré. Vous pouvez soumettre à nouveau le formulaire d’inscription.", 400
        connection.execute(
            "UPDATE admin_users SET verified_at = CURRENT_TIMESTAMP, verification_token_hash = NULL, verification_expires_at = NULL WHERE id = ?",
            (user["id"],),
        )
    return "Adresse email validée. Vous pouvez maintenant vous connecter à l’administration.", 200


@app.post("/api/login")
def login():
    if login_rate_limited():
        return jsonify(error="Trop de tentatives. Réessayez dans quelques minutes."), 429
    body = request.get_json(silent=True) or {}
    username = body.get("username", "")
    password = body.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 40 or len(password) > 256:
        return jsonify(error="Identifiant ou mot de passe incorrect."), 401
    with connect_db() as connection:
        user = connection.execute("SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    if not user:
        check_password_hash(generate_password_hash("invalid-password", method="pbkdf2:sha256"), password)
        return jsonify(error="Identifiant ou mot de passe incorrect, ou adresse email non validée."), 401
    if not user["verified_at"] or not check_password_hash(user["password_hash"], password):
        return jsonify(error="Identifiant ou mot de passe incorrect, ou adresse email non validée."), 401
    session.clear()
    session.permanent = True
    session["admin_user_id"] = user["id"]
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
    if not session.get("admin_user_id"):
        return jsonify(error="Connexion administrateur requise."), 401
    raffle_id = request.args.get("raffle", type=int, default=1)
    raffle, denied = require_raffle(raffle_id)
    if denied:
        return denied
    with connect_db() as connection:
        rows = connection.execute("SELECT date, first_name, last_name, phone FROM bookings WHERE raffle_id = ? ORDER BY date", (raffle_id,)).fetchall()
    bookings = {row["date"]: booking_payload(row) for row in rows}
    return jsonify(bookings=bookings, priceCents=PRICE_CENTS, totalCents=len(rows) * PRICE_CENTS, raffle=raffle_payload(raffle))


@app.get("/api/admin/entries.csv")
def export_admin_entries():
    if not session.get("admin_user_id"):
        return jsonify(error="Connexion administrateur requise."), 401
    raffle_id = request.args.get("raffle", type=int, default=1)
    raffle, denied = require_raffle(raffle_id)
    if denied:
        return denied
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT date, first_name, last_name, phone FROM bookings WHERE raffle_id = ? ORDER BY date",
            (raffle_id,),
        ).fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Date", "Prénom", "Nom", "Téléphone", "Prix (€)"])
    for row in rows:
        values = [row["date"], row["first_name"], row["last_name"], row["phone"], f"{PRICE_CENTS / 100:.2f}"]
        writer.writerow([f"'{value}" if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value for value in values])
    filename = f"reservations-{raffle['slug']}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM payment_orders p LEFT JOIN payment_order_dates d ON d.payment_order_id = p.id WHERE p.raffle_id = ? AND COALESCE(d.date, p.date) = ? AND p.status = 'pending' AND p.expires_at > ?",
                (raffle_id, date_value, datetime.now(timezone.utc).isoformat()),
            ).fetchone():
                return jsonify(error="Cette date est actuellement en cours de paiement."), 409
            connection.execute(
                "INSERT INTO bookings (raffle_id, date, first_name, last_name, phone) VALUES (?, ?, ?, ?, ?)",
                (raffle_id, date_value, first_name.strip(), last_name.strip(), phone.strip()),
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Cette date est déjà réservée."), 409
    send_booking_confirmation_sms(phone.strip(), first_name.strip(), date_value, raffle["title"])
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
