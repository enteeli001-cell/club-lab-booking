"""In-page QR check-in: pledge, location mode, scanner button, auto refresh, login privacy notice, data wipe."""
import json
import math
import sqlite3
from datetime import datetime

import pytest

from app import db
from app.checkin import GEO_ACCURACY_HINT, check_location, haversine_m
from app.models import AuditLog, Booking, Lab, MailLog, User
from app.notices import CHECKIN_PLEDGE, CHECKIN_PLEDGE_CHECKBOX, CHECKIN_PLEDGE_TITLE, PRIVACY_NOTICE
from tests.conftest import (
    ADMIN_PASSWORD,
    CAPTAIN_EMAIL,
    CAPTAIN_PHONE,
    FUTURE_DAY,
    SUPER_PASSWORD,
    add_booking,
    create_lab,
    create_user,
    login,
)
from tests.test_checkin import _approved, _token, freeze  # noqa: F401  (fixture re-export)

LAB_LAT, LAB_LNG = 31.0, 121.0
M_PER_DEG = 6371008.8 * math.pi / 180


def north(meters):
    """A latitude `meters` north of the lab (same longitude)."""
    return LAB_LAT + meters / M_PER_DEG


def _geo_lab(app, seed, required=True, radius=30):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        lab.geo_lat, lab.geo_lng, lab.geo_radius_m, lab.geo_required = LAB_LAT, LAB_LNG, radius, required
        db.session.commit()


def _post(client, token, **data):
    return client.post(f"/checkin/{token}", data=data, headers={"X-Requested-With": "fetch", "Accept": "application/json"})


def _loc(meters, accuracy=0, **extra):
    return {"pledge": "1", "lat": f"{north(meters):.7f}", "lng": f"{LAB_LNG}", "accuracy": str(accuracy), **extra}


# ------------------------------------------------------------------ pledge

def test_pledge_text_is_the_agreed_eight_items():
    assert CHECKIN_PLEDGE_TITLE == "签到前安全确认"
    assert len(CHECKIN_PLEDGE) == 8
    assert CHECKIN_PLEDGE[0] == "本时段预约申请表上的所有人员均已到场，无冒名或替签。"
    assert CHECKIN_PLEDGE[7] == "离开前清理台面，工具按原位归还。"
    assert CHECKIN_PLEDGE_CHECKBOX == "我已阅读并承诺遵守以上要求"


def test_checkin_without_pledge_is_rejected_and_writes_nothing(app, client, seed, freeze):
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    resp = _post(client, token)  # JSON mode, no pledge
    data = resp.get_json()
    assert resp.status_code == 400 and data["ok"] is False and data["code"] == "pledge" and "承诺" in data["message"]
    html = client.post(f"/checkin/{token}")  # classic form, no pledge: confirm page again with the message
    assert html.status_code == 400
    text = html.get_data(as_text=True)
    assert "我已阅读并承诺遵守以上要求" in text and "请先阅读并勾选" in text
    with app.app_context():
        b = db.session.get(Booking, bid)
        assert b.checked_in_at is None and b.pledge_accepted_at is None
        assert AuditLog.query.filter_by(action="booking.checkin").count() == 0


def test_checkin_with_pledge_stores_timestamp_and_admin_sees_it(app, client, seed, freeze):
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    resp = _post(client, token, pledge="1", booking_id=str(bid))
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    with app.app_context():
        assert db.session.get(Booking, bid).pledge_accepted_at == datetime(2099, 6, 15, 10, 5)
    admin = client.application.test_client()
    login(admin, "officer01", ADMIN_PASSWORD, staff=True)
    page = admin.get("/review/?status=approved").get_data(as_text=True)
    assert "安全承诺：06-15 10:05" in page
    assert "安全承诺" not in client.get("/records/?tab=bookings").get_data(as_text=True)


def test_direct_checkin_page_shows_pledge_before_confirm_button(app, client, seed, freeze):
    _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    text = client.get(f"/checkin/{token}").get_data(as_text=True)
    assert CHECKIN_PLEDGE_TITLE in text and all(item in text for item in CHECKIN_PLEDGE)
    assert text.index(CHECKIN_PLEDGE_TITLE) < text.index("确认签到</button>")
    assert 'name="pledge"' in text and "disabled" in text
    ok = client.post(f"/checkin/{token}", data={"pledge": "1"})
    assert ok.status_code == 200 and "签到成功" in ok.get_data(as_text=True)


