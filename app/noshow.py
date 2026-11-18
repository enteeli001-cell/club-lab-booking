"""No-show counting and forced reminders for captains who book but never check in."""

from __future__ import annotations

from datetime import datetime

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import AppSetting, Booking, NoShowAlert, User
from app.notices import NO_SHOW_WARNING


def threshold() -> int:
    try:
        return max(1, int(current_app.config.get("NO_SHOW_ALERT_THRESHOLD", 3)))
    except (TypeError, ValueError):
        return 3


def feature_start() -> datetime | None:
    row = db.session.get(AppSetting, "checkin_enabled_at")
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def _end_dt(booking) -> datetime:
    return datetime.combine(booking.date, booking.end_time)


def noshow_bookings(now: datetime, user_id: int | None = None) -> list:
    """Approved, already ended, never checked in; not counting bookings from before the feature."""
    start = feature_start()
    from sqlalchemy.orm import selectinload

    query = Booking.query.options(selectinload(Booking.lab)).filter(
        Booking.status == "approved",
        Booking.checked_in_at.is_(None),
        Booking.date <= now.date(),
    )
    if start is not None:
        query = query.filter(Booking.date >= start.date())
    if user_id is not None:
        query = query.filter(Booking.user_id == user_id)
    rows = []
    for booking in query.order_by(Booking.date.asc(), Booking.start_time.asc()).all():
        end = _end_dt(booking)
        if end < now and (start is None or end >= start):
            rows.append(booking)
    return rows


def counts_by_user(now: datetime) -> dict[int, list]:
    grouped: dict[int, list] = {}
    for booking in noshow_bookings(now):
        grouped.setdefault(booking.user_id, []).append(booking)
    return grouped


def resolve_email(user) -> str | None:
    """Latest booking-form email first, then the account's remembered email."""
    latest = (
        Booking.query.filter(Booking.user_id == user.id, Booking.captain_email.isnot(None), Booking.captain_email != "")
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .first()
    )
    if latest and (latest.captain_email or "").strip():
        return latest.captain_email.strip()
    if (user.last_email or "").strip():
        return user.last_email.strip()
    return None


def _detail_lines(bookings, limit=5) -> list[str]:
    lines = []
    for b in sorted(bookings, key=lambda x: (x.date, x.start_time), reverse=True)[:limit]:
        lines.append(f"{b.date} {b.time_range_label} {b.lab.name}")
    return lines


def alert_email_body(user, count: int, bookings) -> str:
    details = "\n".join(f"  - {line}" for line in _detail_lines(bookings))
    return (
        f"{user.name}，你好：\n\n"
        f"你已通过审核的实验室预约，累计 {count} 次没有扫码签到。最近的记录：\n{details}\n\n"
        f"{NO_SHOW_WARNING}\n"
        f"到达实验室后，请用手机扫描实验室门口的二维码完成签到。\n"
        f"（此邮件由系统自动发送）\n"
    )


def scan_noshow_alerts(now: datetime | None = None) -> dict:
    """Create one alert per new cumulative level >= threshold and email the captain. Idempotent."""
    from app.mailer import send_mail
    from app.utils import local_now

    now = now or local_now()
    limit = threshold()
    created = 0
    for user_id, bookings in counts_by_user(now).items():
        count = len(bookings)
        if count < limit:
            continue
        user = db.session.get(User, user_id)
        if user is None or not user.is_regular_user:
            continue
        if NoShowAlert.query.filter_by(user_id=user_id, level=count).first():
            continue
        alert = NoShowAlert(user_id=user_id, level=count)
        db.session.add(alert)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            continue
        created += 1
        email = resolve_email(user)
        if not email:
            alert.email_status = "no_email"
        else:
            alert.email_to = email
            try:
                log = send_mail(
                    email,
                    "【社团实验室预约管理系统】未签到提醒",
                    alert_email_body(user, count, bookings),
                )
                alert.email_status = log.status
            except Exception:
                current_app.logger.exception("未签到提醒邮件发送失败")
                alert.email_status = "failed"
        db.session.commit()
    return {"alerts": created}


def pending_alert_context(user) -> dict | None:
    alert = (
        NoShowAlert.query.filter(NoShowAlert.user_id == user.id, NoShowAlert.acknowledged_at.is_(None))
        .order_by(NoShowAlert.level.desc())
        .first()
    )
    if alert is None:
        return None
    from app.utils import local_now

    bookings = noshow_bookings(local_now(), user.id)
    return {
        "count": max(alert.level, len(bookings)),
        "details": _detail_lines(bookings),
        "warning": NO_SHOW_WARNING,
    }
