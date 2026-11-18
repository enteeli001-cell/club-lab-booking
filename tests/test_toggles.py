"""Captain feature switches and maintenance mode (总管理员 only)."""
from datetime import time

import pytest

from app import create_app, db, toggles
from app.models import AppSetting, AuditLog, Booking, Lab, User
from tests.conftest import (
    API_TOKEN,
    FUTURE_DAY,
    add_booking,
    api_headers,
    booking_form,
    login,
)

DISABLED = "该功能已被管理员暂时关闭"
UPGRADING = "网站正在升级，请稍后再访问"
BLOCK_PAGE = "<p>该功能已被管理员暂时关闭。</p>"  # the error page body (the greyed-out nav only carries it as a tooltip)


def set_feature(app, key, enabled):
    with app.app_context():
        toggles.set_feature(key, enabled)


def set_maintenance(app, enabled):
    with app.app_context():
        toggles.set_maintenance(enabled)


def text(resp):
    return resp.get_data(as_text=True)


@pytest.fixture
def booking_id(app, seed):
    with app.app_context():
        b = add_booking(db.session.get(Lab, seed["lab_id"]), db.session.get(User, seed["member_id"]))
        return b.id


# ------------------------------------------------------------------ defaults / migration

def test_defaults_all_features_on_and_maintenance_off(app):
    with app.app_context():
        assert all(toggles.feature_states().values())
        assert set(toggles.feature_states()) == set(toggles.FEATURE_KEYS)
        assert toggles.maintenance_enabled() is False
        assert toggles.maintenance_message() == UPGRADING
        for key in toggles.FEATURE_KEYS:
            assert db.session.get(AppSetting, "feature." + key).value == "1"
        assert db.session.get(AppSetting, "maintenance.enabled").value == "0"


def test_migration_is_additive_and_keeps_existing_values(app):
    with app.app_context():
        toggles.set_feature("schedule", False)
        toggles.set_maintenance_message("自定义文案")
        db.session.delete(db.session.get(AppSetting, "feature.checkin"))
        db.session.commit()
        toggles.ensure_toggle_settings()
        toggles.ensure_toggle_settings()
        assert db.session.get(AppSetting, "feature.schedule").value == "0"
        assert db.session.get(AppSetting, "feature.checkin").value == "1"
        assert db.session.get(AppSetting, "maintenance.message").value == "自定义文案"


# ------------------------------------------------------------------ feature switches: server side

def _captain_requests(seed, booking_id):
    """feature key -> list of (method, path, data) a logged-in captain would send."""
    return {
        "booking_create": [("GET", "/bookings/new", None), ("POST", "/bookings/new", booking_form(seed["lab_id"], start_time="14:00", end_time="15:00"))],
        "booking_edit": [("GET", f"/bookings/{booking_id}/edit", None), ("POST", f"/bookings/{booking_id}/edit", booking_form(seed["lab_id"], purpose="改过的用途"))],
        "booking_cancel": [("POST", f"/bookings/{booking_id}/cancel", {})],
        "records": [("GET", "/bookings", None), ("GET", "/records", None)],
        "schedule": [("GET", "/schedule", None)],
        "checkin": [("GET", "/checkin/TOKEN", None), ("POST", "/checkin/TOKEN", {})],
        "announcements": [("GET", "/announcements/", None), ("GET", "/announcements/1", None), ("POST", "/announcements/ack", {})],
        "tool_borrow": [("GET", "/tools", None), ("GET", "/tools/new", None), ("POST", "/tools/new", {})],
        "tool_return": [("GET", "/tools/1/return", None), ("POST", "/tools/1/return", {})],
        "change_password": [("GET", "/password", None), ("POST", "/password", {"current_password": "password123", "new_password": "Valid#1a", "confirm_password": "Valid#1a"})],
    }


def _send(client, method, path, data):
    return client.get(path) if method == "GET" else client.post(path, data=data)


ENDPOINT_FEATURE_KEYS = ["booking_create", "booking_edit", "booking_cancel", "records", "schedule", "checkin",
                         "announcements", "tool_borrow", "tool_return", "change_password"]


