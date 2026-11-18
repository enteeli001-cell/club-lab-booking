"""Shared 6-digit e-mail verification codes (material purchases and profile e-mail binding).

Codes are never stored: only an HMAC-SHA256 digest bound to a purpose and a record id.
Validity 5 minutes, at most 5 wrong attempts per code, resend cooldown 60 s, 5 sends per record.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app

from app.utils import local_now

CODE_LENGTH = 6
CODE_TTL_SECONDS = 5 * 60
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_COOLDOWN_SECONDS = 60
CODE_MAX_SENDS = 5
CODE_RE = re.compile(r"^[0-9]{6}$")


def generate_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def code_digest(purpose: str, record_id: int, code: str, extra: str = "") -> str:
    key = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    message = f"{purpose}:{record_id}:{extra}:{code}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def digest_matches(stored: str | None, purpose: str, record_id: int, code: str, extra: str = "") -> bool:
    if not stored or not CODE_RE.fullmatch(code or ""):
        return False
    return hmac.compare_digest(stored, code_digest(purpose, record_id, code, extra))


def expires_from_now() -> datetime:
    return local_now() + timedelta(seconds=CODE_TTL_SECONDS)


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is None or local_now() >= expires_at


def seconds_left(expires_at: datetime | None) -> int:
    if expires_at is None:
        return 0
    return max(0, int((expires_at - local_now()).total_seconds()))


def resend_wait(sent_at: datetime | None) -> int:
    if sent_at is None:
        return 0
    return max(0, int(CODE_RESEND_COOLDOWN_SECONDS - (local_now() - sent_at).total_seconds()))


def delivery_allowed(user_id: int) -> bool:
    """Persistent per-user hourly limit across purposes and server workers."""
    from sqlalchemy import func
    from app import db
    from app.models import EmailVerification, MaterialPurchase

    cutoff = local_now() - timedelta(hours=1)
    profile_sends = db.session.query(func.coalesce(func.sum(EmailVerification.send_count), 0)).filter(
        EmailVerification.user_id == user_id, EmailVerification.sent_at >= cutoff).scalar()
    purchase_sends = db.session.query(func.coalesce(func.sum(MaterialPurchase.code_send_count), 0)).filter(
        MaterialPurchase.user_id == user_id, MaterialPurchase.code_sent_at >= cutoff).scalar()
    return int(profile_sends) + int(purchase_sends) < 10


def mask_email(address: str | None) -> str:
    address = (address or "").strip()
    if "@" not in address:
        return ""
    local, _, domain = address.partition("@")
    shown = local[:2] if len(local) > 2 else local[:1]
    return f"{shown}***@{domain}"
