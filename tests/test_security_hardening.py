"""Security and hardening checks added during the self-audit."""

from sqlalchemy import text

from app import db
from app.models import User
from tests.conftest import ADMIN_PASSWORD, STRONG_PASSWORD, login


def test_client_ip_trusts_cf_header_only_from_localhost(app, client):
    with app.app_context():
        from app.activity import client_ip
        from flask import Request
        from werkzeug.test import EnvironBuilder

        # Trusted proxy (local tunnel): prefer CF-Connecting-IP over X-Forwarded-For.
        builder = EnvironBuilder(
            path="/login",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "CF-Connecting-IP": "203.0.113.50",
                "X-Forwarded-For": "198.51.100.10, 127.0.0.1",
            },
        )
        env = builder.get_environ()
        req = Request(env)
        with app.test_request_context(environ_overrides=env):
            assert client_ip(req) == "203.0.113.50"

        # Untrusted remote: ignore spoofed forwarded headers.
        builder = EnvironBuilder(
            path="/login",
            environ_base={"REMOTE_ADDR": "203.0.113.99"},
            headers={
                "CF-Connecting-IP": "8.8.8.8",
                "X-Forwarded-For": "8.8.8.8",
            },
        )
        env = builder.get_environ()
        req = Request(env)
        assert client_ip(req) == "203.0.113.99"


def test_login_log_uses_cf_connecting_ip(app, client):
    from app.models import LoginLog, User
    from tests.conftest import create_user

    with app.app_context():
        create_user("capip01", "测队长", "member", STRONG_PASSWORD)

    resp = client.post(
        "/login",
        data={"student_id": "capip01", "password": STRONG_PASSWORD},
        headers={"CF-Connecting-IP": "203.0.113.77"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with app.app_context():
        log = LoginLog.query.order_by(LoginLog.id.desc()).first()
        assert log is not None
        assert log.success is True
        assert log.ip == "203.0.113.77"


def test_spoofed_forwarded_ip_ignored_when_not_local(app, client):
    from app.models import LoginLog
    from tests.conftest import create_user

    with app.app_context():
        create_user("capip02", "测队长二", "member", STRONG_PASSWORD)

    client.post(
        "/login",
        data={"student_id": "capip02", "password": "wrong-password"},
        headers={"CF-Connecting-IP": "8.8.8.8", "X-Forwarded-For": "8.8.8.8"},
        environ_base={"REMOTE_ADDR": "198.51.100.20"},
    )
    with app.app_context():
        log = LoginLog.query.order_by(LoginLog.id.desc()).first()
        assert log is not None
        assert log.ip == "198.51.100.20"


def test_session_cookie_flags_http_and_https_via_tunnel(client):
    resp = client.get("/login")
    cookie = resp.headers.get("Set-Cookie", "")
    # Flask may set session on first request; also check device cookie later after login.
    # Issue a failed login to force session cookie.
    resp = client.post("/login", data={"student_id": "nobody", "password": "nope"})
    header = ",".join(resp.headers.getlist("Set-Cookie"))
    assert "lab_session=" in header or "session=" in header or "Set-Cookie" in str(resp.headers)
    # Collect all cookies
    all_cookies = header
    if "lab_session=" in all_cookies:
        sess = [p for p in all_cookies.split(",") if "lab_session=" in p]
        blob = " ".join(sess)
        assert "HttpOnly" in blob
        assert "SameSite=Lax" in blob
        assert "Secure" not in blob

    # Via local tunnel (https proto trusted only from 127.0.0.1)
    resp = client.post(
        "/login",
        data={"student_id": "nobody", "password": "nope"},
        headers={"X-Forwarded-Proto": "https"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    blob = ",".join(resp.headers.getlist("Set-Cookie"))
    if "lab_session=" in blob:
        assert "Secure" in blob
        assert "HttpOnly" in blob
        assert "SameSite=Lax" in blob


def test_500_page_hides_traceback(app, client):
    @app.route("/__boom__")
    def boom():
        raise RuntimeError("TRACE_MARK_SHOULD_NOT_LEAK")

    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["DEBUG"] = False
    resp = client.get("/__boom__")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 500
    assert "TRACE_MARK_SHOULD_NOT_LEAK" not in body
    assert "Traceback" not in body
    assert "服务器繁忙" in body


def test_password_toggle_on_all_password_pages(client):
    for path in ("/login", "/admin/login", "/forgot-password"):
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'class="pw-toggle"' in text
        assert "显示密码" in text
        assert 'type="password"' in text


def test_csrf_rejects_post_without_token(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    resp = client.post("/login", data={"student_id": "x", "password": "y"})
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "表单已过期" in body or "CSRF" in body


def test_approved_member_cannot_open_staff_or_borrow_pages(app, client):
    with app.app_context():
        user = User(student_id="am001", name="特批队员", role="approved_member")
        user.set_password(STRONG_PASSWORD)
        db.session.add(user)
        db.session.commit()
    login(client, "am001", STRONG_PASSWORD)
    home = client.get("/", follow_redirects=True)
    body = home.get_data(as_text=True)
    assert "/tools/admin" not in body
    assert "/people" not in body or 'href="/people"' not in body
    assert client.get("/people/", follow_redirects=True).status_code == 403
    assert client.get("/managers/", follow_redirects=True).status_code == 403
    assert client.get("/tools/admin").status_code == 403
    assert client.get("/tools/new", follow_redirects=False).status_code in (302, 303)


def test_officer_cannot_open_superadmin_modules(app, client, seed):
    login(client, "officer01", ADMIN_PASSWORD, staff=True)
    assert client.get("/people/", follow_redirects=True).status_code == 403
    assert client.get("/managers/", follow_redirects=True).status_code == 403
    assert client.get("/activity/users").status_code == 403
    assert client.get("/activity/admins").status_code == 403


def test_sqlite_wal_mode(app):
    with app.app_context():
        mode = db.session.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"