@pytest.mark.parametrize("key", ENDPOINT_FEATURE_KEYS)
def test_feature_off_blocks_captain_on_server(app, client, seed, booking_id, key):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        token_path = f"/checkin/{lab.checkin_token}"
        member = db.session.get(User, seed["member_id"])
        member.role = "member"
        db.session.commit()
    login(client, "2021001")
    requests = [(m, token_path if p == "/checkin/TOKEN" else p, d) for m, p, d in _captain_requests(seed, booking_id)[key]]

    set_feature(app, key, False)
    for method, path, data in requests:
        resp = _send(client, method, path, data)
        assert resp.status_code == 403, (method, path)
        assert BLOCK_PAGE in text(resp), (method, path)
    with app.app_context():  # nothing was written while blocked
        b = db.session.get(Booking, booking_id)
        assert b.status == "pending" and b.purpose == "活动筹备"
        assert Booking.query.count() == 1
        assert db.session.get(User, seed["member_id"]).check_password("password123")

    # every other feature keeps working for the captain
    for other, reqs in _captain_requests(seed, booking_id).items():
        if other == key:
            continue
        method, path, data = reqs[0]
        if method != "GET":
            continue
        resp = client.get(token_path if path == "/checkin/TOKEN" else path)
        assert BLOCK_PAGE not in text(resp), (other, path)

    set_feature(app, key, True)  # recovery
    method, path, data = requests[0]
    resp = _send(client, method, path, data)
    assert BLOCK_PAGE not in text(resp)


@pytest.mark.parametrize("key", ENDPOINT_FEATURE_KEYS)
def test_feature_off_does_not_affect_staff(app, seed, booking_id, key):
    set_feature(app, key, False)
    for student_id in ("admin", "officer01"):
        client = app.test_client()
        login(client, student_id, staff=True)
        for path in ("/", "/schedule", "/announcements/", "/review/", "/tools/admin", "/labs/"):
            resp = client.get(path)
            assert resp.status_code == 200, (student_id, path)
            assert BLOCK_PAGE not in text(resp)


def test_captain_home_greys_out_disabled_features(app, client, seed):
    login(client, "2021001")
    page = text(client.get("/captain"))
    assert 'class="nav-off"' not in page
    for key in ("booking_create", "records", "schedule", "announcements", "change_password"):
        set_feature(app, key, False)
    resp = client.get("/captain")
    assert resp.status_code == 200
    page = text(resp)
    assert page.count('class="nav-off"') >= 5
    assert 'href="/schedule"' not in page.split("</header>")[0]
    assert DISABLED in page


def test_announcement_popup_suppressed_when_feature_off(app, client, seed):
    from app.models import Announcement

    with app.app_context():
        db.session.add(Announcement(title="重要通知", body="正文", level=3, pinned=False, created_by_id=seed["admin_id"]))
        db.session.commit()
    login(client, "2021001")
    assert 'id="announce-overlay"' in text(client.get("/captain"))
    set_feature(app, "announcements", False)
    assert 'id="announce-overlay"' not in text(client.get("/captain"))
    staff = app.test_client()
    login(staff, "officer01", staff=True)
    assert staff.get("/announcements/").status_code == 200


def test_forced_password_change_still_possible_when_change_password_off(app, client, seed):
    with app.app_context():
        db.session.get(User, seed["member_id"]).must_change_password = True
        db.session.commit()
    set_feature(app, "change_password", False)
    login(client, "2021001")
    resp = client.get("/password")
    assert resp.status_code == 200
    assert BLOCK_PAGE not in text(resp)


