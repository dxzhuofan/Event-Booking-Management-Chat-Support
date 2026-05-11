from flask import Flask, flash, redirect, request, jsonify, render_template, session, url_for
from flask_cors import CORS
from dotenv import load_dotenv
import os
import json
import re
import smtplib
import ssl
import threading
from decimal import Decimal, InvalidOperation
from datetime import date as date_cls, datetime, timedelta, timezone
from email.message import EmailMessage
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from sqlalchemy import inspect, text, func

from models import db, User, VenuePackage, Booking, ChatMessage, EmailLog

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
TEMPLATES_DIR = os.path.join(BASE_DIR, "..", "templates")
STATIC_DIR = os.path.join(BASE_DIR, "..", "static")

load_dotenv(os.path.join(ROOT_DIR, ".env"))


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default=False):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY must be set in your local .env file or environment.")

DB_PATH = os.path.join(BASE_DIR, "eventbook.sqlite")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("SQLALCHEMY_DATABASE_URI", f"sqlite:///{DB_PATH}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = _env_int("SMTP_PORT", 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)
SMTP_USE_SSL = _env_bool("SMTP_USE_SSL", False)
MAIL_FROM = (os.environ.get("MAIL_FROM") or SMTP_USERNAME or "noreply@dashdesk.local").strip() or "noreply@dashdesk.local"
EMAIL_REMINDER_INTERVAL_SECONDS = max(_env_int("EMAIL_REMINDER_INTERVAL_SECONDS", 900), 60)
EMAIL_REMINDER_LEAD_HOURS = max(_env_int("EMAIL_REMINDER_LEAD_HOURS", 72), 1)

_email_reminder_thread_started = False
_email_reminder_stop_event = threading.Event()

VENUE_SEED_DATA = [
    {
        "name": "Economy Session",
        "location_label": "Lobby Space",
        "price": Decimal("2700.00"),
        "image_url": "/static/css/assets/Lobby_space.jpg",
        "features": ["20-30 pax", "WiFi", "Basic AV"],
    },
    {
        "name": "Compact Summit",
        "location_label": "Studio Hall",
        "price": Decimal("3909.00"),
        "image_url": "/static/css/assets/studio_hall.jpg",
        "features": ["40-60 pax", "Stage Mic", "Recording"],
    },
    {
        "name": "Executive Conference",
        "location_label": "Premium Hall",
        "price": Decimal("5912.00"),
        "image_url": "/static/css/assets/prem_hall.jpeg",
        "features": ["80-120 pax", "Lighting", "Sound System"],
    },
    {
        "name": "Luxury Gala Setup",
        "location_label": "Grand Ballroom",
        "price": Decimal("8500.00"),
        "image_url": "/static/css/assets/grand_ball.jpg",
        "features": ["200-300 pax", "Full AV", "Catering Ready"],
    },
]

db.init_app(app)
CORS(app)  # This allows your frontend to connect to the backend


def seed_venue_packages():
    changed = False

    for item in VENUE_SEED_DATA:
        venue = VenuePackage.query.filter_by(name=item["name"]).first()
        if venue is None:
            db.session.add(
                VenuePackage(
                    name=item["name"],
                    location_label=item["location_label"],
                    price=item["price"],
                    image_url=item["image_url"],
                    features_json=json.dumps(item["features"]),
                )
            )
            changed = True
            continue

        if not (venue.image_url or "").strip():
            venue.image_url = item["image_url"]
            changed = True

    if changed:
        db.session.commit()


def _ensure_venue_package_image_column():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns("venue_packages")}
    if "image_url" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE venue_packages ADD COLUMN image_url VARCHAR(255)"))


def _ensure_booking_payment_status_column():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns("bookings")}
    if "payment_status" not in columns:
        try:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE bookings ADD COLUMN payment_status VARCHAR(16) NOT NULL DEFAULT 'unpaid'"))
        except Exception as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _admin_json_forbidden():
    return jsonify({"status": "error", "message": "Forbidden"}), 403


def _request_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form.to_dict(flat=True)


def _normalize_features(raw_features):
    if raw_features is None:
        return []
    if isinstance(raw_features, list):
        values = raw_features
    else:
        text_value = str(raw_features).strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            values = parsed
        else:
            values = re.split(r"[,\n]+", text_value)
    return [str(item).strip() for item in values if str(item).strip()]


