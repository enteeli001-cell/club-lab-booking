"""No-show forced reminders (B), start-of-booking reminder mails (C), page footer."""
from datetime import datetime, time, timedelta

import pytest

from app import db
from app.jobs import run_scans, send_start_reminders
from app.models import Booking, Lab, MailLog, NoShowAlert, User
from app.noshow import scan_noshow_alerts
from tests.conftest import FUTURE_DAY, add_booking, login


def _user(app, seed, key="member_id"):
    return db.session.get(User, seed[key])


def _lab(app, seed):
    return db.session.get(Lab, seed["lab_id"])


def _no_show(app, seed, n, *, status="approved"):
    """n approved bookings on consecutive days, all ending long before NOW."""
    out = []
    for i in range(n):
        day = FUTURE_DAY + timedelta(days=i)
        out.append(add_booking(_lab(app, seed), _user(app, seed), day=day, start=time(9), end=time(10), status=status))
    return out


NOW = datetime(FUTURE_DAY.year, FUTURE_DAY.month, FUTURE_DAY.day, 12, 0) + timedelta(days=10)


@pytest.fixture
def clock(monkeypatch):
    def _set(dt):
        monkeypatch.setattr("app.utils.local_now", lambda: dt)
        monkeypatch.setattr("app.routes.checkin.local_now", lambda: dt)
        monkeypatch.setattr("app.routes.noshow.local_now", lambda: dt)
        return dt

    return _set


def test_second_no_show_does_not_alert_third_does_and_sends_mail(app, seed, clock):
    clock(NOW)
    with app.app_context():
        _no_show(app, seed, 2)
        assert scan_noshow_alerts(NOW) == {"alerts": 0}
        assert NoShowAlert.query.count() == 0
        _no_show_third = add_booking(
            _lab(app, seed), _user(app, seed), day=FUTURE_DAY + timedelta(days=2), start=time(9), end=time(10), status="approved"
        )
        assert scan_noshow_alerts(NOW) == {"alerts": 1}
        alert = NoShowAlert.query.one()
        assert alert.level == 3 and alert.acknowledged_at is None
        assert alert.email_status == "local" and alert.email_to == "captain@example.com"
        mails = MailLog.query.filter(MailLog.subject.like("%未签到提醒%")).all()
        assert len(mails) == 1 and "累计 3 次" in mails[0].body
        # Idempotent: scanning again does nothing, no second mail.
        assert scan_noshow_alerts(NOW) == {"alerts": 0}
        assert MailLog.query.filter(MailLog.subject.like("%未签到提醒%")).count() == 1


def test_forced_overlay_until_acknowledged_then_fourth_alerts_again(app, client, seed, clock):
    clock(NOW)
    with app.app_context():
        _no_show(app, seed, 3)
        scan_noshow_alerts(NOW)
    login(client, "2021001")
    page = client.get("/captain").get_data(as_text=True)
    assert "noshow-overlay" in page and "累计 <strong>3</strong>" in page and "我已知晓" in page
    assert "可能会被限制预约" in page
    # Still forced on other pages.
    assert "noshow-overlay" in client.get("/records").get_data(as_text=True)
    resp = client.post("/noshow/ack", data={"next": "/records"})
    assert resp.status_code == 302
    with app.app_context():
        alert = NoShowAlert.query.one()
        assert alert.acknowledged_at is not None
    assert "noshow-overlay" not in client.get("/captain").get_data(as_text=True)
    # Same count again: no new alert, no overlay.
    with app.app_context():
        assert scan_noshow_alerts(NOW) == {"alerts": 0}
        add_booking(_lab(app, seed), _user(app, seed), day=FUTURE_DAY + timedelta(days=3), start=time(9), end=time(10), status="approved")
        assert scan_noshow_alerts(NOW) == {"alerts": 1}
        assert sorted(a.level for a in NoShowAlert.query.all()) == [3, 4]
    assert "累计 <strong>4</strong>" in client.get("/captain").get_data(as_text=True)


def test_cancelled_rejected_checked_in_and_future_not_counted(app, seed, clock):
    clock(NOW)
    with app.app_context():
        _no_show(app, seed, 1, status="cancelled")
        _no_show(app, seed, 1, status="rejected")
        done = _no_show(app, seed, 1)[0]
        done.checked_in_at = NOW
        add_booking(_lab(app, seed), _user(app, seed), day=NOW.date() + timedelta(days=1), status="approved")
        # approved but still running: not ended yet
        add_booking(_lab(app, seed), _user(app, seed), day=NOW.date(), start=time(11), end=time(13), status="approved")
        from app.noshow import noshow_bookings

        assert noshow_bookings(NOW, seed["member_id"]) == []