def test_captain_login_off_blocks_login_and_ends_sessions(app, client, seed):
    login(client, "2021001")
    assert client.get("/captain").status_code == 200
    set_feature(app, "captain_login", False)

    resp = client.get("/captain")  # existing session is ended
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/captain").status_code == 302

    resp = client.get("/login")
    assert resp.status_code == 200 and DISABLED in text(resp)
    resp = client.post("/login", data={"student_id": "2021001", "password": "password123"})
    assert resp.status_code == 403 and DISABLED in text(resp)
    assert client.get("/captain").status_code == 302  # still not logged in

    for student_id in ("admin", "officer01"):  # staff entrance untouched
        staff = app.test_client()
        resp = login(staff, student_id, staff=True)
        assert resp.status_code == 200 and "管理工作台" in text(resp)

    set_feature(app, "captain_login", True)
    resp = login(client, "2021001")
    assert resp.status_code == 200 and "欢迎你进入测试预约平台" in text(resp)


def test_forgot_password_off_blocks_get_and_post(app, client, seed):
    assert client.get("/forgot-password").status_code == 200
    set_feature(app, "forgot_password", False)
    resp = client.get("/forgot-password")
    assert resp.status_code == 403 and DISABLED in text(resp)
    resp = client.post("/forgot-password", data={"name": "张三", "student_id": "2021001"})
    assert resp.status_code == 403 and DISABLED in text(resp)
    set_feature(app, "forgot_password", True)
    assert client.get("/forgot-password").status_code == 200


def test_api_respects_feature_switches(app, client, seed, booking_id):
    payload = {"lab_id": seed["lab_id"], "date": FUTURE_DAY.isoformat(), "start_time": "15:00", "end_time": "16:00",
               "purpose": "API 预约", "people_count": 1, "captain_phone": "13800138000", "captain_email": "captain@example.com"}
    set_feature(app, "booking_create", False)
    resp = client.post("/api/bookings", json=payload, headers=api_headers("2021001"))
    assert resp.status_code == 403 and DISABLED in resp.get_json()["error"]
    resp = client.post("/api/conflicts", json=payload, headers=api_headers("2021001"))
    assert resp.status_code == 403
    assert client.get("/api/bookings", headers=api_headers("2021001")).status_code == 200  # other features fine
    set_feature(app, "booking_create", True)

    set_feature(app, "records", False)
    assert client.get("/api/bookings", headers=api_headers("2021001")).status_code == 403
    assert client.get(f"/api/bookings/{booking_id}", headers=api_headers("2021001")).status_code == 403
    assert client.get("/api/bookings", headers=api_headers("officer01")).status_code == 200  # staff unaffected
    assert client.get("/api/bookings", headers=api_headers("admin")).status_code == 200
    set_feature(app, "records", True)

    set_feature(app, "booking_cancel", False)
    resp = client.delete(f"/api/bookings/{booking_id}", headers=api_headers("2021001"))
    assert resp.status_code == 403 and DISABLED in resp.get_json()["error"]
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "pending"
    set_feature(app, "booking_cancel", True)

    set_feature(app, "captain_login", False)
    assert client.get("/api/me", headers=api_headers("2021001")).status_code == 403
    assert client.get("/api/me", headers=api_headers("officer01")).status_code == 200
    set_feature(app, "captain_login", True)
    assert client.get("/api/me", headers=api_headers("2021001")).status_code == 200
    assert client.delete(f"/api/bookings/{booking_id}", headers=api_headers("2021001")).status_code == 200


# ------------------------------------------------------------------ superadmin pages

def test_toggle_pages_are_superadmin_only(app, client, seed):
    for path in ("/switches/", "/switches/maintenance"):
        resp = client.get(path)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    for student_id, staff in (("2021001", False), ("officer01", True)):
        c = app.test_client()
        login(c, student_id, staff=staff)
        assert c.get("/switches/").status_code == 403
        assert c.get("/switches/maintenance").status_code == 403
        assert c.post("/switches/features/schedule", data={"enabled": "0"}).status_code == 403
        assert c.post("/switches/maintenance/state", data={"enabled": "1", "confirm": "yes"}).status_code == 403
        assert c.post("/switches/maintenance/message", data={"message": "x"}).status_code == 403
        assert "功能开关" not in text(c.get("/", follow_redirects=True))
    with app.app_context():
        assert toggles.feature_enabled("schedule") and not toggles.maintenance_enabled()


