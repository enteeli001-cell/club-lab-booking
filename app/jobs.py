"""Background scanner: start-of-booking reminder mails, no-show alerts and level-3 announcement mails.

The scan functions are plain callables (tests call them directly); the thread only
loops over them every SCAN_INTERVAL_SECONDS and never exits on an exception.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import text

from app import db
from app.models import Booking
from app.notices import REMINDER_RULES


def reminder_body(booking, window_minutes: int) -> str:
    opens = datetime.combine(booking.date, booking.start_time) - timedelta(minutes=window_minutes)
    return (
        f"{booking.user.name}，你好：\n\n"
        f"你预约的实验室测试即将开始：\n"
        f"实验室：{booking.lab.name}\n"
        f"日期时段：{booking.date} {booking.time_range_label}\n\n"
        f"签到方式：到达实验室后，请用手机扫描实验室门口的二维码，登录后点击“确认签到”。"
        f"只有扫码才能完成签到，不能在网页上直接签到。\n"
        f"签到窗口：{opens.strftime('%H:%M')}（开始前 {window_minutes} 分钟）至 {booking.end_time.strftime('%H:%M')}（结束时间）。\n\n"
        f"实验室规则提醒：{REMINDER_RULES}\n\n"
        f"如果无法到场，请提前在“申请记录”中取消预约。\n"
        f"（此邮件由系统自动发送）\n"
    )


def send_start_reminders(now: datetime | None = None) -> dict:
    """Mail approved captains whose booking starts within REMINDER_MINUTES_BEFORE. Once per booking."""
    from app.checkin import early_minutes
    from app.mailer import send_mail
    from app.utils import local_now

    now = now or local_now()
    lead = timedelta(minutes=int(current_app.config.get("REMINDER_MINUTES_BEFORE", 30)))
    max_attempts = 1 + int(current_app.config.get("REMINDER_MAX_RETRIES", 2))
    sent = failed = skipped = 0
    candidates = Booking.query.filter(
        Booking.status == "approved",
        Booking.reminder_sent_at.is_(None),
        Booking.reminder_attempts < max_attempts,
        Booking.date >= now.date(),
        Booking.date <= (now + lead).date(),
    ).all()
    for booking in candidates:
        start = datetime.combine(booking.date, booking.start_time)
        if not (now < start <= now + lead):
            continue  # past bookings are never back-filled; far-future ones wait
        recipient = (booking.captain_email or booking.user.last_email or "").strip()
        if not recipient:
            skipped += 1
            continue
        seen_attempts = booking.reminder_attempts
        booking_id = booking.id
        # Conditional UPDATE claims the booking; only one worker can win each attempt.
        claim = db.session.execute(
            text(
                "UPDATE bookings SET reminder_attempts = reminder_attempts + 1 "
                "WHERE id = :id AND reminder_sent_at IS NULL AND reminder_attempts = :seen AND status = 'approved'"
            ),
            {"id": booking_id, "seen": seen_attempts},
        )
        db.session.commit()
        if claim.rowcount != 1:
            continue
        db.session.expire_all()
        booking = db.session.get(Booking, booking_id)
        try:
            log = send_mail(
                recipient,
                "【社团实验室预约管理系统】预约即将开始，请扫码签到",
                reminder_body(booking, early_minutes()),
                booking_id=booking_id,
            )
            ok = log.status in ("sent", "local")
        except Exception:
            current_app.logger.exception("开始前提醒邮件发送失败")
            ok = False
        if ok:
            db.session.execute(
                text("UPDATE bookings SET reminder_sent_at = :now WHERE id = :id AND reminder_sent_at IS NULL"),
                {"now": now.strftime("%Y-%m-%d %H:%M:%S.%f"), "id": booking_id},
            )
            db.session.commit()
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "skipped_no_email": skipped}


def run_scans(now: datetime | None = None) -> dict:
    """One scan pass for both features; each part is isolated so one failure cannot stop the other."""
    from app.announcements import scan_announcement_mail
    from app.noshow import scan_noshow_alerts

    result = {}
    for name, fn in (
        ("reminders", send_start_reminders),
        ("noshow", scan_noshow_alerts),
        ("announcements", scan_announcement_mail),
    ):
        try:
            result[name] = fn(now)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("定时扫描 %s 失败", name)
            result[name] = {"error": True}
    return result


def start_background_scanner(app) -> threading.Thread | None:
    """Start the 60-second scanner thread (never under TESTING; only once per process)."""
    if app.config.get("TESTING") or app.extensions.get("scanner_thread"):
        return None
    interval = max(5, int(app.config.get("SCAN_INTERVAL_SECONDS", 60)))
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                with app.app_context():
                    run_scans()
            except Exception:
                try:
                    app.logger.exception("后台扫描异常（已忽略，继续运行）")
                except Exception:
                    pass

    thread = threading.Thread(target=loop, name="lab-scanner", daemon=True)
    app.extensions["scanner_thread"] = thread
    app.extensions["scanner_stop"] = stop
    thread.start()
    return thread