def test_bookings_before_feature_start_not_counted(app, seed, clock):
    from app.models import AppSetting

    with app.app_context():
        db.session.get(AppSetting, "checkin_enabled_at").value = "2099-07-01T00:00:00"
        db.session.commit()
        _no_show(app, seed, 3)  # all in June 2099, before the feature start
        assert scan_noshow_alerts(NOW) == {"alerts": 0}


def test_no_email_is_flagged_and_admin_overview(app, client, seed, clock):
    clock(NOW)
    with app.app_context():
        user = _user(app, seed)
        user.last_email = None
        for b in _no_show(app, seed, 3):
            b.captain_email = None
        db.session.commit()
        scan_noshow_alerts(NOW)
        assert NoShowAlert.query.one().email_status == "no_email"
        assert MailLog.query.count() == 0
    login(client, "officer01", staff=True)
    page = client.get("/noshows/").get_data(as_text=True)
    assert "张三" in page and "无邮箱" in page and "较多" in page
    assert "张三" in client.get("/noshows/?view=high").get_data(as_text=True)
    cap = app.test_client()
    login(cap, "2021001")
    assert cap.get("/noshows/").status_code == 403


def test_email_prefers_latest_booking_form_email(app, seed, clock):
    clock(NOW)
    with app.app_context():
        user = _user(app, seed)
        user.last_email = "account@example.com"
        bookings = _no_show(app, seed, 3)
        bookings[0].captain_email = "old@example.com"
        bookings[2].captain_email = "latest@example.com"
        bookings[2].created_at = NOW
        bookings[0].created_at = NOW - timedelta(days=5)
        bookings[1].created_at = NOW - timedelta(days=4)
        db.session.commit()
        scan_noshow_alerts(NOW)
        assert NoShowAlert.query.one().email_to == "latest@example.com"


# ------------------------------------------------------------------ C


def _approved_at(app, seed, start_dt, minutes=60, **kw):
    end_dt = start_dt + timedelta(minutes=minutes)
    return add_booking(
        _lab(app, seed), _user(app, seed), day=start_dt.date(), start=start_dt.time(), end=end_dt.time(), status="approved", **kw
    )


def test_reminder_sent_once_within_window_only(app, seed, clock):
    now = datetime(2099, 6, 15, 9, 0)
    clock(now)
    with app.app_context():
        inside = _approved_at(app, seed, now + timedelta(minutes=25))
        outside = _approved_at(app, seed, now + timedelta(minutes=45))
        inside_id, outside_id = inside.id, outside.id
        past = _approved_at(app, seed, now - timedelta(minutes=10))  # already started: no back-fill
        past_id = past.id
        assert send_start_reminders(now)["sent"] == 1
        assert db.session.get(Booking, inside_id).reminder_sent_at is not None
        assert db.session.get(Booking, outside_id).reminder_sent_at is None
        assert db.session.get(Booking, past_id).reminder_sent_at is None
        mails = MailLog.query.filter(MailLog.booking_id == inside_id).all()
        assert len(mails) == 1 and mails[0].status == "local"
        body = mails[0].body
        assert "创新实验室" in body and "扫描实验室门口的二维码" in body and "签到窗口" in body and "实验室规则提醒" in body
        # Once only.
        assert send_start_reminders(now)["sent"] == 0
        assert send_start_reminders(now + timedelta(minutes=5))["sent"] == 0
        assert MailLog.query.filter(MailLog.booking_id == inside_id).count() == 1
        # The 45-minute booking becomes due later.
        assert send_start_reminders(now + timedelta(minutes=20))["sent"] == 1


def test_pending_and_cancelled_bookings_get_no_reminder(app, seed, clock):
    now = datetime(2099, 6, 15, 9, 0)
    clock(now)
    with app.app_context():
        start = now + timedelta(minutes=20)
        add_booking(_lab(app, seed), _user(app, seed), day=start.date(), start=start.time(), end=(start + timedelta(hours=1)).time(), status="pending")
        add_booking(_lab(app, seed), _user(app, seed), day=start.date(), start=start.time(), end=(start + timedelta(hours=1)).time(), status="cancelled")
        assert send_start_reminders(now)["sent"] == 0


