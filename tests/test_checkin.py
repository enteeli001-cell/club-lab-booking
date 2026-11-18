"""Booking check-in by scanning the lab's fixed QR code."""
import re
from datetime import datetime, time

import pytest

from app import db
from app.models import AuditLog, Booking, Lab
from tests.conftest import (
    FUTURE_DAY,
    SUPER_PASSWORD,
    add_booking,
    create_lab,
    create_user,
    login,
)


@pytest.fixture
def freeze(monkeypatch):
    def _freeze(hour, minute=0, day=FUTURE_DAY):
        fake = datetime(day.year, day.month, day.day, hour, minute)
        monkeypatch.setattr("app.utils.local_now", lambda: fake)
        monkeypatch.setattr("app.routes.checkin.local_now", lambda: fake)
        return fake

    return _freeze


def _token(app, lab_id):
    with app.app_context():
        return db.session.get(Lab, lab_id).checkin_token


def _approved(app, seed, **kw):
    with app.app_context():
        booking = add_booking(
            db.session.get(Lab, seed["lab_id"]),
            db.session.get(__import__("app.models", fromlist=["User"]).User, seed["member_id"]),
            status="approved",
            **kw,
        )
        return booking.id


def test_lab_token_is_generated_stable_and_unique(app, seed):
    with app.app_context():
        other = create_lab("第二实验室")
        first = db.session.get(Lab, seed["lab_id"]).checkin_token
        second = other.checkin_token
        assert first and second and first != second
        assert len(first) >= 20
    # Restarting the app (running the startup migration again) must not change the token.
    from app import _ensure_checkin_schema

    with app.app_context():
        _ensure_checkin_schema()
        assert db.session.get(Lab, seed["lab_id"]).checkin_token == first


def test_invalid_token(app, client, seed, freeze):
    freeze(10)
    login(client, "2021001")
    resp = client.get("/checkin/not-a-real-token")
    assert resp.status_code == 404
    assert "二维码无效" in resp.get_data(as_text=True)


def test_no_tokenless_entry(app, client, seed):
    login(client, "2021001")
    assert client.get("/checkin/").status_code == 404
    assert client.get("/checkin").status_code == 404


def test_window_before_inside_after(app, client, seed, freeze):
    bid = _approved(app, seed)  # 10:00-11:00
    token = _token(app, seed["lab_id"])
    login(client, "2021001")

    freeze(9, 30)
    text = client.get(f"/checkin/{token}").get_data(as_text=True)
    assert "还没到签到时间" in text and "09:45" in text
    assert "还没到签到时间" in client.post(f"/checkin/{token}").get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Booking, bid).checked_in_at is None

    freeze(9, 50)
    text = client.get(f"/checkin/{token}").get_data(as_text=True)
    assert "确认签到" in text and "活动筹备" in text

    freeze(11, 30)
    text = client.get(f"/checkin/{token}").get_data(as_text=True)
    assert "已过签到时间" in text and "确认签到" not in text
    assert "已过签到时间" in client.post(f"/checkin/{token}").get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Booking, bid).checked_in_at is None


def test_checkin_records_time_ip_ua_and_audit_then_duplicate(app, client, seed, freeze):
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    resp = client.post(f"/checkin/{token}", data={"pledge": "1"}, headers={"User-Agent": "PytestPhone/1.0"})
    assert resp.status_code == 200
    assert "签到成功" in resp.get_data(as_text=True)
    with app.app_context():
        b = db.session.get(Booking, bid)
        assert b.checked_in_at == datetime(2099, 6, 15, 10, 5)
        assert b.checkin_ip
        assert "PytestPhone" in b.checkin_ua
        log = AuditLog.query.filter_by(action="booking.checkin").one()
        assert log.target_id == bid and log.actor_role == "member"
    freeze(10, 20)
    again = client.get(f"/checkin/{token}").get_data(as_text=True)
    assert "已签到过" in again
    assert "已签到过" in client.post(f"/checkin/{token}").get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Booking, bid).checked_in_at == datetime(2099, 6, 15, 10, 5)
        assert AuditLog.query.filter_by(action="booking.checkin").count() == 1


