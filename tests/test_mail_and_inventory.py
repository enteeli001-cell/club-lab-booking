from pathlib import Path
from unittest.mock import patch

from app import db
from app.mailer import is_reserved_recipient, send_mail
from app.models import Booking, MailLog, ToolCatalog, ToolLoan, User
from app.notices import REJECT_EMAIL_WARNING
from app.utils import MINIMAL_PNG_BYTES, local_now
from tests.conftest import CAPTAIN_EMAIL, CAPTAIN_PHONE, login
from tests.test_tools import _loan_form, _png
from datetime import timedelta
import io


def test_reserved_recipients():
    assert is_reserved_recipient("zhangsan@example.com")
    assert is_reserved_recipient("a@foo.example.org")
    assert is_reserved_recipient("user@my.test")
    assert is_reserved_recipient("x@y.invalid")
    assert not is_reserved_recipient("someone@example.edu")


def test_booking_requires_captain_email(app, client, seed):
    login(client, "2021001")
    missing = client.post(
        "/bookings/new",
        data={
            "lab_id": str(seed["lab_id"]),
            "date": "2099-06-15",
            "start_time": "10:00",
            "end_time": "11:00",
            "purpose": "测试",
            "people_count": "1",
            "captain_phone": CAPTAIN_PHONE,
            "captain_email": "",
        },
        follow_redirects=True,
    )
    assert "请填写有效的队长邮箱".encode("utf-8") in missing.data
    bad = client.post(
        "/bookings/new",
        data={
            "lab_id": str(seed["lab_id"]),
            "date": "2099-06-15",
            "start_time": "10:00",
            "end_time": "11:00",
            "purpose": "测试",
            "people_count": "1",
            "captain_phone": CAPTAIN_PHONE,
            "captain_email": "not-an-email",
        },
        follow_redirects=True,
    )
    assert "请填写有效的队长邮箱".encode("utf-8") in bad.data


def test_reject_requires_reason_and_sends_to_local_outbox(app, client, seed):
    from tests.conftest import add_booking
    from app.models import Lab

    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        booking = add_booking(lab, member, purpose="待拒")
        booking_id = booking.id
        outbox = Path(app.config["OUTBOX_DIR"])

    login(client, "officer01", staff=True)
    listing = client.get("/review/?status=pending")
    assert listing.status_code == 200
    assert REJECT_EMAIL_WARNING.encode("utf-8") in listing.data
    assert "申请审核列表".encode("utf-8") in listing.data

    empty = client.post(f"/review/{booking_id}/reject", data={"reason": "   "}, follow_redirects=True)
    assert "请填写理由".encode("utf-8") in empty.data
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "pending"
        assert MailLog.query.count() == 0

    rejected = client.post(
        f"/review/{booking_id}/reject",
        data={"reason": "设备维护，改期再约"},
        follow_redirects=True,
    )
    assert "已拒绝".encode("utf-8") in rejected.data
    with app.app_context():
        rec = db.session.get(Booking, booking_id)
        assert rec.status == "rejected"
        assert rec.reject_reason == "设备维护，改期再约"
        log = MailLog.query.one()
        assert log.recipient == CAPTAIN_EMAIL
        assert log.status == "local"
        assert "设备维护" in log.body
        assert log.filename
        assert (outbox / log.filename).is_file()

    logs_page = client.get("/mail/")
    assert logs_page.status_code == 200
    assert "本地存档".encode("utf-8") in logs_page.data
    assert CAPTAIN_EMAIL.encode("utf-8") in logs_page.data


def test_reserved_domain_never_calls_smtp_even_when_enabled(app, client, seed, monkeypatch):
    from tests.conftest import add_booking
    from app.models import Lab

    monkeypatch.setenv("MAIL_SEND_ENABLED", "true")
    monkeypatch.setenv("MAIL_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_USERNAME", "sender@example.test")
    monkeypatch.setenv("MAIL_FROM", "sender@example.test")
    monkeypatch.setenv("MAIL_PASSWORD_ENV", "SMTP_AUTH_CODE")
    monkeypatch.setenv("SMTP_AUTH_CODE", "dummy-not-used")

    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        booking = add_booking(lab, member, purpose="保留域名")
        booking_id = booking.id

    with patch("app.mailer.send_via_smtp") as mocked:
        login(client, "officer01", staff=True)
        resp = client.post(f"/review/{booking_id}/reject", data={"reason": "理由充分"}, follow_redirects=True)
        assert "已拒绝".encode("utf-8") in resp.data
        mocked.assert_not_called()
    with app.app_context():
        log = MailLog.query.one()
        assert log.status == "local"


