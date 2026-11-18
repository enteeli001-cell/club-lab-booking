from datetime import date, time

from app import db
from app.models import Booking, Lab
from app.notices import BOOKING_SUBMITTED_NOTICE
from tests.conftest import CAPTAIN_PHONE, FUTURE_DAY, add_booking, booking_form, login


def test_create_booking_pending(app, client, seed):
    login(client, "2021001")
    resp = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="10:00", end_time="11:00", purpose="例会", people_count="3"),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "待审核".encode("utf-8") in resp.data
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in resp.data
    assert b'id="booking-submitted"' in resp.data
    assert "已提交测试申请".encode("utf-8") in resp.data
    with app.app_context():
        booking = Booking.query.filter_by(user_id=seed["member_id"]).one()
        assert booking.status == "pending"
        assert booking.people_count == 3
        assert booking.duration_minutes == 60


def test_rejects_past_outside_hours_capacity_and_granularity(app, client, seed):
    login(client, "2021001")
    base = booking_form(seed["lab_id"], purpose="测试", people_count="2")

    past = dict(base, date="2020-01-01")
    resp = client.post("/bookings/new", data=past, follow_redirects=True)
    assert "不能预约已经过去的时间".encode("utf-8") in resp.data

    late = dict(base, start_time="21:00", end_time="23:00")
    resp = client.post("/bookings/new", data=late, follow_redirects=True)
    assert "开放时间".encode("utf-8") in resp.data

    over = dict(base, people_count="99")
    resp = client.post("/bookings/new", data=over, follow_redirects=True)
    assert "不能超过实验室容量".encode("utf-8") in resp.data

    too_long = dict(base, start_time="10:00", end_time="11:01")
    resp = client.post("/bookings/new", data=too_long, follow_redirects=True)
    assert "不得超过 1 小时".encode("utf-8") in resp.data

    order = dict(base, start_time="11:00", end_time="10:00")
    resp = client.post("/bookings/new", data=order, follow_redirects=True)
    assert "结束时间必须晚于开始时间".encode("utf-8") in resp.data

    with app.app_context():
        assert Booking.query.count() == 0


def test_disabled_lab_cannot_be_booked(app, client, seed):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        lab.enabled = False
        db.session.commit()
    login(client, "2021001")
    resp = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], purpose="测试"),
        follow_redirects=True,
    )
    assert "停用".encode("utf-8") in resp.data


def test_member_sees_only_own_bookings_and_can_cancel(app, client, seed):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(__import__("app.models", fromlist=["User"]).User, seed["member_id"])
        other = db.session.get(__import__("app.models", fromlist=["User"]).User, seed["other_id"])
        mine = add_booking(lab, member, start=time(10, 0), end=time(11, 0), purpose="我的预约")
        add_booking(lab, other, start=time(14, 0), end=time(15, 0), purpose="别人的预约")
        mine_id = mine.id

    login(client, "2021001")
    listing = client.get("/bookings")
    assert "我的预约".encode("utf-8") in listing.data
    assert "别人的预约".encode("utf-8") not in listing.data

    client.post("/logout")
    login(client, "2021002")
    forbidden = client.post(f"/bookings/{mine_id}/cancel", follow_redirects=True)
    assert "只能取消自己的预约".encode("utf-8") in forbidden.data
    with app.app_context():
        assert db.session.get(Booking, mine_id).status == "pending"

    client.post("/logout")
    login(client, "2021001")
    ok = client.post(f"/bookings/{mine_id}/cancel", follow_redirects=True)
    assert "预约已取消".encode("utf-8") in ok.data
    with app.app_context():
        assert db.session.get(Booking, mine_id).status == "cancelled"


def test_schedule_view(app, client, seed):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        member = db.session.get(__import__("app.models", fromlist=["User"]).User, seed["member_id"])
        add_booking(lab, member, purpose="占用查看")
    login(client, "2021001")
    day = client.get(f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}&view=day")
    week = client.get(f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}&view=week")
    assert day.status_code == 200 and week.status_code == 200
    assert "占用查看".encode("utf-8") in day.data
    assert "占用查看".encode("utf-8") in week.data
