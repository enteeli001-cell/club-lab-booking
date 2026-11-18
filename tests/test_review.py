from datetime import time

from app import db
from app.models import Booking, User
from tests.conftest import add_booking, login


def test_member_cannot_review(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        booking = add_booking(lab, member)
        booking_id = booking.id
    login(client, "2021001")
    assert client.post(f"/review/{booking_id}/approve").status_code == 403
    assert client.post(f"/review/{booking_id}/reject", data={"reason": "不行"}).status_code == 403


def test_officer_approve_and_reject(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        a = add_booking(lab, member, start=time(10, 0), end=time(11, 0), purpose="通过我")
        b = add_booking(lab, other, start=time(14, 0), end=time(15, 0), purpose="驳回我")
        a_id, b_id = a.id, b.id

    login(client, "officer01", staff=True)
    empty = client.post(f"/review/{b_id}/reject", data={"reason": ""}, follow_redirects=True)
    assert "请填写理由".encode("utf-8") in empty.data

    rejected = client.post(f"/review/{b_id}/reject", data={"reason": "设备维护"}, follow_redirects=True)
    assert "已拒绝".encode("utf-8") in rejected.data

    approved = client.post(f"/review/{a_id}/approve", follow_redirects=True)
    assert "已通过".encode("utf-8") in approved.data

    with app.app_context():
        assert db.session.get(Booking, a_id).status == "approved"
        rec = db.session.get(Booking, b_id)
        assert rec.status == "rejected"
        assert rec.reject_reason == "设备维护"
        assert rec.reviewed_by_id == seed["officer_id"]


def test_approve_blocked_when_approved_conflict_exists(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="approved")
        pending = add_booking(lab, member, start=time(10, 29), end=time(11, 29), status="pending")
        pending_id = pending.id

    login(client, "officer01", staff=True)
    resp = client.post(f"/review/{pending_id}/approve", follow_redirects=True)
    assert "已通过的预约时间冲突".encode("utf-8") in resp.data
    with app.app_context():
        assert db.session.get(Booking, pending_id).status == "pending"


def test_second_pending_cannot_be_approved_after_first_approved(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        member = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        first = add_booking(lab, member, start=time(10, 0), end=time(11, 0), status="pending")
        second = add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="pending")
        first_id, second_id = first.id, second.id

    login(client, "admin", staff=True)
    assert "已通过".encode("utf-8") in client.post(f"/review/{first_id}/approve", follow_redirects=True).data
    resp = client.post(f"/review/{second_id}/approve", follow_redirects=True)
    assert "已通过的预约时间冲突".encode("utf-8") in resp.data
    with app.app_context():
        assert db.session.get(Booking, first_id).status == "approved"
        assert db.session.get(Booking, second_id).status == "pending"