def test_wrong_lab_and_no_booking(app, client, seed, freeze):
    _approved(app, seed)
    with app.app_context():
        other = create_lab("第二实验室")
        other_token = other.checkin_token
    login(client, "2021001")
    freeze(10, 5)
    text = client.get(f"/checkin/{other_token}").get_data(as_text=True)
    assert "扫错实验室" in text and "创新实验室" in text
    assert client.post(f"/checkin/{other_token}").status_code == 200
    with app.app_context():
        assert Booking.query.filter(Booking.checked_in_at.isnot(None)).count() == 0

    # Another captain with no booking at all.
    other_client = client.application.test_client()
    login(other_client, "2021002")
    token = _token(app, seed["lab_id"])
    assert "没有找到可签到的预约" in other_client.get(f"/checkin/{token}").get_data(as_text=True)


def test_pending_booking_cannot_check_in(app, client, seed, freeze):
    with app.app_context():
        from app.models import User

        add_booking(db.session.get(Lab, seed["lab_id"]), db.session.get(User, seed["member_id"]), status="pending")
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    assert "没有找到可签到的预约" in client.post(f"/checkin/{token}").get_data(as_text=True)


def test_anonymous_redirects_to_login_and_returns(app, client, seed, freeze):
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    freeze(10, 5)
    resp = client.get(f"/checkin/{token}")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login?next=")
    assert f"/checkin/{token}" in resp.headers["Location"].replace("%2F", "/")
    # Logging in with the carried "next" lands back on the check-in page.
    nxt = f"/checkin/{token}"
    resp = client.post(
        "/login?next=" + nxt.replace("/", "%2F"),
        data={"student_id": "2021001", "password": "password123", "next": nxt},
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith(nxt)
    assert "确认签到" in client.get(nxt).get_data(as_text=True)


def test_admin_needs_no_checkin(app, client, seed, freeze):
    token = _token(app, seed["lab_id"])
    login(client, "officer01", staff=True)
    freeze(10)
    assert "无需签到" in client.get(f"/checkin/{token}").get_data(as_text=True)


def test_checkin_rate_limited(app, client, seed, freeze):
    app.config["CHECKIN_RATE_LIMIT"] = 3
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10)
    codes = [client.get(f"/checkin/{token}").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


def test_admin_cannot_see_or_use_reset(app, client, seed):
    login(client, "officer01", staff=True)
    page = client.get("/checkin-codes/")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "重置二维码" not in text and "下载 PNG" in text and "打印" in text
    before = _token(app, seed["lab_id"])
    assert client.post(f"/checkin-codes/{seed['lab_id']}/reset", data={"confirm": "yes"}).status_code == 403
    assert _token(app, seed["lab_id"]) == before


def test_member_cannot_open_admin_pages(app, client, seed):
    login(client, "2021001")
    for path in ("/checkin-codes/", f"/checkin-codes/{seed['lab_id']}.png", f"/checkin-codes/{seed['lab_id']}/print"):
        assert client.get(path).status_code == 403


def test_qr_png_and_print_view(app, client, seed):
    login(client, "admin", staff=True)
    png = client.get(f"/checkin-codes/{seed['lab_id']}.png")
    assert png.status_code == 200 and png.data.startswith(b"\x89PNG")
    assert "attachment" in client.get(f"/checkin-codes/{seed['lab_id']}.png?download=1").headers["Content-Disposition"]
    printed = client.get(f"/checkin-codes/{seed['lab_id']}/print").get_data(as_text=True)
    assert "预约队长请用手机扫码签到" in printed and "创新实验室" in printed
    assert f"/checkin/{_token(app, seed['lab_id'])}" in printed


TUNNEL_HEADERS = {"Host": "example-tunnel.test", "CF-Visitor": '{"scheme":"https"}'}


def test_qr_url_uses_public_base_url_or_request_host(app, client, seed):
    token = _token(app, seed["lab_id"])
    client.post("/admin/login", data={"password": SUPER_PASSWORD}, headers=TUNNEL_HEADERS)
    text = client.get("/checkin-codes/", headers=TUNNEL_HEADERS).get_data(as_text=True)
    assert f"https://example-tunnel.test/checkin/{token}" in text
    app.config["PUBLIC_BASE_URL"] = "https://fixed.example.org/"
    text = client.get("/checkin-codes/", headers=TUNNEL_HEADERS).get_data(as_text=True)
    assert f"https://fixed.example.org/checkin/{token}" in text
    assert "example-tunnel.test/checkin" not in text


def test_superadmin_reset_invalidates_old_code_and_audits(app, client, seed, freeze):
    _approved(app, seed)
    old = _token(app, seed["lab_id"])
    login(client, "admin", staff=True)
    page = client.get("/checkin-codes/").get_data(as_text=True)
    assert "重置二维码" in page
    # Reset without confirmation does nothing.
    client.post(f"/checkin-codes/{seed['lab_id']}/reset")
    assert _token(app, seed["lab_id"]) == old
    resp = client.post(f"/checkin-codes/{seed['lab_id']}/reset", data={"confirm": "yes"}, follow_redirects=True)
    assert "旧码立即失效" in resp.get_data(as_text=True)
    new = _token(app, seed["lab_id"])
    assert new != old
    with app.app_context():
        assert AuditLog.query.filter_by(action="checkin.reset").count() == 1
    freeze(10, 5)
    captain = app.test_client()
    login(captain, "2021001")
    assert captain.get(f"/checkin/{old}").status_code == 404
    assert "确认签到" in captain.get(f"/checkin/{new}").get_data(as_text=True)


def test_pinned_card_on_captain_pages(app, client, seed, freeze):
    _approved(app, seed)
    login(client, "2021001")
    freeze(9, 30)
    for path in ("/captain", "/bookings/new", "/bookings", "/records"):
        text = client.get(path).get_data(as_text=True)
        assert "checkin-pin" in text, path
        assert "未到签到时间" in text, path
        assert "扫描实验室门口的二维码" in text, path
    freeze(10, 0)
    assert "可签到" in client.get("/captain").get_data(as_text=True)
    freeze(11, 30)
    assert "已过期未签到" in client.get("/captain").get_data(as_text=True)
    token = _token(app, seed["lab_id"])
    freeze(10, 10)
    client.post(f"/checkin/{token}", data={"pledge": "1"})
    text = client.get("/captain").get_data(as_text=True)
    assert "已签到 06-15 10:10" in text
    assert "已签到 06-15 10:10" in client.get("/records").get_data(as_text=True)


def test_no_card_for_pending_or_far_future(app, client, seed, freeze):
    with app.app_context():
        from app.models import User

        add_booking(db.session.get(Lab, seed["lab_id"]), db.session.get(User, seed["member_id"]), status="pending")
    login(client, "2021001")
    freeze(9, 30)
    assert "checkin-pin" not in client.get("/captain").get_data(as_text=True)


def test_admin_views_show_status_and_filter_missing(app, client, seed, freeze):
    done_id = _approved(app, seed, start=time(10, 0), end=time(11, 0))
    with app.app_context():
        from app.models import User

        add_booking(
            db.session.get(Lab, seed["lab_id"]),
            db.session.get(User, seed["other_id"]),
            start=time(12, 0), end=time(13, 0), purpose="未到场组", status="approved",
        )
        b = db.session.get(Booking, done_id)
        b.checked_in_at = datetime(2099, 6, 15, 10, 2)
        db.session.commit()
    freeze(14)
    login(client, "officer01", staff=True)
    review = client.get("/review/?status=approved").get_data(as_text=True)
    assert "已签到 06-15 10:02" in review and "已过期未签到" in review
    missing = client.get("/review/?checkin=missing").get_data(as_text=True)
    assert "已过期未签到" in missing and "已签到 06-15 10:02" not in missing
    sched = client.get(f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}").get_data(as_text=True)
    assert "已签到" in sched
    sched_missing = client.get(
        f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}&checkin=missing"
    ).get_data(as_text=True)
    assert "未到场组" in sched_missing and "活动筹备" not in sched_missing and "张三 10:00" not in sched_missing
    q = f"start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}"
    stats_missing = client.get(f"/stats/?{q}&checkin=missing").get_data(as_text=True)
    assert "未到场组" in stats_missing and "已签到 06-15" not in stats_missing
    # Captains never see other people's status in the schedule.
    cap = app.test_client()
    login(cap, "2021001")
    assert "已签到" not in cap.get(f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}").get_data(as_text=True)


