"""Passive security center and viewable staff-password ciphertext."""

import html
from datetime import timedelta

from app import db
from app.models import AuditLog, Blacklist, SecurityEvent, User
from app.pwstore import decrypt_password
from app.security import clear_security_events
from tests.conftest import ADMIN_PASSWORD, STRONG_PASSWORD, SUPER_PASSWORD, login


def teardown_function(_fn=None):
    # Extra safety; the app fixture already uses a temp database.
    pass


def _cleanup(app):
    with app.app_context():
        clear_security_events()
        Blacklist.query.delete()
        db.session.commit()


def test_staff_password_cipher_roundtrip(app):
    with app.app_context():
        user = User(student_id="cipher01", name="密文管理员", role="admin")
        user.set_password("LabAdmin#12yz")
        db.session.add(user)
        db.session.commit()
        assert user.has_password_cipher
        assert decrypt_password(user.password_cipher) == "LabAdmin#12yz"
        captain = User(student_id="ciphcap", name="队长", role="member")
        captain.set_password(STRONG_PASSWORD)
        db.session.add(captain)
        db.session.commit()
        assert not captain.password_cipher


def test_superadmin_can_reveal_admin_password_and_it_is_logged(app, client, seed):
    login(client, "admin", staff=True)
    created = client.post(
        "/managers/new",
        data={"name": "可查看管理员", "student_id": "viewadm1", "contact_phone": "13900006666"},
        follow_redirects=True,
    )
    text = created.get_data(as_text=True)
    assert "issued-password" in text
    with app.app_context():
        user = User.query.filter_by(student_id="viewadm1").one()
        assert user.has_password_cipher
        uid = user.id
        expected = decrypt_password(user.password_cipher)
    revealed = client.post(f"/managers/{uid}/reveal-password", follow_redirects=True)
    body = revealed.get_data(as_text=True)
    assert revealed.status_code == 200
    assert expected in html.unescape(body)
    assert "记入审计日志" in body
    with app.app_context():
        log = AuditLog.query.filter_by(action="admin.view_password").order_by(AuditLog.id.desc()).first()
        assert log is not None
        assert "viewadm1" in log.summary
        assert expected not in (log.summary or "")
        assert expected not in (log.payload or "")
    _cleanup(app)


def test_officer_cannot_reveal_or_open_security_center(app, client, seed):
    login(client, "officer01", ADMIN_PASSWORD, staff=True)
    assert client.get("/security/").status_code == 403
    assert client.post("/managers/1/reveal-password").status_code == 403
    _cleanup(app)


def test_honeypot_and_auto_ban(app, client):
    env = {"REMOTE_ADDR": "198.51.100.23"}
    resp = client.get("/_security_honeypot/do-not-index", environ_base=env)
    assert resp.status_code == 404
    with app.app_context():
        ev = SecurityEvent.query.filter_by(event_type="honeypot").first()
        assert ev is not None
        assert ev.severity == "critical"
        assert ev.ip == "198.51.100.23"
        assert ev.auto_blocked is True
        assert Blacklist.query.filter_by(kind="ip", value="198.51.100.23", active=True).first()
    banned = client.get("/login", environ_base=env)
    assert banned.status_code == 403
    assert "黑" in banned.get_data(as_text=True) or "封" in banned.get_data(as_text=True) or "禁止" in banned.get_data(as_text=True) or "无法" in banned.get_data(as_text=True)
    _cleanup(app)


def test_path_scan_known_paths(app, client):
    env = {"REMOTE_ADDR": "198.51.100.24"}
    resp = client.get("/.env", environ_base=env)
    assert resp.status_code == 404
    with app.app_context():
        ev = SecurityEvent.query.filter_by(event_type="path_scan", ip="198.51.100.24").first()
        assert ev is not None
        assert ev.severity == "high"
        assert ev.auto_blocked is True
    _cleanup(app)


def test_brute_force_auto_ban(app, client):
    env = {"REMOTE_ADDR": "198.51.100.25"}
    last = None
    for _ in range(9):
        last = client.post(
            "/login",
            data={"student_id": "nosuchuser", "password": "wrong-password"},
            environ_base=env,
        )
    with app.app_context():
        events = SecurityEvent.query.filter_by(event_type="brute_force", ip="198.51.100.25").all()
        assert events
        assert any(ev.auto_blocked for ev in events)
        assert Blacklist.query.filter_by(kind="ip", value="198.51.100.25", active=True).first()
    banned = client.get("/login", environ_base=env)
    assert banned.status_code == 403
    _cleanup(app)


def test_superadmin_session_is_not_auto_banned(app, client, seed):
    headers = {"CF-Connecting-IP": "203.0.113.80"}
    env = {"REMOTE_ADDR": "127.0.0.1"}
    # Log the superadmin in through the "tunnel" so the session IP is remembered.
    login_resp = client.post(
        "/admin/login",
        data={"password": SUPER_PASSWORD},
        headers=headers,
        environ_base=env,
        follow_redirects=True,
    )
    assert login_resp.status_code == 200
    trap = client.get(
        "/_security_honeypot/keep-admin",
        headers=headers,
        environ_base=env,
        follow_redirects=False,
    )
    assert trap.status_code == 404
    with app.app_context():
        ev = (
            SecurityEvent.query.filter_by(event_type="honeypot", ip="203.0.113.80")
            .order_by(SecurityEvent.id.desc())
            .first()
        )
        assert ev is not None
        assert ev.auto_blocked is False
        active = Blacklist.query.filter_by(kind="ip", value="203.0.113.80", active=True).all()
        assert not any(b.is_in_effect() for b in active)
    still = client.get("/", headers=headers, environ_base=env, follow_redirects=True)
    assert still.status_code == 200
    _cleanup(app)


def test_localhost_not_auto_banned_by_honeypot(app, client):
    resp = client.get("/_security_honeypot/local")
    assert resp.status_code == 404
    with app.app_context():
        ev = SecurityEvent.query.filter_by(event_type="honeypot").order_by(SecurityEvent.id.desc()).first()
        assert ev is not None
        assert ev.auto_blocked is False
        assert not Blacklist.query.filter_by(value="127.0.0.1", active=True).first()
    assert client.get("/login").status_code == 200
    _cleanup(app)


def test_robots_txt_lists_honeypot(client, app):
    resp = client.get("/robots.txt")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "text/plain" in (resp.headers.get("Content-Type") or "")
    assert "Disallow: /_security_honeypot/" in body
    _cleanup(app)


def test_security_headers_present(client, app):
    resp = client.get("/login")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors 'none'" in (resp.headers.get("Content-Security-Policy") or "")
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    _cleanup(app)


def test_security_dashboard_requires_superadmin_and_lists_events(app, client, seed):
    client.get("/.git/config", environ_base={"REMOTE_ADDR": "198.51.100.88"})
    login(client, "officer01", ADMIN_PASSWORD, staff=True)
    assert client.get("/security/").status_code == 403
    client.post("/logout")
    login(client, "admin", SUPER_PASSWORD, staff=True)
    page = client.get("/security/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "安全防护中心" in text
    assert "路径扫描" in text
    assert "ipinfo.io" in text
    export = client.get("/security/export.xlsx")
    assert export.status_code == 200
    assert "spreadsheet" in (export.headers.get("Content-Type") or "")
    _cleanup(app)