def test_wrong_lab_or_early_takes_priority_over_pledge_message(app, client, seed, freeze):
    _approved(app, seed)
    with app.app_context():
        other_token = create_lab("第二实验室").checkin_token
    login(client, "2021001")
    freeze(10, 5)
    assert "扫错实验室" in _post(client, other_token, pledge="1").get_json()["title"]
    freeze(7, 0)
    assert "还没到签到时间" in _post(client, _token(app, seed["lab_id"]), pledge="1").get_json()["title"]


def test_booking_id_must_match_and_json_login_required(app, client, seed, freeze):
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    freeze(10, 5)
    anon = _post(client, token, pledge="1")
    assert anon.status_code == 401 and anon.get_json()["code"] == "login"
    login(client, "2021001")
    bad = _post(client, token, pledge="1", booking_id=str(bid + 99))
    assert bad.status_code == 400 and bad.get_json()["code"] == "booking_mismatch"
    again = _post(client, token, pledge="1", booking_id=str(bid))
    assert again.get_json()["ok"] is True
    dup = _post(client, token, pledge="1", booking_id=str(bid)).get_json()
    assert dup["ok"] is False and "已签到过" in dup["title"]


# ------------------------------------------------------------------ location mode

def test_haversine_basics():
    assert haversine_m(31, 121, 31, 121) == 0
    assert abs(haversine_m(31, 121, north(100), 121) - 100) < 0.01
    assert 111000 < haversine_m(0, 0, 1, 0) < 111400


def test_geo_boundaries_29_ok_31_rejected(app, client, seed, freeze):
    _geo_lab(app, seed)
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    far = _post(client, token, **_loc(31))
    assert far.status_code == 403 and far.get_json()["code"] == "geo" and "超出签到范围" in far.get_json()["message"]
    with app.app_context():
        assert db.session.get(Booking, bid).checked_in_at is None
    near = _post(client, token, **_loc(29))
    assert near.status_code == 200 and near.get_json()["ok"] is True
    with app.app_context():
        b = db.session.get(Booking, bid)
        assert abs(b.checkin_distance_m - 29) < 0.1 and abs(b.checkin_lat - north(29)) < 1e-6
        assert b.checkin_lng == LAB_LNG and b.checkin_accuracy == 0


def test_geo_accuracy_is_tolerance_capped_and_rejected_when_too_large(app, seed):
    _geo_lab(app, seed)
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        ok = check_location(lab, north(60), LAB_LNG, 40)     # 60 - 40 = 20 <= 30
        assert ok["ok"] and abs(ok["distance"] - 60) < 0.1
        assert not check_location(lab, north(100), LAB_LNG, 40)["ok"]   # 100 - 40 = 60 > 30
        assert check_location(lab, north(79.9), LAB_LNG, 50)["ok"]      # 79.9 - 50 = 29.9 <= 30
        assert not check_location(lab, north(81), LAB_LNG, 100)["ok"]   # tolerance is capped at 50 m
        assert check_location(lab, north(79.9), LAB_LNG, 100)["ok"]     # 100 m is still acceptable
        bad = check_location(lab, north(5), LAB_LNG, 101)
        assert not bad["ok"] and GEO_ACCURACY_HINT in bad["message"]


def test_geo_missing_or_garbage_location_is_rejected(app, client, seed, freeze):
    _geo_lab(app, seed)
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    for data in ({"pledge": "1"}, {"pledge": "1", "lat": "abc", "lng": "1", "accuracy": "3"},
                 {"pledge": "1", "lat": "95", "lng": "121", "accuracy": "3"}, {"pledge": "1", "lat": "nan", "lng": "nan"}):
        resp = _post(client, token, **data)
        assert resp.status_code == 403 and resp.get_json()["code"] == "geo" and "位置" in resp.get_json()["message"]
    html = client.post(f"/checkin/{token}", data={"pledge": "1"})
    assert html.status_code == 403 and "没有收到你的位置" in html.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Booking, bid).checked_in_at is None
    inaccurate = _post(client, token, **_loc(1, accuracy=150)).get_json()
    assert GEO_ACCURACY_HINT in inaccurate["message"]


