import html
import io
import re
from datetime import date, timedelta

from openpyxl import Workbook

from app import db
from app.models import AuditLog, Blacklist, Booking, ToolCatalog, User
from app.utils import today
from tests.conftest import add_booking, login


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = "teams.xlsx"
    return buf


def test_regular_admin_cannot_access_super_modules(app, client, seed):
    login(client, "officer01", staff=True)
    for path in ("/people/", "/people/import", "/managers/", "/activity/users", "/activity/admins"):
        assert client.get(path).status_code == 403
    assert client.post("/people/import/confirm", follow_redirects=False).status_code == 403
    assert client.post("/managers/new", data={"name": "x", "student_id": "x"}).status_code == 403
    assert client.post("/activity/blacklist", data={"kind": "ip", "value": "1.1.1.1", "reason": "x"}).status_code == 403


def test_team_import_creates_and_updates(app, client, seed):
    login(client, "admin", staff=True)
    first = _xlsx(
        [
            ["队名", "队长姓名", "队长学号", "队长手机", "队长邮箱", "队员1姓名", "队员1学号", "队员1手机"],
            ["先锋队", "导入队长", "2099001", "13900001111", "lead@example.com", "队员甲", "2099011", "13900002222"],
        ]
    )
    preview = client.post("/people/import", data={"file": (first, "teams.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    assert preview.status_code == 200
    assert "新增".encode("utf-8") in preview.data
    assert "先锋队".encode("utf-8") in preview.data
    confirm = client.post("/people/import/confirm", follow_redirects=True)
    assert "新增 1 支".encode("utf-8") in confirm.data
    with app.app_context():
        user = User.query.filter_by(student_id="2099001").one()
        assert user.name == "导入队长"
        assert user.team_name == "先锋队"
        assert user.last_email == "lead@example.com"
        assert user.teammates[0].name == "队员甲"
        assert user.teammates[0].phone == "13900002222"

    second = _xlsx(
        [
            ["队名", "队长姓名", "队长学号", "队长手机", "队长邮箱", "队员1姓名", "队员1学号", "队员1手机"],
            ["先锋队改名", "导入队长乙", "2099001", "13900003333", "lead2@example.com", "队员乙", "2099012", "13900004444"],
        ]
    )
    client.post("/people/import", data={"file": (second, "teams.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    confirm = client.post("/people/import/confirm", follow_redirects=True)
    assert "更新 1 支".encode("utf-8") in confirm.data
    with app.app_context():
        user = User.query.filter_by(student_id="2099001").one()
        assert user.name == "导入队长乙"
        assert user.team_name == "先锋队改名"
        assert user.last_phone == "13900003333"
        assert [m.student_id for m in user.teammates] == ["2099012"]


def test_special_guest_login_and_expiry(app, client, seed):
    login(client, "admin", staff=True)
    future = (today() + timedelta(days=10)).isoformat()
    past = (today() - timedelta(days=1)).isoformat()
    created = client.post(
        "/people/special",
        data={
            "name": "特批来宾",
            "student_id": "SP0001",
            "organization": "示例单位",
            "special_reason": "交流访问",
            "valid_until": future,
        },
        follow_redirects=True,
    )
    assert "已创建特批非参赛账号".encode("utf-8") in created.data
    client.post("/logout")

    resp = client.post("/login", data={"student_id": "SP0001", "password": "SP0001"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "请先修改".encode("utf-8") in resp.data or "修改密码".encode("utf-8") in resp.data
    client.post("/logout")

    with app.app_context():
        user = User.query.filter_by(student_id="SP0001").one()
        user.valid_until = today() - timedelta(days=1)
        user.must_change_password = False
        user.set_password("Valid#1a", require_change=False)
        db.session.commit()
    expired = client.post("/login", data={"student_id": "SP0001", "password": "Valid#1a"}, follow_redirects=True)
    assert "有效期".encode("utf-8") in expired.data
    with app.app_context():
        # still unauthenticated: no session user
        assert User.query.filter_by(student_id="SP0001").one().is_account_active is False


def test_blacklist_and_lift(app, client, seed):
    login(client, "admin", staff=True)
    blocked_ip = "203.0.113.50"
    self_ban = client.post(
        "/activity/blacklist",
        data={"kind": "ip", "value": "127.0.0.1", "reason": "误操作"},
        follow_redirects=True,
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert "不能拉黑自己当前的 IP".encode("utf-8") in self_ban.data

    added = client.post(
        "/activity/blacklist",
        data={"kind": "ip", "value": blocked_ip, "reason": "测试拉黑"},
        follow_redirects=True,
    )
    assert "已加入拉黑名单".encode("utf-8") in added.data

    banned = client.get("/login", environ_base={"HTTP_X_FORWARDED_FOR": blocked_ip})
    assert banned.status_code == 403
    assert "访问已被禁止".encode("utf-8") in banned.data

    with app.app_context():
        row = Blacklist.query.filter_by(value=blocked_ip, kind="ip").one()
        block_id = row.id
    lifted = client.post(f"/activity/blacklist/{block_id}/lift", follow_redirects=True)
    assert "已解除拉黑".encode("utf-8") in lifted.data

    device = "testdeviceid001"
    added_dev = client.post(
        "/activity/blacklist",
        data={"kind": "device", "value": device, "reason": "设备测试"},
        follow_redirects=True,
    )
    assert "已加入拉黑名单".encode("utf-8") in added_dev.data
    client.post("/logout")
    ok = client.get("/login", environ_base={"HTTP_X_FORWARDED_FOR": blocked_ip})
    assert ok.status_code == 200
    assert "访问已被禁止".encode("utf-8") not in ok.data
    client.set_cookie("lab_device_id", device)
    banned_dev = client.get("/login")
    assert banned_dev.status_code == 403
    assert "访问已被禁止".encode("utf-8") in banned_dev.data


def test_undo_review(app, client, seed):
    from app.models import Lab

    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        booking = add_booking(lab, member, purpose="待撤销")
        booking_id = booking.id

    login(client, "officer01", staff=True)
    client.post(f"/review/{booking_id}/approve", follow_redirects=True)
    client.post("/logout")
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "approved"
        log = AuditLog.query.filter_by(action="booking.approve", target_id=booking_id).order_by(AuditLog.id.desc()).first()
        assert log is not None
        log_id = log.id

    login(client, "admin", staff=True)
    undone = client.post(f"/activity/logs/{log_id}/undo", follow_redirects=True)
    assert "恢复为待审核".encode("utf-8") in undone.data
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "pending"
        assert AuditLog.query.filter_by(action="audit.undo").count() >= 1

    client.post("/logout")
    login(client, "officer01", staff=True)
    client.post(f"/review/{booking_id}/reject", data={"reason": "测试拒绝"}, follow_redirects=True)
    with app.app_context():
        log = AuditLog.query.filter_by(action="booking.reject", target_id=booking_id).order_by(AuditLog.id.desc()).one()
        reject_id = log.id
    client.post("/logout")
    login(client, "admin", staff=True)
    undone = client.post(f"/activity/logs/{reject_id}/undo", follow_redirects=True)
    assert "恢复为待审核".encode("utf-8") in undone.data
    assert "无法撤回".encode("utf-8") in undone.data
    with app.app_context():
        rec = db.session.get(Booking, booking_id)
        assert rec.status == "pending"


def test_admin_password_shown_once_and_strong(app, client, seed):
    login(client, "admin", staff=True)
    page = client.post(
        "/managers/new",
        data={"name": "新管理员", "student_id": "mgr9001", "contact_phone": "13900005555"},
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "仅显示一次".encode("utf-8") in page.data
    text = page.get_data(as_text=True)
    match = re.search(r'class="issued-password">([^<]+)<', text)
    assert match, text[:500]
    password = html.unescape(match.group(1)).strip()
    assert len(password) >= 16
    assert any("A" <= c <= "Z" for c in password)
    assert any("a" <= c <= "z" for c in password)
    assert any(c.isdigit() for c in password)
    assert any(not c.isalnum() for c in password)
    again = client.get("/managers/")
    assert "issued-password".encode("utf-8") not in again.data
    assert "仅显示一次".encode("utf-8") not in again.data
    with app.app_context():
        user = User.query.filter_by(student_id="mgr9001").one()
        assert user.role == "admin"
        assert user.check_password(password)
        assert user.must_change_password is False
