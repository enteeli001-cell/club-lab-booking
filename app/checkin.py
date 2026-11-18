"""Booking check-in helpers: fixed per-lab QR token, windows, states and QR images."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from io import BytesIO

from flask import current_app, has_request_context, request

DEFAULT_EARLY_MINUTES = 15

STATE_LABELS = {
    "early": "未到签到时间",
    "open": "可签到",
    "done": "已签到",
    "expired": "已过期未签到",
}
# Reuse existing badge colours: warn / ok / grey / red.
STATE_BADGES = {
    "early": "off",
    "open": "pending",
    "done": "approved",
    "expired": "rejected",
}


GEO_MAX_ACCURACY_M = 100.0   # worse than this: ask the user to retry somewhere with better signal
GEO_TOLERANCE_CAP_M = 50.0   # accuracy only buys at most this much slack
GEO_DEFAULT_RADIUS_M = 30
GEO_ACCURACY_HINT = "定位误差较大，请到窗边或门口重试"


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    import math

    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _float_or_none(value):
    import math

    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def check_location(lab, lat_raw, lng_raw, accuracy_raw) -> dict:
    """Server-side location verdict for a check-in at `lab`.

    Returns {"ok", "message", "lat", "lng", "accuracy", "distance"}; reasons never leak the lab coordinates.
    """
    lat, lng = _float_or_none(lat_raw), _float_or_none(lng_raw)
    accuracy = _float_or_none(accuracy_raw)
    result = {"ok": False, "message": "", "lat": lat, "lng": lng, "accuracy": accuracy, "distance": None}
    if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        result["message"] = "本实验室要求位置验证，但没有收到你的位置。请允许浏览器获取位置信息后重试（手机需开启定位）。"
        return result
    accuracy = 0.0 if accuracy is None or accuracy < 0 else accuracy
    result["accuracy"] = accuracy
    if accuracy > GEO_MAX_ACCURACY_M:
        result["message"] = GEO_ACCURACY_HINT + f"（当前误差约 {accuracy:.0f} 米）。"
        return result
    distance = haversine_m(lat, lng, lab.geo_lat, lab.geo_lng)
    result["distance"] = distance
    radius = lab.geo_radius_m or GEO_DEFAULT_RADIUS_M
    if distance - min(accuracy, GEO_TOLERANCE_CAP_M) <= radius:
        result["ok"] = True
        return result
    result["message"] = f"你的位置距离实验室约 {distance:.0f} 米，超出签到范围（{radius} 米）。请到实验室现场签到。"
    return result


def new_checkin_token() -> str:
    return secrets.token_urlsafe(16)


def early_minutes() -> int:
    try:
        value = int(current_app.config.get("CHECKIN_EARLY_MINUTES", DEFAULT_EARLY_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_EARLY_MINUTES
    return max(0, value)


def checkin_window(booking) -> tuple[datetime, datetime]:
    start = datetime.combine(booking.date, booking.start_time)
    end = datetime.combine(booking.date, booking.end_time)
    return start - timedelta(minutes=early_minutes()), end


def in_window(booking, now: datetime) -> bool:
    opens, closes = checkin_window(booking)
    return opens <= now <= closes


def checkin_state(booking, now: datetime | None = None) -> str | None:
    """early / open / done / expired for approved bookings, None otherwise."""
    if booking.status != "approved":
        return None
    if booking.checked_in_at is not None:
        return "done"
    from app.utils import local_now

    now = now or local_now()
    opens, closes = checkin_window(booking)
    if now < opens:
        return "early"
    if now > closes:
        return "expired"
    return "open"


def checkin_info(booking, now: datetime | None = None) -> dict | None:
    state = checkin_state(booking, now)
    if state is None:
        return None
    opens, _closes = checkin_window(booking)
    if state == "done":
        label = f"已签到 {booking.checked_in_at.strftime('%m-%d %H:%M')}"
    elif state == "early":
        label = f"未到签到时间（{opens.strftime('%H:%M')} 起可签到）"
    else:
        label = STATE_LABELS[state]
    return {"state": state, "label": label, "badge": STATE_BADGES[state], "short": STATE_LABELS[state], "opens": opens.strftime("%H:%M")}


def captain_checkin_cards(user, now: datetime | None = None) -> list[dict]:
    """Approved bookings of the captain that still matter for check-in (today..7 days)."""
    from app.models import Booking
    from app.utils import local_now

    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_regular_user", False):
        return []
    now = now or local_now()
    today = now.date()
    from sqlalchemy.orm import selectinload

    rows = (
        Booking.query.options(selectinload(Booking.lab)).filter(
            Booking.user_id == user.id,
            Booking.status == "approved",
            Booking.date >= today,
            Booking.date <= today + timedelta(days=7),
        )
        .order_by(Booking.date.asc(), Booking.start_time.asc())
        .all()
    )
    cards = []
    for booking in rows:
        info = checkin_info(booking, now)
        if info is None:
            continue
        cards.append({"booking": booking, "info": info})
    # Actionable ones first: open, then early, then done/expired of today.
    order = {"open": 0, "early": 1, "done": 2, "expired": 3}
    cards.sort(key=lambda c: (order[c["info"]["state"]], c["booking"].date, c["booking"].start_time))
    return cards


def public_base_url() -> str:
    """PUBLIC_BASE_URL config/env first, else the address used for the current request."""
    configured = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured.startswith(("http://", "https://")):
        return configured
    if has_request_context():
        from app.activity import request_is_https

        scheme = "https" if request_is_https() else request.scheme
        return f"{scheme}://{request.host}".rstrip("/")
    return ""


def checkin_url(lab, base: str | None = None) -> str:
    base = public_base_url() if base is None else base
    return f"{base}/checkin/{lab.checkin_token}"


def qr_png_bytes(text: str, *, scale: int = 10, border: int = 4) -> bytes:
    import segno

    code = segno.make(text, error="m")
    buf = BytesIO()
    code.save(buf, kind="png", scale=scale, border=border, dark="#000000", light="#ffffff")
    return buf.getvalue()


def ensure_lab_tokens() -> int:
    """Backfill a permanent token on every lab that lacks one. Returns how many were set."""
    from app import db
    from app.models import Lab

    changed = 0
    for lab in Lab.query.filter(Lab.checkin_token.is_(None)).all():
        lab.checkin_token = new_checkin_token()
        lab.checkin_token_updated_at = None
        changed += 1
    if changed:
        db.session.commit()
    return changed
