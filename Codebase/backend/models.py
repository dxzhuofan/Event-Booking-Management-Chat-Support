import json
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "full_name": self.full_name,
            "email": self.email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class VenuePackage(db.Model):
    __tablename__ = "venue_packages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    location_label = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    image_url = db.Column(db.String(255), nullable=True)
    features_json = db.Column(db.Text, nullable=False, default="[]")

    def features(self):
        try:
            return json.loads(self.features_json or "[]")
        except Exception:
            return []

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "location_label": self.location_label,
            "price": float(self.price),
            "image_url": self.image_url,
            "features": self.features(),
        }


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(254), nullable=False, index=True)

    venue_package_id = db.Column(db.Integer, db.ForeignKey("venue_packages.id"), nullable=False)
    venue_package = db.relationship("VenuePackage")

    date_iso = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    start_time = db.Column(db.String(5), nullable=False)  # HH:MM
    end_time = db.Column(db.String(5), nullable=False)  # HH:MM

    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    tax = db.Column(db.Numeric(10, 2), nullable=False)
    total = db.Column(db.Numeric(10, 2), nullable=False)

    payment_status = db.Column(db.String(16), nullable=False, default="unpaid")
    status = db.Column(db.String(16), nullable=False, default="upcoming")
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "user_email": self.user_email,
            "venue_package": self.venue_package.to_dict() if self.venue_package else None,
            "venue_package_id": self.venue_package_id,
            "date": self.date_iso,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "time_range": f"{self.start_time} - {self.end_time}",
            "subtotal": float(self.subtotal),
            "tax": float(self.tax),
            "total": float(self.total),
            "payment_status": self.payment_status,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class EmailLog(db.Model):
    __tablename__ = "email_logs"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True, index=True)
    booking = db.relationship("Booking")
    recipient_email = db.Column(db.String(254), nullable=False, index=True)
    template_name = db.Column(db.String(80), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    sent_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "booking_id": self.booking_id,
            "recipient_email": self.recipient_email,
            "template_name": self.template_name,
            "subject": self.subject,
            "body": self.body,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(254), nullable=False, index=True)
    user_message = db.Column(db.Text, nullable=False)
    bot_reply = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "user_email": self.user_email,
            "user_message": self.user_message,
            "bot_reply": self.bot_reply,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