def test_lab_without_geo_required_does_not_need_location(app, client, seed, freeze):
    _geo_lab(app, seed, required=False)   # coordinates stored, switch off
    bid = _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    resp = _post(client, token, pledge="1")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    with app.app_context():
        b = db.session.get(Booking, bid)
        assert b.checkin_lat is None and b.checkin_distance_m is None


def test_geo_required_without_coordinates_is_inactive(app, seed):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        lab.geo_required = True
        assert lab.geo_active is False
        lab.geo_lat, lab.geo_lng = 1.0, 2.0
        assert lab.geo_active is True


def test_geo_data_is_admin_only_and_never_shown_to_other_captains(app, client, seed, freeze):
    _geo_lab(app, seed)
    _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    assert _post(client, token, **_loc(12)).get_json()["ok"]
    needle = f"{north(12):.5f}"
    admin = client.application.test_client()
    login(admin, "officer01", ADMIN_PASSWORD, staff=True)
    page = admin.get("/review/?status=approved").get_data(as_text=True)
    assert needle in page and "距实验室约 12 米" in page
    other = client.application.test_client()
    login(other, "2021002")
    for url in ("/captain", "/bookings", "/schedule", "/records/?tab=bookings"):
        text = other.get(url).get_data(as_text=True)
        assert needle not in text and "距实验室" not in text
    assert needle not in client.get("/schedule").get_data(as_text=True)


def test_geo_page_marks_form_and_cards(app, client, seed, freeze):
    _geo_lab(app, seed)
    _approved(app, seed)
    token = _token(app, seed["lab_id"])
    login(client, "2021001")
    freeze(10, 5)
    assert 'data-geo="1"' in client.get(f"/checkin/{token}").get_data(as_text=True)
    assert 'data-geo="1"' in client.get("/captain").get_data(as_text=True)


# ------------------------------------------------------------------ lab settings

def test_lab_form_geo_validation_and_save(app, client, seed):
    login(client, "officer01", ADMIN_PASSWORD, staff=True)
    base = {"name": "定位实验室", "capacity": "8", "open_start": "08:00", "open_end": "20:00", "enabled": "on"}
    assert "经纬度" in client.post("/labs/new", data={**base, "geo_required": "on"}).get_data(as_text=True)
    for bad in ({"geo_lat": "91", "geo_lng": "10"}, {"geo_lat": "10", "geo_lng": "181"}, {"geo_lat": "10"}, {"geo_lat": "x", "geo_lng": "y"},
                {"geo_lat": "10", "geo_lng": "10", "geo_radius_m": "2"}, {"geo_lat": "10", "geo_lng": "10", "geo_radius_m": "5000"}):
        resp = client.post("/labs/new", data={**base, **bad})
        assert resp.status_code == 200, bad
    with app.app_context():
        assert Lab.query.filter_by(name="定位实验室").count() == 0
    ok = client.post("/labs/new", data={**base, "geo_lat": "31.230416", "geo_lng": "121.473701", "geo_radius_m": "45", "geo_required": "on"})
    assert ok.status_code == 302
    with app.app_context():
        lab = Lab.query.filter_by(name="定位实验室").one()
        assert (lab.geo_lat, lab.geo_lng, lab.geo_radius_m, lab.geo_required, lab.geo_active) == (31.230416, 121.473701, 45, True, True)
        lab_id = lab.id
    form = client.get(f"/labs/{lab_id}/edit").get_data(as_text=True)
    assert 'value="31.230416"' in form and 'value="45"' in form and "使用当前位置" in form and "checked" in form
    client.post(f"/labs/{lab_id}/edit", data={**base, "geo_lat": "31.230416", "geo_lng": "121.473701", "geo_radius_m": "45"})
    with app.app_context():
        assert db.session.get(Lab, lab_id).geo_required is False
    assert "开启" not in client.get("/labs/").get_data(as_text=True).split("定位实验室")[1].split("</tr>")[0]