def test_excel_export_has_checkin_columns(app, client, seed):
    from io import BytesIO

    from openpyxl import load_workbook

    bid = _approved(app, seed)
    with app.app_context():
        db.session.get(Booking, bid).checked_in_at = datetime(2099, 6, 15, 10, 3, 4)
        db.session.commit()
    login(client, "admin", staff=True)
    resp = client.get(f"/stats/export?start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}")
    sheet = load_workbook(BytesIO(resp.data))["明细"]
    header = [c.value for c in sheet[1]]
    assert header[-2:] == ["签到状态", "签到时间"]
    row = [c.value for c in sheet[2]]
    assert row[-1] == "2099-06-15 10:03:04" and row[-2] == "已签到"


def test_nav_has_checkin_qr_for_staff_only(app, client, seed):
    login(client, "officer01", staff=True)
    assert "/checkin-codes/" in client.get("/").get_data(as_text=True)
    cap = app.test_client()
    login(cap, "2021001")
    assert "/checkin-codes/" not in cap.get("/captain").get_data(as_text=True)


def test_migration_adds_columns_to_old_database(tmp_path):
    import sqlite3

    from app import create_app

    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, student_id VARCHAR(32) NOT NULL, name VARCHAR(64) NOT NULL,
            password_hash VARCHAR(256) NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE labs (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL, capacity INTEGER NOT NULL,
            open_start TIME NOT NULL, open_end TIME NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE bookings (id INTEGER PRIMARY KEY, lab_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            date DATE NOT NULL, start_time TIME NOT NULL, end_time TIME NOT NULL, purpose VARCHAR(200) NOT NULL,
            people_count INTEGER NOT NULL, status VARCHAR(16) NOT NULL, reject_reason VARCHAR(200),
            created_at DATETIME NOT NULL, reviewed_by_id INTEGER, reviewed_at DATETIME);
        INSERT INTO users VALUES (1,'admin','总管理员','x','superadmin','2026-01-01 00:00:00');
        INSERT INTO labs VALUES (1,'旧实验室',5,'08:00:00','20:00:00',1,'2026-01-01 00:00:00');
        INSERT INTO labs VALUES (2,'旧实验室二',5,'08:00:00','20:00:00',1,'2026-01-01 00:00:00');
        INSERT INTO bookings (id,lab_id,user_id,date,start_time,end_time,purpose,people_count,status,created_at)
            VALUES (1,1,1,'2026-02-01','10:00:00','11:00:00','旧预约',1,'approved','2026-01-01 00:00:00');
        """
    )
    con.commit()
    con.close()
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
            "SECRET_KEY": "k",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        }
    )
    with app.app_context():
        labs = Lab.query.order_by(Lab.id).all()
        assert [l.name for l in labs] == ["旧实验室", "旧实验室二"]
        assert labs[0].checkin_token and labs[1].checkin_token and labs[0].checkin_token != labs[1].checkin_token
        booking = Booking.query.one()
        assert booking.purpose == "旧预约" and booking.checked_in_at is None
