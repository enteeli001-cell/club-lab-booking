"""Login/audit logging, device cookies, UA parsing, and IP/device blacklist.

Never record passwords, tokens, or authorization codes in logs or payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any

from flask import Request, current_app, has_app_context, has_request_context, request
from flask_login import current_user

from app import db
from app.utils import local_now

DEVICE_COOKIE = "lab_device_id"
SECRET_KEY_RE = re.compile(
    r"(password|passwd|pwd|token|secret|auth_code|authorization|api_key|csrf_token|SMTP_AUTH|^code$|verification_code|验证码)",
    re.I,
)

_OS_RULES = (
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
    ("CrOS", "Chrome OS"),
)
_BROWSER_RULES = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Safari/", "Safari"),
    ("Playwright", "Playwright"),
    ("Python", "Python"),
)


def _normalize_ip(value: str) -> str:
    text = (value or "").strip().strip("[]")
    if text.startswith("::ffff:"):
        text = text.rsplit(":", 1)[-1]
    return text


def is_trusted_proxy(remote_addr: str | None) -> bool:
    """Only the local Cloudflare tunnel (or local loopback) may supply forwarded headers."""
    return _normalize_ip(remote_addr or "") in {"127.0.0.1", "::1", "localhost"}


def _looks_like_ip(value: str) -> bool:
    text = _normalize_ip(value)
    if not text or text.lower() in {"unknown", "null", "-"}:
        return False
    # IPv4
    parts = text.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    # IPv6 (very loose: has colons and hex chars)
    if ":" in text and all(c in "0123456789abcdefABCDEF:" for c in text):
        return True
    return False


def request_is_https(req: Request | None = None) -> bool:
    req = req or (request if has_request_context() else None)
    if req is None:
        return False
    if bool(req.is_secure):
        return True
    if not is_trusted_proxy(req.remote_addr):
        return False
    proto = (req.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    visitor = (req.headers.get("CF-Visitor") or "").lower()
    if '"https"' in visitor or "'https'" in visitor:
        return True
    return False


def client_ip(req: Request | None = None) -> str:
    """Return the real client IP, trusting proxy headers only from the local tunnel.

    When the request comes from 127.0.0.1/::1 (cloudflared), prefer
    CF-Connecting-IP, then the first X-Forwarded-For hop. Otherwise use
    REMOTE_ADDR and ignore spoofed forwarded headers.
    """
    req = req or (request if has_request_context() else None)
    if req is None:
        return ""
    remote = _normalize_ip(req.remote_addr or "")
    if is_trusted_proxy(remote):
        cf = (req.headers.get("CF-Connecting-IP") or "").split(",")[0].strip()
        if _looks_like_ip(cf):
            return _normalize_ip(cf)
        forwarded = (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if _looks_like_ip(forwarded):
            return _normalize_ip(forwarded)
    return remote


def parse_user_agent(ua: str | None) -> tuple[str, str]:
    ua = ua or ""
    os_name = "未知系统"
    for needle, label in _OS_RULES:
        if needle in ua:
            os_name = label
            break
    browser = "未知浏览器"
    for needle, label in _BROWSER_RULES:
        if needle in ua:
            browser = label
            break
    return browser, os_name


def ua_fingerprint(ua: str | None) -> str:
    text = (ua or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def current_user_agent() -> str:
    if not has_request_context():
        return ""
    return (request.headers.get("User-Agent") or "")[:300]


def current_device_id() -> str:
    if not has_request_context():
        return ""
    return (request.cookies.get(DEVICE_COOKIE) or "").strip()


def new_device_id() -> str:
    return secrets.token_hex(16)


def ensure_device_cookie(response):
    """Set a durable device id cookie when the browser has not sent one."""
    if not has_request_context():
        return response
    if request.cookies.get(DEVICE_COOKIE):
        return response
    if request.path.startswith("/static/"):
        return response
    response.set_cookie(
        DEVICE_COOKIE,
        new_device_id(),
        max_age=365 * 24 * 3600,
        path="/",
        samesite="Lax",
        httponly=False,
        secure=request_is_https(),
    )
    return response


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if SECRET_KEY_RE.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # never keep obvious bearer tokens if someone passed a raw string
        if value.lower().startswith("bearer "):
            return "[redacted]"
        if has_app_context():
            for env_key in ("SMTP_AUTH_CODE", "SUPER_ADMIN_NEW_PASSWORD"):
                secret = None
                try:
                    import os
                    secret = os.environ.get(env_key)
                except Exception:
                    secret = None
                if secret and secret in value:
                    value = value.replace(secret, "[redacted]")
            token = current_app.config.get("API_TOKEN")
            if token and token in value:
                value = value.replace(token, "[redacted]")
        return value
    return value


def redact_url(value: str | None) -> str | None:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    if not value:
        return None
    try:
        parts = urlsplit(value)
        query = urlencode([(key, "[redacted]" if SECRET_KEY_RE.search(key) else redact(val))
                           for key, val in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))[:300]
    except ValueError:
        return None


def _payload_text(payload: Any) -> str | None:
    if payload is None:
        return None
    try:
        return json.dumps(redact(payload), ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps({"_error": "unserializable"}, ensure_ascii=False)


def load_payload(log) -> dict:
    if not log or not log.payload:
        return {}
    try:
        data = json.loads(log.payload)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_login(*, user=None, student_id: str | None = None, success: bool, reason: str | None = None) -> None:
    from app.models import LoginLog

    ua = current_user_agent()
    browser, os_name = parse_user_agent(ua)
    log = LoginLog(
        created_at=local_now(),
        user_id=getattr(user, "id", None),
        student_id=(student_id or getattr(user, "student_id", None) or "")[:32] or None,
        role=getattr(user, "role", None),
        ip=client_ip() or None,
        user_agent=ua or None,
        device_id=current_device_id() or None,
        ua_fingerprint=ua_fingerprint(ua) or None,
        browser=browser,
        os_name=os_name,
        success=bool(success),
        reason=(reason or None) and reason[:120],
    )
    db.session.add(log)
    db.session.commit()


def record_audit(
    action: str,
    *,
    summary: str,
    target_type: str | None = None,
    target_id: int | None = None,
    payload: Any = None,
    reversible: bool = False,
    undo_blocked_reason: str | None = None,
    actor=None,
) -> None:
    from app.models import AuditLog

    if actor is None and has_request_context() and current_user.is_authenticated:
        actor = current_user
    ua = current_user_agent()
    log = AuditLog(
        created_at=local_now(),
        actor_id=getattr(actor, "id", None),
        actor_student_id=getattr(actor, "student_id", None),
        actor_role=getattr(actor, "role", None),
        ip=client_ip() or None,
        device_id=current_device_id() or None,
        user_agent=ua or None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=(summary or "")[:400],
        payload=_payload_text(payload),
        reversible=bool(reversible) and not undo_blocked_reason,
        undo_blocked_reason=(undo_blocked_reason or None) and undo_blocked_reason[:200],
    )
    db.session.add(log)
    db.session.commit()


def active_blacklist_hits(ip: str | None = None, device_id: str | None = None):
    from app.models import Blacklist

    ip = (ip if ip is not None else client_ip()) or ""
    device_id = (device_id if device_id is not None else current_device_id()) or ""
    from sqlalchemy import and_, or_

    now = local_now()
    conditions = []
    if ip:
        conditions.append(and_(Blacklist.kind == "ip", Blacklist.value == ip))
    if device_id:
        conditions.append(and_(Blacklist.kind == "device", Blacklist.value == device_id))
    if not conditions:
        return []
    # One query per request (was one per kind); IP hits stay ahead of device hits.
    rows = Blacklist.query.filter(Blacklist.active.is_(True), or_(*conditions)).order_by(Blacklist.id.asc()).all()
    rows.sort(key=lambda row: 0 if row.kind == "ip" else 1)
    return [row for row in rows if row.is_in_effect(now)]


def is_blacklisted() -> bool:
    return bool(active_blacklist_hits())


def blacklist_exempt_path(path: str) -> bool:
    if path.startswith("/static/"):
        return True
    if path in {"/health", "/favicon.ico"}:
        return True
    return False
