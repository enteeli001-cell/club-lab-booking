from datetime import time

from app import db
from app.models import Booking, User
from app.utils import find_conflicts, intervals_overlap, overlap_minutes, validate_booking
from app.notices import BOOKING_SUBMITTED_NOTICE
from tests.conftest import FUTURE_DAY, add_booking, booking_form, login


def test_intervals_adjacent_do_not_overlap():
    assert not intervals_overlap(time(10, 0), time(11, 0), time(11, 0), time(12, 0))
    assert not intervals_overlap(time(11, 0), time(12, 0), time(10, 0), time(11, 0))
    assert intervals_overlap(time(10, 0), time(11, 0), time(10, 30), time(11, 30))
    assert intervals_overlap(time(10, 0), time(11, 0), time(10, 0), time(11, 0))
    assert intervals_overlap(time(10, 0), time(12, 0), time(11, 0), time(11, 30))
    assert not intervals_overlap(time(9, 0), time(10, 0), time(10, 0), time(11, 0))


def test_submit_rejects_approved_overlap_but_allows_adjacent(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        other = db.session.get(User, seed["other_id"])
        add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="approved")

    login(client, "2021001")
    allowed_30 = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="10:30", end_time="11:30", purpose="重叠30分钟"),
        follow_redirects=True,
    )
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in allowed_30.data

    rejected_31 = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="10:29", end_time="11:29", purpose="重叠31分钟"),
        follow_redirects=True,
    )
    assert "已通过的预约时间冲突".encode("utf-8") in rejected_31.data
    assert "31 分钟".encode("utf-8") in rejected_31.data

    exact = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], purpose="完全重叠"),
        follow_redirects=True,
    )
    assert "已通过的预约时间冲突".encode("utf-8") in exact.data

    adjacent = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="11:00", end_time="12:00", purpose="首尾相接"),
        follow_redirects=True,
    )
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in adjacent.data

    before = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="09:00", end_time="10:00", purpose="前面相接"),
        follow_redirects=True,
    )
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in before.data

    with app.app_context():
        mine = Booking.query.filter_by(user_id=seed["member_id"]).all()
        assert len(mine) == 3


def test_pending_overlap_allowed_with_warning(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        other = db.session.get(User, seed["other_id"])
        add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="pending")

    login(client, "2021001")
    resp = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], purpose="同样时段"),
        follow_redirects=True,
    )
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in resp.data
    assert "待审核预约重叠".encode("utf-8") in resp.data


def test_validate_booking_helper_boundaries(app, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        other = db.session.get(User, seed["other_id"])
        approved = add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="approved")
        errors = validate_booking(lab, FUTURE_DAY, time(11, 0), time(12, 0), 2)
        assert errors == []
        errors = validate_booking(lab, FUTURE_DAY, time(10, 0), time(11, 0), 2)
        assert any("冲突" in e for e in errors)
        approved_conflicts, pending = find_conflicts(lab.id, FUTURE_DAY, time(10, 30), time(11, 30))
        assert approved_conflicts == []
        assert pending == []
        approved_conflicts, pending = find_conflicts(lab.id, FUTURE_DAY, time(10, 29), time(11, 29))
        assert approved.id in [b.id for b in approved_conflicts]
        assert pending == []
        assert overlap_minutes(time(10, 0), time(11, 0), time(10, 30), time(11, 30)) == 30
        assert overlap_minutes(time(10, 0), time(11, 0), time(10, 29), time(11, 30)) == 31