def test_superadmin_toggles_feature_with_audit_log(app, client, seed):
    login(client, "admin", staff=True)
    page = text(client.get("/switches/"))
    for _key, label, _desc in toggles.FEATURES:
        assert label in page
    assert "全部开启" in page and 'name="csrf_token"' in page

    resp = client.post("/switches/features/schedule", data={"enabled": "0"}, follow_redirects=True)
    assert resp.status_code == 200 and "已关闭「占用视图」" in text(resp) and "已关闭 1 项" in text(resp)
    with app.app_context():
        assert not toggles.feature_enabled("schedule")
        log = AuditLog.query.filter_by(action="toggle.feature").one()
        assert "关闭" in log.summary and "占用视图" in log.summary and log.actor_student_id == "admin"
    assert client.get("/schedule").status_code == 200  # the superadmin is not affected

    client.post("/switches/features/schedule", data={"enabled": "1"})
    with app.app_context():
        assert toggles.feature_enabled("schedule")
        assert AuditLog.query.filter_by(action="toggle.feature").count() == 2

    assert "未知的功能开关" in text(client.post("/switches/features/nope", data={"enabled": "0"}, follow_redirects=True))
    assert "请求无效" in text(client.post("/switches/features/schedule", data={"enabled": "x"}, follow_redirects=True))
    with app.app_context():
        assert AuditLog.query.filter_by(action="toggle.feature").count() == 2
    assert client.get("/activity/admins").status_code == 200


def test_toggle_posts_require_csrf(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path / "data"), "LOG_DIR": str(tmp_path / "logs"),
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'csrf.db'}", "SECRET_KEY": "k", "API_TOKEN": API_TOKEN})
    with app.app_context():
        user = User(student_id="admin", name="总管理员", role="superadmin")
        user.set_password("SuperAdmin#12x", require_change=False)
        db.session.add(user)
        db.session.commit()
        uid = user.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    assert client.get("/switches/").status_code == 200
    assert client.post("/switches/features/schedule", data={"enabled": "0"}).status_code == 400
    assert client.post("/switches/maintenance/state", data={"enabled": "1", "confirm": "yes"}).status_code == 400
    assert client.post("/switches/maintenance/message", data={"message": "x"}).status_code == 400
    with app.app_context():
        assert toggles.feature_enabled("schedule") and not toggles.maintenance_enabled()
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', text(client.get("/switches/"))).group(1)
    assert client.post("/switches/features/schedule", data={"enabled": "0", "csrf_token": token}).status_code == 302
    with app.app_context():
        assert not toggles.feature_enabled("schedule")


# ------------------------------------------------------------------ maintenance mode