def test_smtp_success_mocked_and_failure_does_not_block_reject(app, client, seed, monkeypatch):
    from tests.conftest import add_booking
    from app.models import Lab

    monkeypatch.setenv("MAIL_SEND_ENABLED", "true")
    monkeypatch.setenv("MAIL_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_USERNAME", "sender@example.test")
    monkeypatch.setenv("MAIL_FROM", "sender@example.test")
    monkeypatch.setenv("MAIL_PASSWORD_ENV", "TEST_SMTP_SECRET")
    monkeypatch.setenv("TEST_SMTP_SECRET", "dummy-not-used")

    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        ok_booking = add_booking(lab, member, purpose="模拟成功")
        ok_booking.captain_email = "captain.ok@school.edu"
        fail_booking = add_booking(lab, member, purpose="模拟失败", start=__import__("datetime").time(14, 0), end=__import__("datetime").time(15, 0))
        fail_booking.captain_email = "captain.fail@school.edu"
        db.session.commit()
        ok_id, fail_id = ok_booking.id, fail_booking.id

    login(client, "officer01", staff=True)
    with patch("app.mailer.send_via_smtp") as mocked:
        mocked.return_value = None
        resp = client.post(f"/review/{ok_id}/reject", data={"reason": "通过邮件检查"}, follow_redirects=True)
        assert "已拒绝".encode("utf-8") in resp.data
        mocked.assert_called_once()
    with app.app_context():
        log = MailLog.query.filter_by(booking_id=ok_id).one()
        assert log.status == "sent"
        assert log.recipient == "captain.ok@school.edu"

    with patch("app.mailer.send_via_smtp", side_effect=RuntimeError("smtp down")) as mocked:
        resp = client.post(f"/review/{fail_id}/reject", data={"reason": "仍然要拒绝"}, follow_redirects=True)
        assert "已拒绝".encode("utf-8") in resp.data
        assert "邮件发送失败".encode("utf-8") in resp.data
        mocked.assert_called_once()
    with app.app_context():
        rec = db.session.get(Booking, fail_id)
        assert rec.status == "rejected"
        log = MailLog.query.filter_by(booking_id=fail_id).one()
        assert log.status == "failed"
        assert "smtp down" in (log.error or "")
        assert "dummy-not-used" not in (log.error or "")


def test_send_via_smtp_blocked_in_pytest(app):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = "a@school.edu"
    msg["Subject"] = "x"
    msg.set_content("y")
    with app.app_context():
        try:
            from app.mailer import send_via_smtp

            send_via_smtp(msg)
            raise AssertionError("should not send")
        except RuntimeError as exc:
            assert "pytest" in str(exc)


def _ensure_catalog(name, total, is_major=True):
    item = ToolCatalog(name=name, total_quantity=total, is_major=is_major)
    db.session.add(item)
    db.session.commit()
    return item


def test_inventory_and_over_borrow(app, client, seed):
    with app.app_context():
        wrench = _ensure_catalog("扳手", 2)
        wrench_id = wrench.id

    login(client, "2021001")
    # first loan uses 2, remaining 0
    data = _loan_form(
        seed["lab_id"],
        tool_catalog_id=[str(wrench_id)],
        tool_name=[""],
        tool_quantity=["2"],
    )
    resp = client.post("/tools/new", data=data, follow_redirects=True, content_type="multipart/form-data")
    assert "工具借用已登记".encode("utf-8") in resp.data

    over = _loan_form(
        seed["lab_id"],
        tool_catalog_id=[str(wrench_id)],
        tool_name=[""],
        tool_quantity=["1"],
        borrow_photo=_png("second.png"),
    )
    resp = client.post("/tools/new", data=over, follow_redirects=True, content_type="multipart/form-data")
    assert "超出库存".encode("utf-8") in resp.data
    with app.app_context():
        assert ToolLoan.query.count() == 1

    # 其他 does not count
    other = _loan_form(
        seed["lab_id"],
        tool_catalog_id=["other"],
        tool_name=["自制夹具"],
        tool_quantity=["9"],
        borrow_photo=_png("other.png"),
    )
    resp = client.post("/tools/new", data=other, follow_redirects=True, content_type="multipart/form-data")
    assert "工具借用已登记".encode("utf-8") in resp.data
    with app.app_context():
        from app.inventory import borrowed_quantity, remaining_quantity

        assert borrowed_quantity(wrench_id) == 2
        item = db.session.get(ToolCatalog, wrench_id)
        assert remaining_quantity(item) == 0
        other_item = ToolLoan.query.order_by(ToolLoan.id.desc()).first().items[0]
        assert other_item.is_other is True
        assert other_item.name == "自制夹具"


def test_admin_tools_inventory_card_and_catalog_permission(app, client, seed):
    with app.app_context():
        _ensure_catalog("卡尺", 5)
        # seed a loan matching catalog by name via helper
        caliper = ToolCatalog.query.filter_by(name="卡尺").one()
        cid = caliper.id

    login(client, "2021001")
    client.post(
        "/tools/new",
        data=_loan_form(seed["lab_id"], tool_catalog_id=[str(cid)], tool_name=[""], tool_quantity=["1"]),
        content_type="multipart/form-data",
    )
    client.post("/logout")

    login(client, "officer01", staff=True)
    page = client.get("/tools/admin")
    assert page.status_code == 200
    assert "主要工具库存".encode("utf-8") in page.data
    assert "卡尺".encode("utf-8") in page.data
    assert "剩余可借".encode("utf-8") in page.data
    denied = client.get("/tools/catalog")
    assert denied.status_code == 403
    denied_post = client.post("/tools/catalog/new", data={"name": "电钻", "total_quantity": "3", "is_major": "1"})
    assert denied_post.status_code == 403
    client.post("/logout")

    login(client, "admin", staff=True)
    home = client.get("/")
    assert home.status_code == 200
    assert "欢迎你".encode("utf-8") in home.data
    assert "申请审核列表".encode("utf-8") in home.data
    assert "工具借用记录".encode("utf-8") in home.data

    created = client.post(
        "/tools/catalog/new",
        data={"name": "电钻", "total_quantity": "3", "is_major": "1"},
        follow_redirects=True,
    )
    assert "已添加工具".encode("utf-8") in created.data
    with app.app_context():
        assert ToolCatalog.query.filter_by(name="电钻").one().total_quantity == 3