def test_new_columns_added_to_old_database(app):
    from sqlalchemy import inspect, text

    from app import _ensure_checkin_schema

    with app.app_context():
        for col in ("pledge_accepted_at", "checkin_lat", "checkin_lng", "checkin_accuracy", "checkin_distance_m"):
            db.session.execute(text(f"ALTER TABLE bookings DROP COLUMN {col}"))
        for col in ("geo_lat", "geo_lng", "geo_radius_m", "geo_required"):
            db.session.execute(text(f"ALTER TABLE labs DROP COLUMN {col}"))
        db.session.commit()
        _ensure_checkin_schema()
        names = {c["name"] for c in inspect(db.engine).get_columns("labs")} | {c["name"] for c in inspect(db.engine).get_columns("bookings")}
        assert {"geo_lat", "geo_lng", "geo_radius_m", "geo_required", "pledge_accepted_at", "checkin_lat", "checkin_distance_m"} <= names


# ------------------------------------------------------------------ scanner button + vendored library

def test_button_and_modal_only_inside_window(app, client, seed, freeze):
    bid = _approved(app, seed)  # 10:00-11:00, window opens 09:45
    login(client, "2021001")
    freeze(8, 0)
    early = client.get("/captain").get_data(as_text=True)
    assert "二维码签到 · 09:45 开放" in early and "ck-open-btn" not in early and 'id="ck-modal"' not in early
    freeze(9, 50)
    for url in ("/captain", "/bookings"):
        text = client.get(url).get_data(as_text=True)
        assert 'class="btn ck-open-btn"' in text and f'data-booking-id="{bid}"' in text and ">二维码签到</button>" in text
        assert 'id="ck-modal"' in text and CHECKIN_PLEDGE_TITLE in text and CHECKIN_PLEDGE_CHECKBOX in text
        assert all(item in text for item in CHECKIN_PLEDGE)
        assert 'id="ck-start" disabled' in text and 'id="ck-cancel"' in text
        assert 'accept="image/*" capture="environment"' in text
        assert "vendor/jsQR.js" in text and "checkin.js" in text and "http://" not in text.split('id="ck-modal"')[1] and "cdn" not in text.lower()
    _post(client, _token(app, seed["lab_id"]), pledge="1")
    done = client.get("/captain").get_data(as_text=True)
    assert "ck-open-btn" not in done and "已签到" in done
    freeze(12, 0)
    assert "ck-open-btn" not in client.get("/captain").get_data(as_text=True)


def test_scanner_assets_are_local_with_license_and_no_old_nav_entry(app, client, seed):
    login(client, "2021001")
    js = client.get("/static/vendor/jsQR.js")
    assert js.status_code == 200 and b"jsQR" in js.data
    assert client.get("/static/checkin.js").status_code == 200
    lic = client.get("/static/vendor/jsQR.LICENSE.txt").get_data(as_text=True)
    assert "Apache License" in lic
    page = client.get("/captain").get_data(as_text=True)
    assert "队长签到" not in page and "unpkg" not in page and "jsdelivr" not in page
    src = open("app/static/checkin.js", encoding="utf-8").read()
    assert "facingMode" in src and "environment" in src and "pledge" in src and "getCurrentPosition" in src and "enableHighAccuracy" in src


# ------------------------------------------------------------------ auto refresh meta

def test_auto_refresh_meta_present_and_configurable(app, client, seed):
    login(client, "2021001")
    page = client.get("/captain").get_data(as_text=True)
    assert '<meta name="auto-refresh-seconds" content="30">' in page
    assert '<meta name="request-method" content="GET">' in page
    assert '<meta name="csrf-token"' in page
    assert '<meta name="auto-refresh-seconds" content="30">' in client.application.test_client().get("/login").get_data(as_text=True)
    app.config["AUTO_REFRESH_SECONDS"] = 0
    assert 'content="0"' in client.get("/captain").get_data(as_text=True).split('name="auto-refresh-seconds"')[1][:20]
    app.config["AUTO_REFRESH_SECONDS"] = 30
    boss = app.test_client()
    login(boss, "admin", SUPER_PASSWORD, staff=True)
    assert 'name="auto-refresh-seconds" content="30"' in boss.get("/").get_data(as_text=True)