def test_enabling_maintenance_needs_second_confirmation(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post("/switches/maintenance/state", data={"enabled": "1"}, follow_redirects=True)
    assert "二次确认" in text(resp)
    with app.app_context():
        assert not toggles.maintenance_enabled()
        assert AuditLog.query.filter(AuditLog.action.like("maintenance.%")).count() == 0
    page = text(client.get("/switches/maintenance"))
    assert 'name="confirm"' in page and "confirm(" in page and "maint-banner" not in page

    resp = client.post("/switches/maintenance/state", data={"enabled": "1", "confirm": "yes"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert toggles.maintenance_enabled()
        assert AuditLog.query.filter_by(action="maintenance.on").count() == 1
    for path in ("/switches/maintenance", "/", "/switches/", "/review/"):  # prominent banner on superadmin pages
        resp = client.get(path)
        assert resp.status_code == 200 and "maint-banner" in text(resp) and "维护模式已开启" in text(resp)


def test_maintenance_blocks_everyone_but_superadmin(app, seed, booking_id):
    captain, officer, anon, boss = (app.test_client() for _ in range(4))
    login(captain, "2021001")
    login(officer, "officer01", staff=True)
    login(boss, "admin", staff=True)
    set_maintenance(app, True)

    paths = ["/", "/login", "/captain", "/bookings", "/bookings/new", "/schedule", "/review/", "/labs/", "/announcements/",
             "/forgot-password", "/password", "/switches/", "/no-such-page", "/robots.txt", "/login/captain-name?student_id=2021001"]
    for c in (captain, officer, anon):
        for path in paths:
            resp = c.get(path)
            assert resp.status_code == 503, path
            assert resp.headers["Retry-After"] == "600"
            assert UPGRADING in text(resp)
            assert "<nav" not in text(resp)
        resp = c.post("/login", data={"student_id": "2021001", "password": "password123"})
        assert resp.status_code == 503
        resp = c.post(f"/bookings/{booking_id}/cancel")
        assert resp.status_code == 503
        resp = c.get("/health")
        assert resp.status_code == 200 and text(resp) == "ok"
        assert c.get("/static/style.css").status_code == 200
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "pending"

    resp = anon.get("/api/bookings", headers=api_headers("2021001"))
    assert resp.status_code == 503 and resp.get_json()["error"] == UPGRADING and resp.headers["Retry-After"] == "600"
    assert anon.get("/api/health").status_code == 200

    for path in ("/", "/review/", "/schedule", "/switches/"):  # the superadmin keeps working
        assert boss.get(path).status_code == 200


def test_maintenance_superadmin_can_log_in_and_switch_off(app, seed):
    set_maintenance(app, True)
    client = app.test_client()
    resp = client.get("/admin/login")
    assert resp.status_code == 200 and 'name="password"' in text(resp)

    # a regular admin's correct password is refused while maintenance is on
    resp = client.post("/admin/login", data={"password": "LabAdmin#12yz"})
    assert resp.status_code == 503 and "只有总管理员可以登录" in text(resp)
    assert client.get("/").status_code == 503

    resp = client.post("/admin/login", data={"password": "SuperAdmin#12x"}, follow_redirects=True)
    assert resp.status_code == 200 and "管理工作台" in text(resp) and "maint-banner" in text(resp)
    resp = client.post("/switches/maintenance/state", data={"enabled": "0"}, follow_redirects=True)
    assert resp.status_code == 200 and "maint-banner" not in text(resp)
    with app.app_context():
        assert not toggles.maintenance_enabled()
        assert AuditLog.query.filter_by(action="maintenance.off").count() == 1

    # everything is back to normal for everyone
    anon = app.test_client()
    assert anon.get("/login").status_code == 200
    assert "Retry-After" not in anon.get("/login").headers
    captain = app.test_client()
    assert "欢迎你进入测试预约平台" in text(login(captain, "2021001"))
    officer = app.test_client()
    assert "管理工作台" in text(login(officer, "officer01", staff=True))


def test_maintenance_stale_captain_session_does_not_hide_admin_login(app, seed):
    client = app.test_client()
    login(client, "2021001")
    set_maintenance(app, True)
    resp = client.get("/admin/login")
    assert resp.status_code == 200 and 'name="password"' in text(resp)
    resp = client.post("/admin/login", data={"password": "SuperAdmin#12x"}, follow_redirects=True)
    assert resp.status_code == 200 and "管理工作台" in text(resp)


def test_maintenance_message_is_editable(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post("/switches/maintenance/message", data={"message": "  系统升级到 2.0，预计 22:00 恢复  "}, follow_redirects=True)
    assert "提示文案已保存" in text(resp)
    with app.app_context():
        assert toggles.maintenance_message() == "系统升级到 2.0，预计 22:00 恢复"
        assert AuditLog.query.filter_by(action="maintenance.message").count() == 1
    assert "不能为空" in text(client.post("/switches/maintenance/message", data={"message": "   "}, follow_redirects=True))
    assert "不能超过" in text(client.post("/switches/maintenance/message", data={"message": "长" * 201}, follow_redirects=True))
    with app.app_context():
        assert toggles.maintenance_message() == "系统升级到 2.0，预计 22:00 恢复"

    set_maintenance(app, True)
    anon = app.test_client()
    resp = anon.get("/login")
    assert resp.status_code == 503 and "系统升级到 2.0，预计 22:00 恢复" in text(resp)
    resp = anon.get("/captain", headers={"Accept": "application/json"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "maintenance"
