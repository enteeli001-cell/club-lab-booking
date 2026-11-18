from app import db
from app.models import User
from app.utils import mask_person_name
from tests.conftest import STRONG_PASSWORD, login


def test_mask_person_name():
    assert mask_person_name("张三") == "张*三"
    assert mask_person_name("王小明") == "王*明"
    assert mask_person_name("李") == "李*"
    assert mask_person_name("  ") == ""


def test_lookup_shows_masked_name_for_captain(app, client, seed):
    resp = client.get("/login/captain-name?student_id=2021001")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["found"] is True
    assert data["greeting"] == "张*三 同学，您好"
    assert "仅允许本人登录和预约，请勿代他人操作" in data["hint"]
    # do not leak the full unmasked name
    assert "张三" not in data["greeting"]


def test_lookup_missing_or_staff_is_not_found(app, client, seed):
    missing = client.get("/login/captain-name?student_id=no-such-id")
    assert missing.get_json()["found"] is False
    assert missing.get_json()["error"] == "未找到该队长账号"

    staff = client.get("/login/captain-name?student_id=admin")
    assert staff.get_json()["found"] is False
    assert staff.get_json()["error"] == "未找到该队长账号"

    officer = client.get("/login/captain-name?student_id=officer01")
    assert officer.get_json()["found"] is False


def test_lookup_rate_limited_per_ip(app, client, seed):
    app.config["CAPTAIN_NAME_LOOKUP_LIMIT"] = 3
    app.config["CAPTAIN_NAME_LOOKUP_WINDOW"] = 60
    statuses = [
        client.get("/login/captain-name?student_id=2021001").status_code
        for _ in range(4)
    ]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert "频繁" in client.get("/login/captain-name?student_id=2021001").get_json()["error"]


def test_initial_password_login_forces_change(app, client, seed):
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        user.set_password("2021001", require_change=True)
        db.session.commit()

    resp = login(client, "2021001", "2021001")
    assert resp.status_code == 200
    assert "请先修改初始密码".encode("utf-8") in resp.data
    # cannot use other features yet
    blocked = client.get("/bookings", follow_redirects=False)
    assert blocked.status_code == 302
    assert "/password" in blocked.headers["Location"]

    ok = client.post(
        "/password",
        data={
            "current_password": "2021001",
            "new_password": STRONG_PASSWORD,
            "confirm_password": STRONG_PASSWORD,
        },
        follow_redirects=True,
    )
    assert ok.status_code == 200
    assert client.get("/bookings").status_code == 200
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert user.must_change_password is False
        assert user.check_password(STRONG_PASSWORD)


def test_admin_can_reset_captain_password_to_student_id(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post(f"/users/{seed['member_id']}/reset-password", follow_redirects=True)
    assert resp.status_code == 200
    assert "重置为学号".encode("utf-8") in resp.data
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert user.check_password("2021001")
        assert user.must_change_password is True

    # regular admin cannot reset anyone
    client.post("/logout")
    login(client, "officer01", staff=True)
    assert client.post(f"/users/{seed['other_id']}/reset-password").status_code == 403