def test_print_page_has_no_auto_refresh(app, client, seed):
    login(client, "admin", SUPER_PASSWORD, staff=True)
    page = client.get(f"/checkin-codes/{seed['lab_id']}/print").get_data(as_text=True)
    assert "auto-refresh-seconds" not in page and "app.js" not in page


# ------------------------------------------------------------------ login privacy notice

def test_privacy_notice_on_both_login_pages(client):
    assert PRIVACY_NOTICE == ("本预约系统对所有个人信息严格保密，仅用于本次竞赛的测试预约与签到管理，不会外传。"
                              "所有个人信息将在正式比赛结束后统一销毁。")
    for url in ("/login", "/admin/login"):
        html = client.get(url).get_data(as_text=True)
        assert PRIVACY_NOTICE in html and 'class="privacy-note"' in html and "<svg" in html
        assert html.index("</form>") < html.index(PRIVACY_NOTICE)


# ------------------------------------------------------------------ personal data wipe

PHRASE = "确定消除所有个人信息"


@pytest.fixture
def wipe_world(app, seed):
    """Personal data everywhere a captain could have left it."""
    from pathlib import Path

    from app.activity import record_audit
    from app.models import (
        AnnouncementMail, BookingCompanion, LoginLog, NoShowAlert, PasswordResetLog, SecurityEvent, TeamMember, ToolLoan,
    )

    with app.app_context():
        member = db.session.get(User, seed["member_id"])
        member.team_name, member.last_phone, member.last_email = "星河战队", "13700001111", "zhangsan@example.com"
        member.contact_phone, member.organization, member.special_reason = "13700002222", "土木学院", "特批原因张三"
        lab = db.session.get(Lab, seed["lab_id"])
        booking = add_booking(lab, member, purpose="张三的悬臂梁测试", status="approved")
        booking.checkin_ip, booking.checkin_ua = "203.0.113.9", "PhoneUA/1.0"
        booking.checked_in_at, booking.pledge_accepted_at = datetime(2099, 6, 15, 10, 5), datetime(2099, 6, 15, 10, 5)
        booking.checkin_lat, booking.checkin_lng, booking.checkin_accuracy, booking.checkin_distance_m = 31.1234567, 121.7654321, 8.0, 12.0
        booking.reject_reason = "王五请补充"
        db.session.add(BookingCompanion(booking_id=booking.id, source="manual", name="赵六", student_id="20219999", contact="13912345678"))
        db.session.add(TeamMember(captain_id=member.id, name="钱七", student_id="20218888", phone="13600001111"))
        db.session.add(MailLog(recipient="zhangsan@example.com", subject="张三的通知", body="你好张三 13700001111", status="local", booking_id=booking.id))
        db.session.add(NoShowAlert(user_id=member.id, level=1, ack_ip="203.0.113.10", email_to="zhangsan@example.com"))
        db.session.add(PasswordResetLog(student_id="2021001", ip="203.0.113.11", success=False))
        db.session.add(LoginLog(user_id=member.id, student_id="2021001", role="member", ip="203.0.113.12", user_agent="PhoneUA/1.0",
                                device_id="dev-zhangsan", success=True))
        db.session.add(SecurityEvent(event_type="scan_404", severity="low", ip="203.0.113.13", student_id="2021001",
                                     user_agent="PhoneUA/1.0", detail="2021001 张三 访问", path="/x", query_summary="sid=2021001"))
        loan = ToolLoan(user_id=member.id, lab_id=lab.id, team_name="星河战队", contact="13700003333",
                        borrow_at=datetime(2099, 6, 15, 9, 0), expected_return_at=datetime(2099, 6, 15, 12, 0),
                        borrow_photo="photo-张三.jpg", status="borrowed")
        db.session.add(loan)
        db.session.commit()
        record_audit("booking.create", summary="张三（2021001）提交了预约，电话 13700001111", target_type="booking", target_id=booking.id,
                     payload={"name": "张三", "email": "zhangsan@example.com"})
        upload_dir, outbox_dir = Path(app.config["UPLOAD_DIR"]), Path(app.config["OUTBOX_DIR"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "photo-张三.jpg").write_bytes(b"\xff\xd8photo")
        (outbox_dir / "mail-zhangsan.eml").write_text("To: zhangsan@example.com\n张三", encoding="utf-8")
        exports = Path(app.config["DATA_DIR"]) / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        (exports / "all.xlsx").write_bytes("PK张三".encode())
        app.config["BACKUP_DIR"] = str(app.config["DATA_DIR"]) + "/../backups"
        return {"upload": upload_dir / "photo-张三.jpg", "outbox": outbox_dir / "mail-zhangsan.eml", "export": exports / "all.xlsx"}


def _super(app):
    client = app.test_client()
    login(client, "admin", SUPER_PASSWORD, staff=True)
    return client


def _dump(app):
    con = sqlite3.connect(app.config["DB_PATH"])
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def test_wipe_page_super_admin_only(app, client, seed):
    assert client.get("/security/wipe").status_code in (302, 401, 403)
    login(client, "2021001")
    assert client.get("/security/wipe").status_code in (302, 403)
    admin = app.test_client()
    login(admin, "officer01", ADMIN_PASSWORD, staff=True)
    assert admin.get("/security/wipe").status_code == 403
    assert admin.post("/security/wipe", data={"phrase": PHRASE, "password": ADMIN_PASSWORD}).status_code == 403
    assert admin.get("/security/wipe/stats.xlsx").status_code == 403
    boss = _super(app)
    text = boss.get("/security/wipe").get_data(as_text=True)
    assert PHRASE in text and "将被清除" in text and "会保留" in text and "匿名统计" in text
    assert "/security/wipe" in boss.get("/security/").get_data(as_text=True)


def test_wipe_rejects_wrong_phrase_and_wrong_password_and_changes_nothing(app, seed, wipe_world):
    boss = _super(app)
    before = _dump(app)
    for phrase in ("", "确定消除所有个人信息。", PHRASE + "x"):   # surrounding whitespace is stripped; nothing else is
        resp = boss.post("/security/wipe", data={"phrase": phrase, "password": SUPER_PASSWORD})
        assert resp.status_code == 400 and "确认文字不正确" in resp.get_data(as_text=True)
    bad = boss.post("/security/wipe", data={"phrase": PHRASE, "password": "wrong-password"})
    assert bad.status_code == 400 and "密码错误" in bad.get_data(as_text=True)
    assert boss.post("/security/wipe", data={"phrase": PHRASE, "password": ""}).status_code == 400
    with app.app_context():
        assert User.query.filter_by(student_id="2021001").count() == 1
        assert AuditLog.query.filter_by(action="wipe.attempt").count() == 5
        assert AuditLog.query.filter_by(action="wipe.done").count() == 0
    assert wipe_world["upload"].exists() and "张三" in _dump(app)


def test_wipe_failed_attempts_are_rate_limited(app, seed, wipe_world):
    boss = _super(app)
    for _ in range(5):
        assert boss.post("/security/wipe", data={"phrase": "x", "password": SUPER_PASSWORD}).status_code == 400
    locked = boss.post("/security/wipe", data={"phrase": PHRASE, "password": SUPER_PASSWORD})
    assert locked.status_code == 429
    with app.app_context():
        assert User.query.filter_by(student_id="2021001").count() == 1


def test_successful_wipe_leaves_no_personal_strings(app, seed, wipe_world, tmp_path):
    from pathlib import Path

    # Another captain logs in so there is a login log with a typed student number.
    other = app.test_client()
    login(other, "2021002")
    app.test_client().post("/login", data={"student_id": "2021999", "password": "nope"})
    boss = _super(app)
    resp = boss.post("/security/wipe", data={"phrase": f"  {PHRASE}\n", "password": SUPER_PASSWORD})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    assert "个人信息已全部清除" in resp.get_data(as_text=True)
    dump = _dump(app)
    needles = ["张三", "王五", "赵六", "钱七", "2021001", "2021002", "2021999", "20219999", "20218888", CAPTAIN_PHONE, CAPTAIN_EMAIL,
               "13700001111", "13700002222", "13700003333", "13912345678", "13600001111", "zhangsan@example.com", "星河战队", "土木学院",
               "悬臂梁", "203.0.113.", "PhoneUA", "dev-zhangsan", "31.1234567", "121.7654321", "photo-张三", "特批原因", "请补充"]
    leaked = [n for n in needles if n in dump]
    assert not leaked, leaked
    # files
    assert not wipe_world["upload"].exists() and not wipe_world["outbox"].exists() and not wipe_world["export"].exists()
    assert list(Path(app.config["UPLOAD_DIR"]).iterdir()) == []
    # final backup holds the pre-wipe data
    backups = list(Path(app.config["BACKUP_DIR"]).glob("app-before-wipe-*.db"))
    assert len(backups) == 1
    con = sqlite3.connect(backups[0])
    assert con.execute("SELECT count(*) FROM users WHERE name='张三'").fetchone()[0] == 1
    con.close()
    # database state
    with app.app_context():
        members = User.query.filter(User.role == "member").order_by(User.id).all()
        assert [u.student_id for u in members] == ["已注销-0001", "已注销-0002"]
        assert all(not u.enabled and u.team_name is None and u.last_email is None for u in members)
        assert User.query.filter(User.role.in_(["admin", "superadmin"])).count() == 2
        assert Lab.query.count() == 1 and MailLog.query.count() == 0
        booking = Booking.query.one()
        assert (booking.date, booking.status, booking.people_count) == (FUTURE_DAY, "approved", 2)
        assert booking.checked_in_at == datetime(2099, 6, 15, 10, 5) and booking.pledge_accepted_at == datetime(2099, 6, 15, 10, 5)
        assert booking.checkin_ip is None and booking.checkin_lat is None and booking.captain_phone is None
        done = AuditLog.query.filter_by(action="wipe.done").one()
        assert done.actor_student_id == "admin" and "accounts_anonymised=2" in done.summary and "app-before-wipe-" in done.summary
        assert SUPER_PASSWORD not in done.summary and SUPER_PASSWORD not in (done.payload or "")
    assert SUPER_PASSWORD not in dump


def test_after_wipe_admins_log_in_stats_render_and_captains_cannot(app, seed, wipe_world):
    boss = _super(app)
    assert boss.post("/security/wipe", data={"phrase": PHRASE, "password": SUPER_PASSWORD}).status_code == 200
    fresh = app.test_client()
    assert login(fresh, "admin", SUPER_PASSWORD, staff=True).status_code == 200
    assert fresh.get("/stats/").status_code == 200
    assert fresh.get("/security/").status_code == 200
    assert fresh.get("/review/?status=approved").status_code == 200
    officer = app.test_client()
    assert officer.get("/stats/", follow_redirects=False).status_code in (302, 401)  # not logged in yet
    assert "工作台" in login(officer, "officer01", ADMIN_PASSWORD, staff=True).get_data(as_text=True)
    assert officer.get("/review/?status=approved").status_code == 200
    # The old captain account is disabled and its student number is gone.
    old_captain = app.test_client()
    login(old_captain, "2021001")
    assert old_captain.get("/captain", follow_redirects=False).status_code in (302, 401, 403)
    anon = app.test_client()
    anon.post("/login", data={"student_id": "已注销-0001", "password": "2021001"})
    assert anon.get("/captain", follow_redirects=False).status_code in (302, 401, 403)


def test_wipe_stats_excel_is_anonymous(app, seed, wipe_world):
    from io import BytesIO

    from openpyxl import load_workbook

    boss = _super(app)
    resp = boss.get("/security/wipe/stats.xlsx")
    assert resp.status_code == 200 and "spreadsheetml" in resp.mimetype
    sheet = load_workbook(BytesIO(resp.data)).active
    cells = [str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None]
    blob = " ".join(cells)
    assert "创新实验室" in blob and "队伍001" in blob
    for secret in ("张三", "2021001", CAPTAIN_PHONE, CAPTAIN_EMAIL, "悬臂梁", "31.12"):
        assert secret not in blob


def test_wipe_failure_rolls_back_and_keeps_data(app, seed, wipe_world, monkeypatch):
    boss = _super(app)

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.wipe.wipe_personal_data", boom)
    resp = boss.post("/security/wipe", data={"phrase": PHRASE, "password": SUPER_PASSWORD})
    assert resp.status_code == 500 and "回滚" in resp.get_data(as_text=True)
    assert wipe_world["upload"].exists()
    with app.app_context():
        assert User.query.filter_by(student_id="2021001").count() == 1
