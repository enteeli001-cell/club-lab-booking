"""The platform clock is Beijing time (APP_TIMEZONE), whatever zone the host machine is in."""
import os
import time as systime
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import pytest

from app import create_app, db
from app.checkin import checkin_state
from app.models import Booking, MailLog
from app.utils import app_timezone, local_now, timezone_label, today
from tests.conftest import add_booking, booking_form, create_lab, login

BEIJING = ZoneInfo("Asia/Shanghai")
LA = ZoneInfo("America/Los_Angeles")
# 2099-06-15 17:00 UTC = 2099-06-16 01:00 in Beijing, but still 10:00 on June 15 in Los Angeles.
INSTANT = datetime(2099, 6, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def host_in_los_angeles():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    systime.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    systime.tzset()


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr("app.utils.aware_now", lambda: INSTANT.astimezone(app_timezone()))


def test_default_timezone_is_beijing_and_ignores_host_tz(app, host_in_los_angeles):
    assert datetime.now().astimezone().utcoffset().total_seconds() in (-8 * 3600, -7 * 3600)  # host really is LA
    with app.app_context():
        assert app.config["APP_TIMEZONE"] == "Asia/Shanghai"
        assert str(app_timezone()) == "Asia/Shanghai" and timezone_label() == "北京时间"
        real = datetime.now(BEIJING).replace(tzinfo=None)
        assert abs((local_now() - real).total_seconds()) < 5
        assert today() == real.date()
        # The host wall clock differs from the platform clock by the LA/Beijing offset (15 or 16 hours).
        host = datetime.now()
        assert 14 * 3600 < (local_now() - host).total_seconds() < 17 * 3600


def test_app_timezone_is_configurable_and_validated(tmp_path):
    cfg = {"TESTING": True, "DATA_DIR": str(tmp_path / "d"), "LOG_DIR": str(tmp_path / "l"), "SECRET_KEY": "k",
           "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}"}
    other = create_app({**cfg, "APP_TIMEZONE": "America/Los_Angeles"})
    with other.app_context():
        assert timezone_label() == "America/Los_Angeles"
        assert abs((local_now() - datetime.now(LA).replace(tzinfo=None)).total_seconds()) < 5
    with pytest.raises(Exception):
        create_app({**cfg, "APP_TIMEZONE": "Mars/Olympus", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'u.db'}"})


def test_today_and_validation_use_beijing_date(app, client, seed, frozen, host_in_los_angeles):
    with app.app_context():
        assert today() == date(2099, 6, 16)  # LA would still say June 15
        lab = create_lab("全天室", capacity=10, open_start=time(0, 0), open_end=time(23, 59))
        lab_id = lab.id
    login(client, "2021001")
    # June 15 is already "yesterday" on the platform clock even though it is June 15 in Los Angeles.
    past = client.post("/bookings/new", data=booking_form(lab_id, date="2099-06-15", start_time="10:00", end_time="11:00"))
    with app.app_context():
        assert Booking.query.count() == 0
    assert "不能预约已经过去的时间" in past.get_data(as_text=True)
    ok = client.post("/bookings/new", data=booking_form(lab_id, date="2099-06-16", start_time="03:00", end_time="04:00"))
    assert ok.status_code == 302
    with app.app_context():
        assert Booking.query.count() == 1


def test_checkin_window_and_lock_follow_beijing_clock(app, seed, frozen, host_in_los_angeles):
    with app.app_context():
        lab = create_lab("全天室", capacity=10, open_start=time(0, 0), open_end=time(23, 59))
        from app.models import User

        member = db.session.get(User, seed["member_id"])
        # Platform "now" is 2099-06-16 01:00. Starts 01:10 -> window (opens 00:55) is open now.
        soon = add_booking(lab, member, day=date(2099, 6, 16), start=time(1, 10), end=time(2, 10), status="approved")
        assert checkin_state(soon, local_now()) == "open"
        # Starts 10:00 on the 16th -> far in the future for the platform clock.
        later = add_booking(lab, member, day=date(2099, 6, 16), start=time(10, 0), end=time(11, 0), status="approved")
        assert checkin_state(later, local_now()) == "early"
        # 10:00 on the 15th would be "now" in Los Angeles, but already ended for the platform.
        ended = add_booking(lab, member, day=date(2099, 6, 15), start=time(10, 0), end=time(11, 0), status="approved")
        assert checkin_state(ended, local_now()) == "expired"


def test_mail_date_header_and_filename_use_platform_clock(app, seed, frozen, host_in_los_angeles, tmp_path):
    from app.mailer import send_mail

    with app.app_context():
        log = send_mail("someone@example.com", "t", "b")
        assert log.status == "local"
        eml = next((tmp_path / "data" / "outbox").glob("*.eml"))
        assert eml.name.startswith("20990616010000_")
        header = [l for l in eml.read_text(encoding="utf-8").splitlines() if l.startswith("Date:")][0]
        sent = parsedate_to_datetime(header.split(": ", 1)[1])
        assert sent.utcoffset().total_seconds() == 8 * 3600 and sent.hour == 1


def test_pages_say_beijing_time_and_date_min_is_platform_today(app, client, seed, frozen):
    with app.app_context():
        create_lab("全天室", open_start=time(0, 0), open_end=time(23, 59))
    html = client.get("/login").get_data(as_text=True)
    assert "页面所有时间均为北京时间" in html and '<meta name="app-today" content="2099-06-16">' in html
    login(client, "2021001")
    form = client.get("/bookings/new").get_data(as_text=True)
    assert 'class="tz-badge">北京时间' in form and 'min="2099-06-16"' in form
