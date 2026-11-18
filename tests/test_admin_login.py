from app import db
from app.models import User
from app.utils import staff_password_is_unique
from tests.conftest import ADMIN_PASSWORD, STAFF_NEW_PASSWORD, SUPER_PASSWORD, login


def test_admin_login_page_has_password_only(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "管理员登录" in html
    assert 'name="password"' in html
    assert 'name="student_id"' not in html
    assert "不提供忘记密码自助找回" in html
    assert "自助找回" in html
    # legacy path redirects
    legacy = client.get("/login/admin", follow_redirects=False)
    assert legacy.status_code in (301, 302)
    assert "/admin/login" in legacy.headers["Location"]


def test_admin_login_by_password_only(app, client, seed):
    # superadmin
    resp = client.post("/admin/login", data={"password": SUPER_PASSWORD}, follow_redirects=True)
    assert resp.status_code == 200
    assert "总管理员".encode("utf-8") in resp.data
    assert "管理后台".encode("utf-8") in resp.data
    client.post("/logout")
    # regular admin
    resp = login(client, "officer01", staff=True)
    assert "李管理员".encode("utf-8") in resp.data
    assert "管理员".encode("utf-8") in resp.data


def test_wrong_admin_password(client, seed):
    resp = client.post("/admin/login", data={"password": "definitely-wrong"}, follow_redirects=True)
    assert "密码不正确".encode("utf-8") in resp.data


def test_admin_login_lockout_after_failures(app, client, seed):
    app.config["ADMIN_LOGIN_MAX_FAILURES"] = 5
    app.config["ADMIN_LOGIN_LOCK_SECONDS"] = 60
    for _ in range(5):
        resp = client.post("/admin/login", data={"password": "bad-password"})
    assert resp.status_code == 429
    html = resp.data.decode("utf-8")
    assert "失败次数过多" in html
    assert 'id="lock-remaining"' in html
    assert "剩余冻结时间" in html
    assert ">60</span> 秒" in html or ">59</span> 秒" in html
    # even the correct password is blocked while locked
    locked = client.post("/admin/login", data={"password": SUPER_PASSWORD})
    assert locked.status_code == 429
    assert "失败次数过多".encode("utf-8") in locked.data
    assert b'id="lock-remaining"' in locked.data
    # captain entrance stays available
    cap = client.post(
        "/login",
        data={"student_id": "2021001", "password": "password123"},
        follow_redirects=True,
    )
    assert cap.status_code == 200
    body = cap.data.decode("utf-8")
    assert "该 IP 已被暂时冻结" not in body
    assert "退出" in body or "张三" in body or "预约" in body


def test_regular_admin_cannot_manage_accounts_but_can_labs(app, client, seed):
    login(client, "officer01", staff=True)
    assert client.get("/users/").status_code == 403
    assert client.get("/users/new").status_code == 403
    assert client.post("/users/new", data={"name": "x", "student_id": "y", "role": "member"}).status_code == 403
    assert client.get("/labs/").status_code == 200
    assert client.get("/review/").status_code == 200
    assert client.get("/stats/").status_code == 200


def test_superadmin_can_manage_accounts(app, client, seed):
    login(client, "admin", staff=True)
    page = client.get("/users/")
    assert page.status_code == 200
    assert "账号管理".encode("utf-8") in page.data
    resp = client.post(
        "/users/new",
        data={"name": "新管理员", "student_id": "mgr01", "role": "admin", "password": STAFF_NEW_PASSWORD},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "请立即抄录".encode("utf-8") in resp.data
    with app.app_context():
        user = User.query.filter_by(student_id="mgr01").one()
        assert user.role == "admin"
        assert user.check_password(STAFF_NEW_PASSWORD)
        assert user.must_change_password is False


def test_issued_admin_password_must_be_unique(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post(
        "/users/new",
        data={"name": "冲突管理员", "student_id": "mgr02", "role": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=True,
    )
    assert "与其他管理员的密码相同".encode("utf-8") in resp.data
    with app.app_context():
        assert User.query.filter_by(student_id="mgr02").first() is None
        assert staff_password_is_unique(ADMIN_PASSWORD) is False


def test_admin_cannot_change_own_password(app, client, seed):
    login(client, "officer01", staff=True)
    page = client.get("/review/")
    assert "修改密码".encode("utf-8") not in page.data
    resp = client.post(
        "/password",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_password": "Brand#NewAdmin2026x",
            "confirm_password": "Brand#NewAdmin2026x",
        },
        follow_redirects=True,
    )
    assert "管理员不能自行修改密码".encode("utf-8") in resp.data
    with app.app_context():
        user = db.session.get(User, seed["officer_id"])
        assert user.check_password(ADMIN_PASSWORD)


def test_issued_admin_not_forced_to_change_password(app, client, seed):
    with app.app_context():
        user = db.session.get(User, seed["officer_id"])
        user.set_password(ADMIN_PASSWORD, require_change=True)
        db.session.commit()
        assert user.must_change_password is False
    login(client, "officer01", staff=True)
    page = client.get("/review/", follow_redirects=False)
    assert page.status_code == 200