def _parse_price(raw_price):
    if raw_price is None:
        return None
    text_value = str(raw_price).strip()
    if not text_value:
        return None
    try:
        price = Decimal(text_value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if price < 0:
        return None
    return price.quantize(Decimal("0.01"))


def _save_uploaded_image(uploaded_file):
    if not uploaded_file or not getattr(uploaded_file, "filename", ""):
        return None

    filename = secure_filename(uploaded_file.filename)
    if not filename:
        return None

    base_name, extension = os.path.splitext(filename)
    if not extension:
        extension = ".jpg"
    final_name = f"{uuid4().hex}_{base_name}{extension}"
    final_path = os.path.join(UPLOAD_DIR, final_name)
    uploaded_file.save(final_path)
    return f"/static/uploads/{final_name}"


def _resolve_image_url(payload):
    uploaded_image = request.files.get("image_file")
    if uploaded_image and uploaded_image.filename:
        saved_path = _save_uploaded_image(uploaded_image)
        if saved_path:
            return saved_path
    return (payload.get("image_url") or "").strip()


def _venue_payload_or_error(existing_venue=None):
    payload = _request_payload()
    name = (payload.get("name") or (existing_venue.name if existing_venue else "")).strip()
    location_label = (payload.get("location_label") or (existing_venue.location_label if existing_venue else "")).strip()
    image_url = _resolve_image_url(payload)
    if not image_url and existing_venue is not None:
        image_url = existing_venue.image_url or ""

    raw_price = payload.get("price")
    if raw_price in (None, "") and existing_venue is not None:
        raw_price = existing_venue.price
    price = _parse_price(raw_price)

    features = _normalize_features(payload.get("features", existing_venue.features() if existing_venue else []))

    if not name or not location_label or price is None:
        return None, (jsonify({"status": "error", "message": "Name, location label, and price are required."}), 400)

    return {
        "name": name,
        "location_label": location_label,
        "image_url": image_url,
        "price": price,
        "features_json": json.dumps(features),
    }, None


with app.app_context():
    db.create_all()
    _ensure_venue_package_image_column()
    _ensure_booking_payment_status_column()
    seed_venue_packages()


def _require_login():
    return bool(session.get("authenticated_user"))


def _require_admin():
    return bool(session.get("is_admin"))


def _parse_time_hhmm(value: str) -> str | None:
    value = (value or "").strip()
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hours, minutes = value.split(":")
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        return None
    return value


def _time_to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _chat_owner_id():
    owner_id = session.get("authenticated_user")
    if owner_id:
        return owner_id

    if not session.get("chat_session_id"):
        session["chat_session_id"] = uuid4().hex
    return session["chat_session_id"]


def _format_relative_time(dt_value):
    if not dt_value:
        return ""

    delta = datetime.now(dt_value.tzinfo or None) - dt_value
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _booking_status_label(booking):
    if booking.status in {"upcoming", "completed", "cancelled"}:
        return booking.status
    return "completed" if booking.date_iso < date_cls.today().isoformat() else "upcoming"


def _booking_amount_total():
    total = db.session.query(func.coalesce(func.sum(Booking.total), 0)).scalar()
    return float(total or 0)


def _booking_start_datetime(booking):
    try:
        return datetime.strptime(f"{booking.date_iso} {booking.start_time}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _email_recipient_name(email_address):
    user = User.query.filter_by(email=email_address).first()
    return user.full_name if user else email_address


def _email_log_exists(booking_id, template_name):
    return (
        EmailLog.query.filter_by(booking_id=booking_id, template_name=template_name, status="sent").first()
        is not None
    )


def _is_email_request(normalized_text):
    if not normalized_text:
        return False

    email_terms = (
        "email me",
        "email this",
        "send me an email",
        "send this to my email",
        "send to my email",
        "send me a copy",
        "send a copy",
        "gmail",
        "mail me",
    )
    return any(term in normalized_text for term in email_terms)


def _build_booking_confirmation_email(booking):
    venue_name = booking.venue_package.name if booking.venue_package else "your venue"
    display_name = _email_recipient_name(booking.user_email)
    lines = [
        f"Hello {display_name},",
        "",
        f"Your booking for {venue_name} has been confirmed.",
        f"Date: {_format_display_date(booking.date_iso)}",
        f"Time: {_format_time_range_display(booking.start_time, booking.end_time)}",
        f"Location: {booking.venue_package.location_label if booking.venue_package else '-'}",
        f"Subtotal: {_format_currency(booking.subtotal)}",
        f"Tax: {_format_currency(booking.tax)}",
        f"Total: {_format_currency(booking.total)}",
        f"Payment status: {(booking.payment_status or 'unpaid').replace('_', ' ').title()}",
        "",
        "Open DashDesk to review or update the booking details.",
    ]
    subject = f"Booking confirmed: {venue_name} on {_format_display_date(booking.date_iso)}"
    return subject, "\n".join(lines)


def _build_booking_reminder_email(booking):
    venue_name = booking.venue_package.name if booking.venue_package else "your venue"
    display_name = _email_recipient_name(booking.user_email)
    booking_start = _booking_start_datetime(booking)
    reminder_window = f"within the next {EMAIL_REMINDER_LEAD_HOURS} hour(s)"
    if booking_start is not None:
        delta = booking_start - datetime.now()
        days_until = max(int(delta.total_seconds() // 86400), 0)
        reminder_window = f"in about {days_until} day(s)"

    lines = [
        f"Hello {display_name},",
        "",
        f"This is a reminder that your DashDesk booking for {venue_name} is coming up {reminder_window}.",
        f"Date: {_format_display_date(booking.date_iso)}",
        f"Time: {_format_time_range_display(booking.start_time, booking.end_time)}",
        f"Location: {booking.venue_package.location_label if booking.venue_package else '-'}",
        f"Payment status: {(booking.payment_status or 'unpaid').replace('_', ' ').title()}",
        "",
        "If payment is still pending, please complete it before the event date.",
    ]
    subject = f"Reminder: upcoming booking for {venue_name}"
    return subject, "\n".join(lines)


def _build_chat_follow_up_email(user_text, context, user_email, reply_text):
    context = _normalize_chat_context(context)
    display_name = _email_recipient_name(user_email)
    selected_date = _parse_chat_date(user_text, context)
    selected_start, selected_end = _parse_chat_time_range(user_text, context)
    venue_matches = _find_chat_venue_matches(user_text, context)
    bookings = (
        Booking.query.filter_by(user_email=user_email)
        .order_by(Booking.date_iso.asc(), Booking.start_time.asc())
        .limit(3)
        .all()
    )

    lines = [
        f"Hello {display_name},",
        "",
        "Here is the DashDesk chat summary you requested.",
        f"Request: {user_text}",
    ]

    if selected_date:
        lines.append(f"Date context: {_format_display_date(selected_date)}")
    if selected_start and selected_end:
        lines.append(f"Time context: {selected_start} - {selected_end}")
    if venue_matches:
        lines.append("Venue context: " + ", ".join(venue.name for venue in venue_matches[:3]))

    if bookings:
        lines.append("")
        lines.append("Your bookings:")
        for booking in bookings:
            venue_name = booking.venue_package.name if booking.venue_package else "Unknown venue"
            lines.append(
                f"- Booking #{booking.id}: {venue_name} on {_format_display_date(booking.date_iso)} "
                f"from {booking.start_time} to {booking.end_time} ({booking.status})."
            )

    reply_text = (reply_text or "").strip()
    if reply_text:
        lines.extend([
            "",
            "Chat response:",
            reply_text,
        ])

    lines.extend([
        "",
        "Open DashDesk if you want to continue the conversation or update your booking.",
    ])
    subject = f"DashDesk chat summary for {display_name}"
    return subject, "\n".join(lines)


def _log_email_attempt(recipient_email, template_name, subject, body, booking_id=None, status="pending", error_message=None, sent_at=None):
    email_log = EmailLog(
        booking_id=booking_id,
        recipient_email=recipient_email,
        template_name=template_name,
        subject=subject,
        body=body,
        status=status,
        error_message=error_message,
        sent_at=sent_at,
    )
    db.session.add(email_log)
    db.session.commit()
    return email_log


def _send_email_message(recipient_email, template_name, subject, body, booking_id=None):
    if not recipient_email:
        return None

    if not SMTP_HOST:
        return _log_email_attempt(
            recipient_email=recipient_email,
            template_name=template_name,
            subject=subject,
            body=body,
            booking_id=booking_id,
            status="skipped",
            error_message="SMTP host is not configured.",
        )

    pending_log = _log_email_attempt(
        recipient_email=recipient_email,
        template_name=template_name,
        subject=subject,
        body=body,
        booking_id=booking_id,
        status="pending",
    )

    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = recipient_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        if SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as client:
                if SMTP_USERNAME:
                    client.login(SMTP_USERNAME, SMTP_PASSWORD)
                client.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as client:
                if SMTP_USE_TLS:
                    client.starttls(context=ssl.create_default_context())
                if SMTP_USERNAME:
                    client.login(SMTP_USERNAME, SMTP_PASSWORD)
                client.send_message(message)

        pending_log.status = "sent"
        pending_log.error_message = None
        pending_log.sent_at = datetime.now(timezone.utc)
        db.session.commit()
        return pending_log
    except Exception as exc:
        pending_log.status = "failed"
        pending_log.error_message = str(exc)
        db.session.commit()
        app.logger.exception("Failed to send email to %s for %s", recipient_email, template_name)
        return pending_log


def _send_booking_confirmation_email(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking is None or _email_log_exists(booking.id, "booking_confirmation"):
        return None

    subject, body = _build_booking_confirmation_email(booking)
    return _send_email_message(booking.user_email, "booking_confirmation", subject, body, booking_id=booking.id)


def _send_booking_reminder_email(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking is None or _email_log_exists(booking.id, "booking_reminder"):
        return None

    subject, body = _build_booking_reminder_email(booking)
    return _send_email_message(booking.user_email, "booking_reminder", subject, body, booking_id=booking.id)


def _send_chat_follow_up_email(user_text, context, user_email, reply_text):
    if not user_email:
        return None

    subject, body = _build_chat_follow_up_email(user_text, context, user_email, reply_text)
    return _send_email_message(user_email, "chat_follow_up", subject, body)


def _send_due_booking_reminders():
    now = datetime.now()
    reminder_cutoff = now + timedelta(hours=EMAIL_REMINDER_LEAD_HOURS)
    upcoming_bookings = Booking.query.filter(Booking.status == "upcoming").all()
    summary = {"checked": 0, "sent": 0, "skipped": 0, "failed": 0}

    for booking in upcoming_bookings:
        summary["checked"] += 1
        booking_start = _booking_start_datetime(booking)
        if booking_start is None or booking_start < now or booking_start > reminder_cutoff:
            summary["skipped"] += 1
            continue
        if (booking.payment_status or "unpaid").lower() == "paid":
            summary["skipped"] += 1
            continue
        if _email_log_exists(booking.id, "booking_reminder"):
            summary["skipped"] += 1
            continue

        email_log = _send_booking_reminder_email(booking.id)
        if email_log is None:
            summary["skipped"] += 1
        elif email_log.status == "sent":
            summary["sent"] += 1
        elif email_log.status == "skipped":
            summary["skipped"] += 1
        else:
            summary["failed"] += 1

    return summary


def _start_email_reminder_scheduler():
    global _email_reminder_thread_started
    if _email_reminder_thread_started or not SMTP_HOST:
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    def _loop():
        while not _email_reminder_stop_event.wait(EMAIL_REMINDER_INTERVAL_SECONDS):
            try:
                with app.app_context():
                    _send_due_booking_reminders()
            except Exception:
                app.logger.exception("Scheduled email reminder run failed.")

    _email_reminder_thread_started = True
    thread = threading.Thread(target=_loop, name="email-reminder-scheduler", daemon=True)
    thread.start()


def _customer_bookings_summary(email):
    bookings = Booking.query.filter_by(user_email=email).all()
    count = len(bookings)
    total_spend = round(sum(float(booking.total) for booking in bookings), 2)
    latest = max(bookings, key=lambda booking: booking.created_at) if bookings else None
    return count, total_spend, latest


def _group_chat_threads():
    messages = ChatMessage.query.order_by(ChatMessage.created_at.asc()).all()
    users = {user.email: user for user in User.query.all()}
    threads = {}

    for message in messages:
        user = users.get(message.user_email)
        if user is None:
            continue

        thread = threads.setdefault(
            message.user_email,
            {
                "user_email": message.user_email,
                "customer_name": user.full_name,
                "message_count": 0,
                "last_activity": None,
                "last_message": "",
                "last_reply": "",
                "messages": [],
            },
        )
        thread["message_count"] += 1
        thread["last_activity"] = message.created_at
        thread["last_message"] = message.user_message
        thread["last_reply"] = message.bot_reply
        thread["messages"].append(
            {
                "role": "user",
                "text": message.user_message,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
        )
        thread["messages"].append(
            {
                "role": "bot",
                "text": message.bot_reply,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
        )

    ordered_threads = sorted(
        threads.values(),
        key=lambda thread: thread["last_activity"].timestamp() if thread["last_activity"] else 0,
        reverse=True,
    )
    for thread in ordered_threads:
        thread["messages"] = thread["messages"]
        thread["relative_time"] = _format_relative_time(thread["last_activity"])
    return ordered_threads

# Mock database of events for your chatbot to "know" things
def _format_currency(amount):
    try:
        return f"₱ {float(amount or 0):,.2f}"
    except (TypeError, ValueError):
        return "₱ 0.00"


def _format_display_date(date_iso):
    try:
        return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%B %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return date_iso or "-"


def _format_display_time(time_hhmm):
    try:
        return datetime.strptime(time_hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return time_hhmm or "-"


def _format_time_range_display(start_time, end_time):
    return f"{_format_display_time(start_time)} to {_format_display_time(end_time)}"


def _clean_chat_message(message):
    return re.sub(r"\s+", " ", (message or "").strip().lower())


def _normalize_chat_context(context):
    return context if isinstance(context, dict) else {}


def _normalize_chat_date(value):
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        return datetime.strptime(candidate, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _parse_chat_date(message, context=None):
    context = _normalize_chat_context(context)
    text = _clean_chat_message(message)
    today = date_cls.today()

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso_match:
        parsed = _normalize_chat_date(iso_match.group(1))
        if parsed:
            return parsed

    slash_match = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", text)
    if slash_match:
        raw_value = slash_match.group(1)
        for time_format in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw_value, time_format).date().isoformat()
            except ValueError:
                continue

    month_match = re.search(
        r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)\b",
        text,
        re.I,
    )
    if month_match:
        raw_value = month_match.group(1).replace(".", "").replace(",", "")
        for time_format in ("%B %d %Y", "%B %d", "%b %d %Y", "%b %d"):
            try:
                parsed = datetime.strptime(raw_value, time_format)
                if "%Y" not in time_format:
                    parsed = parsed.replace(year=today.year)
                    if parsed.date() < today:
                        parsed = parsed.replace(year=today.year + 1)
                return parsed.date().isoformat()
            except ValueError:
                continue

    if "today" in text:
        return today.isoformat()
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()

    return _normalize_chat_date(context.get("selected_date") or context.get("date"))


def _parse_chat_time_token(raw_value):
    value = re.sub(r"\s+", "", (raw_value or "").lower())
    if not value:
        return None

    meridiem_match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", value)
    if meridiem_match:
        hours = int(meridiem_match.group(1))
        minutes = int(meridiem_match.group(2) or 0)
        meridiem = meridiem_match.group(3)
        if hours == 0 or hours > 12 or minutes > 59:
            return None
        if meridiem == "am":
            hours = 0 if hours == 12 else hours
        else:
            hours = 12 if hours == 12 else hours + 12
        return f"{hours:02d}:{minutes:02d}"

    twenty_four_hour_match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if twenty_four_hour_match:
        hours = int(twenty_four_hour_match.group(1))
        minutes = int(twenty_four_hour_match.group(2))
        if hours > 23 or minutes > 59:
            return None
        return f"{hours:02d}:{minutes:02d}"

    whole_hour_match = re.fullmatch(r"(\d{1,2})(am|pm)", value)
    if whole_hour_match:
        hours = int(whole_hour_match.group(1))
        meridiem = whole_hour_match.group(2)
        if hours == 0 or hours > 12:
            return None
        if meridiem == "am":
            hours = 0 if hours == 12 else hours
        else:
            hours = 12 if hours == 12 else hours + 12
        return f"{hours:02d}:00"

    if value.isdigit():
        hours = int(value)
        if 0 <= hours <= 23:
            return f"{hours:02d}:00"

    return None


def _parse_chat_time_range(message, context=None):
    context = _normalize_chat_context(context)
    text = _clean_chat_message(message)
    range_match = re.search(
        r"\b(?:from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2})\s*(?:-|–|to|until|through|thru)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2})\b",
        text,
        re.I,
    )
    if range_match:
        start_time = _parse_chat_time_token(range_match.group(1))
        end_time = _parse_chat_time_token(range_match.group(2))
        if start_time and end_time:
            return start_time, end_time

    start_time = _parse_chat_time_token(context.get("start_time"))
    end_time = _parse_chat_time_token(context.get("end_time"))
    if start_time and end_time:
        return start_time, end_time

    return None, None


def _venue_search_text(venue):
    features_text = " ".join(venue.features()) if venue else ""
    return " ".join(
        [
            venue.name or "",
            venue.location_label or "",
            features_text,
        ]
    ).lower()


def _find_chat_venue_matches(message, context=None):
    context = _normalize_chat_context(context)
    text = _clean_chat_message(message)
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text).strip()

    venue_id = context.get("venue_id")
    if venue_id not in (None, "") and str(venue_id).isdigit():
        venue = db.session.get(VenuePackage, int(venue_id))
        if venue is not None:
            return [venue]

    context_name = _clean_chat_message(context.get("venue_name"))
    venues = VenuePackage.query.order_by(VenuePackage.price.asc(), VenuePackage.name.asc()).all()
    matches = []

    for venue in venues:
        search_text = _venue_search_text(venue)
        compact_name = re.sub(r"[^a-z0-9]+", " ", (venue.name or "").lower()).strip()
        compact_location = re.sub(r"[^a-z0-9]+", " ", (venue.location_label or "").lower()).strip()

        if context_name and (
            context_name in search_text
            or context_name == compact_name
            or context_name == compact_location
        ):
            matches.append(venue)
            continue

        if venue.name and venue.name.lower() in text:
            matches.append(venue)
            continue

        if venue.location_label and venue.location_label.lower() in text:
            matches.append(venue)
            continue

        if compact_name and compact_name in normalized_text:
            matches.append(venue)
            continue

        if compact_location and compact_location in normalized_text:
            matches.append(venue)
            continue

    return matches


def _booking_conflicts(venue, date_iso, start_time=None, end_time=None):
    query = (
        Booking.query.filter_by(venue_package_id=venue.id, date_iso=date_iso)
        .filter(Booking.status != "cancelled")
        .order_by(Booking.start_time.asc())
    )
    bookings = query.all()
    if not start_time or not end_time:
        return bookings

    requested_start = _time_to_minutes(start_time)
    requested_end = _time_to_minutes(end_time)
    return [
        booking
        for booking in bookings
        if _time_to_minutes(booking.end_time) > requested_start
        and _time_to_minutes(booking.start_time) < requested_end
    ]


def _venue_summary(venue):
    features = ", ".join(venue.features()) or "No listed features"
    return f"{venue.name} ({venue.location_label}) - {_format_currency(venue.price)} | {features}"


def _availability_reply(user_text, context):
    context = _normalize_chat_context(context)
    date_iso = _parse_chat_date(user_text, context)
    start_time, end_time = _parse_chat_time_range(user_text, context)
    checked_venues = _find_chat_venue_matches(user_text, context)
    if not checked_venues:
        checked_venues = VenuePackage.query.order_by(VenuePackage.price.asc(), VenuePackage.name.asc()).all()

    if not date_iso:
        package_names = ", ".join(venue.name for venue in checked_venues[:4]) if checked_venues else "our venue packages"
        return (
            "I can check availability, but I need a date. Try something like 'January 15' or '2026-01-15'. "
            f"Current packages: {package_names}."
        )

    date_label = _format_display_date(date_iso)
    display_start = _format_display_time(start_time) if start_time else None
    display_end = _format_display_time(end_time) if end_time else None
    if not checked_venues:
        return f"I could not find any venue packages in the database for {date_label}."

    if start_time and end_time:
        available = []
        blocked = []
        for venue in checked_venues:
            conflicts = _booking_conflicts(venue, date_iso, start_time, end_time)
            if conflicts:
                blocked.append((venue, conflicts))
            else:
                available.append(venue)

        if len(checked_venues) == 1:
            venue = checked_venues[0]
            if available:
                return (
                    f"Available — {venue.name} is available on {date_label}, from {display_start} to {display_end}. "
                    f"Price: {_format_currency(venue.price)}."
                )

            conflict_text = "; ".join(
                f"Booking #{booking.id} ({_format_time_range_display(booking.start_time, booking.end_time)})"
                for booking in blocked[0][1][:3]
            ) if blocked else "another booking"
            return (
                f"Not available — {venue.name} is already booked on {date_label}, from {display_start} to {display_end}. "
                f"Conflicts: {conflict_text}."
            )

        reply_lines = [f"Availability on {date_label}, from {display_start} to {display_end}:"]
        if available:
            reply_lines.append(
                "Available: " + ", ".join(f"{venue.name} ({_format_currency(venue.price)})" for venue in available[:5])
            )
        if blocked:
            booked_parts = []
            for venue, conflicts in blocked[:5]:
                conflict_ranges = ", ".join(
                    _format_time_range_display(booking.start_time, booking.end_time) for booking in conflicts[:3]
                )
                booked_parts.append(f"{venue.name} ({_format_currency(venue.price)}; {conflict_ranges})")
            reply_lines.append("Not available: " + "; ".join(booked_parts))
        if available and blocked:
            reply_lines.append("Send another time range if you want to check a different slot.")
        return " ".join(reply_lines)

    open_venues = []
    booked_venues = []
    for venue in checked_venues:
        bookings = _booking_conflicts(venue, date_iso)
        if bookings:
            booked_venues.append((venue, bookings))
        else:
            open_venues.append(venue)

    if len(checked_venues) == 1:
        venue = checked_venues[0]
        if open_venues:
            return f"Available — {venue.name} is available on {date_label}. Price: {_format_currency(venue.price)}."

        booking_ranges = ", ".join(
            _format_time_range_display(booking.start_time, booking.end_time) for booking in booked_venues[0][1][:4]
        )
        return (
            f"Not available — {venue.name} is already booked on {date_label}. "
            f"Conflicts: {booking_ranges}. Price: {_format_currency(venue.price)}."
        )

    reply_lines = [f"Availability on {date_label}:"]
    if open_venues:
        reply_lines.append(
            "Available: " + ", ".join(f"{venue.name} ({_format_currency(venue.price)})" for venue in open_venues[:5])
        )
    if booked_venues:
        booked_parts = []
        for venue, bookings in booked_venues[:5]:
            booking_ranges = ", ".join(
                _format_time_range_display(booking.start_time, booking.end_time) for booking in bookings[:3]
            )
            booked_parts.append(f"{venue.name} ({_format_currency(venue.price)}; {booking_ranges})")
        reply_lines.append("Not available: " + "; ".join(booked_parts))
    reply_lines.append("Send a time range if you want me to check a specific slot.")
    return " ".join(reply_lines)


def _package_info_reply(user_text, context):
    matches = _find_chat_venue_matches(user_text, context)
    venues = matches if matches else VenuePackage.query.order_by(VenuePackage.price.asc(), VenuePackage.name.asc()).all()

    if not venues:
        return "There are no venue packages in the database yet."

    if len(venues) == 1:
        return _venue_summary(venues[0])

    return "Current venue packages:\n" + "\n".join(f"- {_venue_summary(venue)}" for venue in venues[:5])


def _booking_status_reply(user_text, context, user_email):
    if not user_email:
        return None

    context = _normalize_chat_context(context)
    date_iso = _parse_chat_date(user_text, context)
    matches = _find_chat_venue_matches(user_text, context)

    query = Booking.query.filter_by(user_email=user_email)
    if date_iso:
        query = query.filter_by(date_iso=date_iso)
    if matches:
        query = query.filter(Booking.venue_package_id.in_([venue.id for venue in matches]))

    bookings = query.order_by(Booking.date_iso.asc(), Booking.start_time.asc()).all()
    if not bookings:
        if date_iso:
            return f"I could not find any bookings for {user_email} on {_format_display_date(date_iso)}."
        return f"I could not find any bookings for {user_email}."

    lower_text = _clean_chat_message(user_text)
    if "next" in lower_text or "upcoming" in lower_text:
        booking = next((item for item in bookings if item.status != "cancelled"), bookings[0])
        venue_name = booking.venue_package.name if booking.venue_package else "Unknown venue"
        return (
            f"Your next booking is #{booking.id} for {venue_name} on {_format_display_date(booking.date_iso)} "
            f"from {booking.start_time} to {booking.end_time}. Status: {booking.status}."
        )

    lines = [f"I found {len(bookings)} booking(s) for {user_email}:"]
    for booking in bookings[:5]:
        venue_name = booking.venue_package.name if booking.venue_package else "Unknown venue"
        lines.append(
            f"- Booking #{booking.id}: {venue_name} on {_format_display_date(booking.date_iso)} "
            f"from {_format_time_range_display(booking.start_time, booking.end_time)} ({booking.status})."
        )
    return "\n".join(lines)


def _is_simple_greeting(normalized_text):
    tokens = normalized_text.split()
    greetings = ("hello", "hi", "hey", "good morning", "good afternoon", "good evening")
    if not tokens or len(tokens) > 3:
        return False
    return any(term in normalized_text for term in greetings)


def _is_billing_request(normalized_text):
    billing_terms = ("billing", "tax", "total", "payment", "invoice", "receipt", "charge", "cost")
    return any(term in normalized_text for term in billing_terms)


def _is_support_request(normalized_text):
    support_terms = ("support", "help", "contact", "assist", "reschedule", "change booking", "change date", "cancel")
    return any(term in normalized_text for term in support_terms)


def _greeting_reply():
    return "Hello. I can help with venue availability, package details, billing, and booking status. What would you like to check?"


def _support_reply(user_text, context, user_email):
    context = _normalize_chat_context(context)
    venue_matches = _find_chat_venue_matches(user_text, context)
    venue_name = venue_matches[0].name if venue_matches else (context.get("venue_name") or "").strip()

    lines = [
        "I can help with venue availability, package details, booking status, and billing.",
    ]
    if venue_name:
        lines.append(f"For {venue_name}, send the date and time you want to review and I will check it for you.")
    else:
        lines.append("If you want to reschedule a booking, open My Bookings and select the booking you want to change.")
    lines.append("If you need human follow-up, leave the request here and the support team can review it in Active Chats.")
    return " ".join(lines)


def _billing_reply(user_text, context):
    context = _normalize_chat_context(context)
    venue_matches = _find_chat_venue_matches(user_text, context)
    if not venue_matches:
        return "Billing is subtotal plus 12% tax. Select a venue package and I can calculate the exact total for you."

    venue = venue_matches[0]
    subtotal = float(venue.price or 0)
    tax = round(subtotal * 0.12, 2)
    total = round(subtotal + tax, 2)
    return (
        f"Billing for {venue.name}: subtotal {_format_currency(subtotal)}, tax {_format_currency(tax)} (12%), "
        f"total {_format_currency(total)}."
    )


def _build_chat_database_context(user_text, context, user_email):
    context = _normalize_chat_context(context)
    venues = VenuePackage.query.order_by(VenuePackage.price.asc(), VenuePackage.name.asc()).all()
    lines = ["Venue packages:"]
    for venue in venues:
        lines.append(f"- {_venue_summary(venue)}")

    selected_venues = _find_chat_venue_matches(user_text, context)
    if selected_venues:
        lines.append("Selected venue context:")
        for venue in selected_venues[:3]:
            lines.append(f"- {_venue_summary(venue)}")

    selected_date = _parse_chat_date(user_text, context)
    selected_start, selected_end = _parse_chat_time_range(user_text, context)
    if selected_date:
        lines.append(f"Selected date: {_format_display_date(selected_date)}")
    if selected_start and selected_end:
        lines.append(f"Selected time: {_format_time_range_display(selected_start, selected_end)}")

    if user_email:
        bookings = (
            Booking.query.filter_by(user_email=user_email)
            .order_by(Booking.date_iso.asc(), Booking.start_time.asc())
            .limit(5)
            .all()
        )
        if bookings:
            lines.append("User bookings:")
            for booking in bookings:
                venue_name = booking.venue_package.name if booking.venue_package else "Unknown venue"
                lines.append(
                    f"- Booking #{booking.id}: {venue_name} on {_format_display_date(booking.date_iso)} "
                    f"from {booking.start_time} to {booking.end_time} ({booking.status})."
                )

    return "\n".join(lines)


def _database_chat_reply(user_text, context, user_email):
    normalized_text = _clean_chat_message(user_text)
    if not normalized_text:
        return None

    if _is_simple_greeting(normalized_text):
        return _greeting_reply()

    if _is_billing_request(normalized_text):
        return _billing_reply(user_text, context)

    if _is_support_request(normalized_text):
        return _support_reply(user_text, context, user_email)

    availability_terms = ("availability", "available", "booked", "open", "free", "slot")
    package_terms = ("price", "pricing", "package", "packages", "venue", "hall", "feature")
    booking_terms = ("my booking", "my bookings", "upcoming", "reservation", "status", "schedule", "next booking")

    if any(term in normalized_text for term in availability_terms):
        return _availability_reply(user_text, context)

    if any(term in normalized_text for term in booking_terms):
        booking_reply = _booking_status_reply(user_text, context, user_email)
        if booking_reply:
            return booking_reply

    if any(term in normalized_text for term in package_terms):
        return _package_info_reply(user_text, context)

    return None


def _fallback_chat_reply(user_text):
    normalized_text = _clean_chat_message(user_text)
    if not normalized_text:
        return "Send a message and I can help with venue availability, package details, billing, or your bookings."

    if any(term in normalized_text for term in ("hello", "hi", "hey")):
        return "Hello. I can help with venue availability, package details, billing, and your bookings."

    return "I can help with venue availability, package details, billing, and your bookings. Ask me about a venue name, a date, or your next reservation."


def _append_chat_email_status(reply_text, email_log, user_email):
    if not user_email:
        return f"{reply_text} Please log in if you want me to email this summary to your Gmail inbox."

    if email_log is None:
        return f"{reply_text} I could not queue the email summary."

    if email_log.status == "sent":
        return f"{reply_text} I emailed a copy to {user_email}."
    if email_log.status == "skipped":
        return f"{reply_text} I could not send the email because SMTP is not configured yet."
    return f"{reply_text} I tried to send the email, but Gmail delivery failed."


def _store_chat_message(user_text, reply):
    chat_owner = _chat_owner_id()
    db.session.add(
        ChatMessage(
            user_email=chat_owner,
            user_message=user_text,
            bot_reply=reply,
        )
    )
    db.session.commit()


@app.route('/chat', methods=['POST'])
def chat_endpoint():
    data = request.get_json(silent=True) or {}
    user_text = str(data.get("message", "")).strip()
    chat_context = _normalize_chat_context(data.get("context"))
    user_email = session.get("authenticated_user")
    normalized_text = _clean_chat_message(user_text)

    if not user_text:
        return jsonify({"status": "error", "message": "Message is required."}), 400

    reply = None
    db_reply = _database_chat_reply(user_text, chat_context, user_email)
    if db_reply:
        reply = db_reply

    if reply is None and OPENAI_API_KEY:
        try:
            db_context = _build_chat_database_context(user_text, chat_context, user_email)
            payload = {
                "model": OPENAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the DashDesk Booking Support Bot. Use the provided database context to answer about venue packages, "
                            "availability, pricing, and bookings. Prioritize the selected date in the context when the user refers to that day. "
                            "Always format times in 12-hour AM/PM notation and state prices in PHP. Do not invent details that are not in the context."
                        ),
                    },
                    {
                        "role": "system",
                        "content": f"Database context:\n{db_context}",
                    },
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.4,
            }
            req = urlrequest.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                },
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            openai_reply = (
                resp_data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if openai_reply:
                reply = openai_reply
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
            pass

    if reply is None:
        reply = _fallback_chat_reply(user_text)

    email_log = None
    if _is_email_request(normalized_text):
        email_log = _send_chat_follow_up_email(user_text, chat_context, user_email, reply)
        reply = _append_chat_email_status(reply, email_log, user_email)

    _store_chat_message(user_text, reply)
    response = {"status": "success", "reply": reply}
    if email_log is not None:
        response["email_status"] = email_log.status
    return jsonify(response)


@app.route("/api/email/send", methods=["POST"])
def api_send_email():
    if not _require_login():
        return jsonify({"status": "error", "message": "Login required."}), 401

    payload = _request_payload()
    subject = (payload.get("subject") or payload.get("title") or "").strip() or "DashDesk email"
    body = (payload.get("body") or payload.get("message") or payload.get("content") or "").strip()
    template_name = (payload.get("template_name") or "manual_email").strip() or "manual_email"

    if not body:
        return jsonify({"status": "error", "message": "Email body is required."}), 400

    recipient_email = session.get("authenticated_user")
    requested_recipient = (payload.get("recipient_email") or "").strip().lower()
    if _require_admin() and requested_recipient:
        recipient_email = requested_recipient

    email_log = _send_email_message(recipient_email, template_name, subject, body)
    if email_log is None:
        return jsonify({"status": "error", "message": "Unable to create email log."}), 400

    return jsonify({"status": "success", "email_log": email_log.to_dict()})


@app.route("/api/venues")
def api_venues():
    venues = VenuePackage.query.order_by(VenuePackage.price.asc()).all()
    return jsonify({"status": "success", "venues": [v.to_dict() for v in venues]})


@app.route("/api/admin/dashboard-summary")
def api_admin_dashboard_summary():
    if not _require_admin():
        return _admin_json_forbidden()

    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    recent_bookings = [booking.to_dict() for booking in bookings[:5]]
    threads = _group_chat_threads()

    return jsonify(
        {
            "status": "success",
            "summary": {
                "customers": User.query.count(),
                "venues": VenuePackage.query.count(),
                "bookings": len(bookings),
                "upcoming_bookings": sum(1 for booking in bookings if _booking_status_label(booking) == "upcoming"),
                "revenue": _booking_amount_total(),
                "active_chats": len(threads),
                "recent_bookings": recent_bookings,
                "recent_chats": threads[:5],
            },
        }
    )


@app.route("/api/admin/bookings")
def api_admin_bookings():
    if not _require_admin():
        return _admin_json_forbidden()

    users = {user.email: user for user in User.query.all()}
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    payload = []

    for booking in bookings:
        user = users.get(booking.user_email)
        payload.append(
            {
                **booking.to_dict(),
                "status": _booking_status_label(booking),
                "customer_name": user.full_name if user else booking.user_email,
                "customer_email": booking.user_email,
            }
        )

    return jsonify({"status": "success", "bookings": payload})


@app.route("/api/admin/bookings/<int:booking_id>/payment-status", methods=["PUT"])
def api_admin_booking_payment_status(booking_id):
    if not _require_admin():
        return _admin_json_forbidden()

    booking = db.session.get(Booking, booking_id)
    if booking is None:
        return jsonify({"status": "error", "message": "Booking not found."}), 404

    payload = _request_payload()
    payment_status = (payload.get("payment_status") or "").strip().lower()
    if payment_status not in {"paid", "unpaid"}:
        return jsonify({"status": "error", "message": "payment_status must be 'paid' or 'unpaid'."}), 400

    booking.payment_status = payment_status
    db.session.commit()
    return jsonify({"status": "success", "booking": booking.to_dict()})


@app.route("/api/admin/customers")
def api_admin_customers():
    if not _require_admin():
        return _admin_json_forbidden()

    customers = []
    for user in User.query.order_by(User.created_at.desc()).all():
        booking_count, total_spend, latest_booking = _customer_bookings_summary(user.email)
        customers.append(
            {
                **user.to_dict(),
                "booking_count": booking_count,
                "total_spend": total_spend,
                "latest_booking": latest_booking.to_dict() if latest_booking else None,
            }
        )

    return jsonify({"status": "success", "customers": customers})


@app.route("/api/admin/activechats")
def api_admin_activechats():
    if not _require_admin():
        return _admin_json_forbidden()

    return jsonify({"status": "success", "threads": _group_chat_threads()})


@app.route("/api/admin/email-health")
def api_admin_email_health():
    if not _require_admin():
        return _admin_json_forbidden()

    return jsonify(
        {
            "status": "success",
            "email": {
                "smtp_configured": bool(SMTP_HOST),
                "smtp_host": SMTP_HOST,
                "smtp_port": SMTP_PORT,
                "use_tls": SMTP_USE_TLS,
                "use_ssl": SMTP_USE_SSL,
                "mail_from": MAIL_FROM,
                "reminder_interval_seconds": EMAIL_REMINDER_INTERVAL_SECONDS,
                "reminder_lead_hours": EMAIL_REMINDER_LEAD_HOURS,
            },
        }
    )


@app.route("/api/admin/email-logs")
def api_admin_email_logs():
    if not _require_admin():
        return _admin_json_forbidden()

    logs = EmailLog.query.order_by(EmailLog.created_at.desc()).limit(100).all()
    return jsonify({"status": "success", "logs": [log.to_dict() for log in logs]})


@app.route("/api/admin/email-reminders/run", methods=["POST"])
def api_admin_run_email_reminders():
    if not _require_admin():
        return _admin_json_forbidden()

    summary = _send_due_booking_reminders()
    return jsonify({"status": "success", "summary": summary})


@app.route("/admin/venues")
def admin_venues():
    if not _require_admin():
        return "Forbidden", 403
    return render_template("admin/venues.html")


@app.route("/api/admin/venues", methods=["GET", "POST"])
def api_admin_venues():
    if not _require_admin():
        return _admin_json_forbidden()

    if request.method == "GET":
        venues = VenuePackage.query.order_by(VenuePackage.price.asc(), VenuePackage.name.asc()).all()
        return jsonify({"status": "success", "venues": [venue.to_dict() for venue in venues]})

    payload, error_response = _venue_payload_or_error()
    if error_response:
        return error_response

    if VenuePackage.query.filter_by(name=payload["name"]).first():
        return jsonify({"status": "error", "message": "A venue package with that name already exists."}), 409

    venue = VenuePackage(**payload)
    db.session.add(venue)
    db.session.commit()
    return jsonify({"status": "success", "venue": venue.to_dict()}), 201


@app.route("/api/admin/venues/<int:venue_id>", methods=["PUT", "DELETE"])
def api_admin_venue_detail(venue_id):
    if not _require_admin():
        return _admin_json_forbidden()

    venue = db.session.get(VenuePackage, venue_id)
    if venue is None:
        return jsonify({"status": "error", "message": "Venue package not found."}), 404

    if request.method == "DELETE":
        if Booking.query.filter_by(venue_package_id=venue.id).first():
            return jsonify({"status": "error", "message": "This venue package cannot be deleted because it is used by existing bookings."}), 409
        db.session.delete(venue)
        db.session.commit()
        return jsonify({"status": "success", "message": "Venue package deleted."})

    payload, error_response = _venue_payload_or_error(existing_venue=venue)
    if error_response:
        return error_response

    duplicate = VenuePackage.query.filter(VenuePackage.name == payload["name"], VenuePackage.id != venue.id).first()
    if duplicate:
        return jsonify({"status": "error", "message": "A venue package with that name already exists."}), 409

    venue.name = payload["name"]
    venue.location_label = payload["location_label"]
    venue.image_url = payload["image_url"]
    venue.price = payload["price"]
    venue.features_json = payload["features_json"]
    db.session.commit()
    return jsonify({"status": "success", "venue": venue.to_dict()})


@app.route("/api/bookings")
def api_bookings():
    if not _require_login():
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    user_email = session.get("authenticated_user")
    bookings = (
        Booking.query.filter_by(user_email=user_email)
        .order_by(Booking.created_at.desc())
        .limit(25)
        .all()
    )

    # Lightweight status normalization (in case old rows exist)
    today = date_cls.today().isoformat()
    result = []
    for booking in bookings:
        if booking.status not in {"upcoming", "completed", "cancelled"}:
            booking.status = "completed" if booking.date_iso < today else "upcoming"
        result.append(booking.to_dict())

    return jsonify({"status": "success", "bookings": result})


@app.route("/api/bookings/<int:booking_id>")
def api_booking_detail(booking_id):
    if not _require_login():
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    user_email = session.get("authenticated_user")
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_email != user_email:
        return jsonify({"status": "error", "message": "Booking not found."}), 404

    return jsonify({"status": "success", "booking": booking.to_dict()})


@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
def api_booking_cancel(booking_id):
    if not _require_login():
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    user_email = session.get("authenticated_user")
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_email != user_email:
        return jsonify({"status": "error", "message": "Booking not found."}), 404

    if booking.status == "cancelled":
        return jsonify({"status": "success", "booking": booking.to_dict(), "message": "Booking is already cancelled."})

    if booking.status != "upcoming":
        return jsonify({"status": "error", "message": "Only upcoming bookings can be cancelled."}), 400

    booking.status = "cancelled"
    db.session.commit()
    return jsonify({"status": "success", "booking": booking.to_dict()})


@app.route("/")
def homepage():
    return render_template("customer/homepage.html")


@app.route("/login", methods=["GET", "POST"])
@app.route('/login/index', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please enter both email and password.", "error")
            return redirect(url_for("login"))

        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))

        session["authenticated_user"] = user.email
        flash("Login successful.", "success")
        return redirect(url_for("booking_page"))

    return render_template('login/index.html')


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not full_name or not email or not password:
            flash("Please fill out all required fields.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return redirect(url_for("register"))

        user = User(
            full_name=full_name,
            email=email,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        session["authenticated_user"] = user.email
        flash("Registration successful. You are now logged in.", "success")
        return redirect(url_for("booking_page"))

    return render_template("login/register.html")


@app.route("/login/alt")
def login_alt():
    return redirect(url_for("login"))


@app.route("/login/customer")
def login_customer():
    return redirect(url_for("login"))


@app.route("/admin/dashboard")
def admin_dashboard():
    if not _require_admin():
        return "Forbidden", 403
    return render_template("admin/dashboard.html")


@app.route("/admin")
def admin_root():
    if _require_admin():
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("login"))


@app.route("/admin/activechats")
def admin_activechats():
    if not _require_admin():
        return "Forbidden", 403
    return render_template("admin/activechats.html")


@app.route("/admin/booking")
def admin_booking():
    if not _require_admin():
        return "Forbidden", 403
    return render_template("admin/booking.html")


@app.route("/admin/customer")
def admin_customer():
    if not _require_admin():
        return "Forbidden", 403
    return render_template("admin/customer.html")


@app.route("/admin/unlock")
def admin_unlock():
    key = (request.args.get("key") or "").strip()
    if not ADMIN_KEY:
        return "Admin key not configured", 500
    if key != ADMIN_KEY:
        return "Forbidden", 403
    session["is_admin"] = True
    return redirect(url_for("admin_dashboard"))

@app.route('/book')
@app.route('/customer/booking')
def booking_page():
    if not _require_login():
        return redirect(url_for("login"))
    return render_template('customer/booking_customer.html', initial_booking_id=request.args.get("booking_id", "").strip())


@app.route('/booking/invoice/<int:booking_id>')
def booking_invoice(booking_id):
    if not _require_login():
        return redirect(url_for("login"))

    user_email = session.get("authenticated_user")
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_email != user_email:
        return "Booking not found.", 404

    venue = booking.venue_package
    return render_template(
        'customer/booking_invoice.html',
        booking=booking,
        date_label=_format_display_date(booking.date_iso),
        time_label=_format_time_range_display(booking.start_time, booking.end_time),
        venue_name=venue.name if venue else "Unknown venue",
        venue_location=venue.location_label if venue else "-",
        subtotal_label=_format_currency(booking.subtotal),
        tax_label=_format_currency(booking.tax),
        total_label=_format_currency(booking.total),
        payment_label=(booking.payment_status or 'unpaid').replace('_', ' ').title(),
        status_label=(booking.status or 'upcoming').replace('_', ' ').title(),
    )


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route('/booking/confirm', methods=['POST'])
def confirm_booking():
    if not _require_login():
        return redirect(url_for("login"))

    existing_booking_id = (request.form.get("booking_id") or "").strip()
    if existing_booking_id.isdigit():
        existing_booking = db.session.get(Booking, int(existing_booking_id))
        if existing_booking is not None:
            if existing_booking.user_email != session.get("authenticated_user"):
                flash("You can only open your own booking.", "error")
                return redirect(url_for("booking_page"))
            return redirect(url_for("booking_page", booking_id=existing_booking.id))

    venue_raw = (request.form.get("venue") or "").strip()
    date_raw = (request.form.get("date") or "").strip()
    time_raw = (request.form.get("time") or "").strip()

    if not venue_raw:
        flash("Please select a venue package before checkout.", "error")
        return redirect(url_for("booking_page"))

    # Support either venue id (preferred) or venue name (fallback)
    venue_obj = None
    if venue_raw.isdigit():
        venue_obj = VenuePackage.query.get(int(venue_raw))
    if venue_obj is None:
        venue_obj = VenuePackage.query.filter_by(name=venue_raw).first()
    if venue_obj is None:
        flash("Selected venue package was not found.", "error")
        return redirect(url_for("booking_page"))

    try:
        datetime.strptime(date_raw, "%Y-%m-%d")
    except ValueError:
        flash("Please select a valid date.", "error")
        return redirect(url_for("booking_page"))

    start_str = ""
    end_str = ""
    if "-" in time_raw:
        start_str, end_str = [p.strip() for p in time_raw.split("-", 1)]
    else:
        start_str = (request.form.get("start_time") or "").strip()
        end_str = (request.form.get("end_time") or "").strip()

    start_time = _parse_time_hhmm(start_str)
    end_time = _parse_time_hhmm(end_str)
    if not start_time or not end_time:
        flash("Please select valid start and end times.", "error")
        return redirect(url_for("booking_page"))
    if _time_to_minutes(end_time) <= _time_to_minutes(start_time):
        flash("End time must be after start time.", "error")
        return redirect(url_for("booking_page"))

    subtotal = float(venue_obj.price)
    tax = round(subtotal * 0.12, 2)
    total = round(subtotal + tax, 2)

    booking = Booking(
        user_email=session.get("authenticated_user"),
        venue_package_id=venue_obj.id,
        date_iso=date_raw,
        start_time=start_time,
        end_time=end_time,
        subtotal=subtotal,
        tax=tax,
        total=total,
        payment_status="unpaid",
        status="upcoming",
    )
    db.session.add(booking)
    db.session.commit()
    email_log = _send_booking_confirmation_email(booking.id)

    flash(f"Booking confirmed for {venue_obj.name}.", "success")
    if email_log is None:
        flash("Confirmation email was not queued.", "error")
    elif email_log.status == "sent":
        flash("Confirmation email sent to the user.", "success")
    elif email_log.status == "skipped":
        flash("Confirmation email was not sent because SMTP is not configured on this server.", "error")
    else:
        flash("Confirmation email failed to send. Check the email settings in the admin panel.", "error")
    return redirect(url_for("booking_page"))

if __name__ == '__main__':
    # Run the server
    app.run(debug=True, port=5000)