def test_reschedule_clears_reminder_and_reminds_again(app, client, seed, clock):
    now = datetime(2099, 6, 15, 9, 0)
    clock(now)
    with app.app_context():
        booking = _approved_at(app, seed, now + timedelta(minutes=25))
        bid = booking.id
        assert send_start_reminders(now)["sent"] == 1
    login(client, "2021001")
    new_start = now + timedelta(hours=3)
    resp = client.post(
        f"/bookings/{bid}/edit",
        data={
            "lab_id": str(seed["lab_id"]),
            "date": new_start.date().isoformat(),
            "start_time": new_start.strftime("%H:%M"),
            "end_time": (new_start + timedelta(hours=1)).strftime("%H:%M"),
            "purpose": "改期",
            "people_count": "1",
            "captain_phone": "13800138000",
            "captain_email": "captain@example.com",
        },
    )
    assert resp.status_code == 302
    with app.app_context():
        b = db.session.get(Booking, bid)
        assert b.status == "pending" and b.reminder_sent_at is None and b.reminder_attempts == 0
        b.status = "approved"  # re-approved by an admin
        db.session.commit()
    later = new_start - timedelta(minutes=20)
    clock(later)
    with app.app_context():
        assert send_start_reminders(later)["sent"] == 1
        assert MailLog.query.filter(MailLog.booking_id == bid, MailLog.subject.like("%即将开始%")).count() == 2


def test_failed_mail_retries_at_most_twice(app, seed, clock, monkeypatch):
    now = datetime(2099, 6, 15, 9, 0)
    clock(now)
    calls = []

    class _Log:
        status = "failed"

    def fake_send(recipient, subject, body, *, booking_id=None):
        calls.append(booking_id)
        return _Log()

    monkeypatch.setattr("app.mailer.send_mail", fake_send)
    with app.app_context():
        booking = _approved_at(app, seed, now + timedelta(minutes=20))
        bid = booking.id
        results = [send_start_reminders(now) for _ in range(5)]
        assert [r["failed"] for r in results] == [1, 1, 1, 0, 0]
        assert len(calls) == 3  # first try + 2 retries
        assert db.session.get(Booking, bid).reminder_sent_at is None


def test_conditional_update_prevents_double_send(app, seed, clock, tmp_path):
    import sqlite3

    from sqlalchemy import event

    now = datetime(2099, 6, 15, 9, 0)
    clock(now)
    with app.app_context():
        booking = _approved_at(app, seed, now + timedelta(minutes=20))
        bid = booking.id
        fired = []

        def rival(conn, cursor, statement, parameters, context, executemany):
            # Another worker wins the claim right before our conditional UPDATE runs.
            if statement.startswith("UPDATE bookings SET reminder_attempts") and not fired:
                fired.append(1)
                other = sqlite3.connect(str(tmp_path / "test.db"), timeout=10)
                other.execute("UPDATE bookings SET reminder_attempts = 1 WHERE id = ?", (bid,))
                other.commit()
                other.close()

        event.listen(db.engine, "before_cursor_execute", rival)
        try:
            assert send_start_reminders(now)["sent"] == 0
        finally:
            event.remove(db.engine, "before_cursor_execute", rival)
        assert fired
        assert MailLog.query.filter(MailLog.booking_id == bid).count() == 0


def test_run_scans_survives_errors(app, seed, monkeypatch):
    def boom(now=None):
        raise RuntimeError("x")

    monkeypatch.setattr("app.jobs.send_start_reminders", boom)
    with app.app_context():
        result = run_scans(NOW)
        assert result["reminders"] == {"error": True}
        assert result["noshow"] == {"alerts": 0}


def test_scanner_thread_not_started_in_testing(app):
    from app.jobs import start_background_scanner

    assert start_background_scanner(app) is None


# ---------------------------------------------------------------- footer


def test_footer_on_pages_but_not_print_view(app, client, seed):
    import app.notices as notices

    assert not hasattr(notices, "FOOTER_NOTICE")
    for path in ("/login", "/admin/login"):
        text = client.get(path).get_data(as_text=True)
        assert "site-footer" in text and "tz-note" in text and "请勿外传" not in text, path
    login(client, "admin", staff=True)
    assert "site-footer" in client.get("/").get_data(as_text=True)
    assert "site-footer" in client.get("/checkin-codes/").get_data(as_text=True)
    assert "site-footer" not in client.get(f"/checkin-codes/{seed['lab_id']}/print").get_data(as_text=True)

