from datetime import datetime, time, timedelta

from app import db
from app.models import Booking, DutyShift, Lab, User
from app.notices import BOOKING_CHANGE_LOCKED_NOTICE
from app.utils import find_booking_contacts, local_now
from tests.conftest import add_booking, booking_form, login


def _open_all_day(lab):
    lab.open_start = time(0, 0)
    lab.open_end = time(23, 59)


def _booking_in(app, seed, minutes_ahead, status="pending", reviewer_id=None):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        _open_all_day(lab)
        user = db.session.get(User, seed["member_id"])
        start_dt = (local_now() + timedelta(minutes=minutes_ahead)).replace(microsecond=0)
        end_dt = start_dt + timedelta(minutes=45)
        if end_dt.date() != start_dt.date():
            end_dt = datetime.combine(start_dt.date(), time(23, 59))
        booking = add_booking(
            lab,
            user,
            day=start_dt.date(),
            start=start_dt.time(),
            end=end_dt.time(),
            status=status,
            purpose="临近时段",
        )
        if reviewer_id:
            booking.reviewed_by_id = reviewer_id
            db.session.commit()
        return booking.id


def test_records_page_has_two_tabs(client, seed):
    login(client, "2021001")
    page = client.get("/records")
    html = page.data.decode("utf-8")
    assert page.status_code == 200
    assert "测试申请记录" in html
    assert "工具借用申请记录" in html
    tools = client.get("/records?tab=tools")
    assert tools.status_code == 200
    assert "工具借用申请记录" in tools.data.decode("utf-8")


def test_captain_home_includes_records_entry(client, seed):
    login(client, "2021001")
    html = client.get("/captain", follow_redirects=True).data.decode("utf-8")
    assert "申请记录" in html
    assert "测试预约" in html
    assert "向实验室借工具使用登记" in html


def test_can_edit_and_cancel_when_more_than_30_minutes(app, client, seed):
    booking_id = _booking_in(app, seed, 31, status="approved")
    login(client, "2021001")
    page = client.get(f"/bookings/{booking_id}/edit")
    assert page.status_code == 200
    assert "修改测试预约".encode("utf-8") in page.data
    with app.app_context():
        booking = db.session.get(Booking, booking_id)
        data = booking_form(
            booking.lab_id,
            date=booking.date.isoformat(),
            start_time=booking.start_time.strftime("%H:%M"),
            end_time=booking.end_time.strftime("%H:%M"),
            purpose="已改期用途",
        )
    saved = client.post(f"/bookings/{booking_id}/edit", data=data, follow_redirects=True)
    assert saved.status_code == 200
    with app.app_context():
        booking = db.session.get(Booking, booking_id)
        assert booking.purpose == "已改期用途"
        assert booking.status == "pending"
        assert booking.reviewed_by_id is None

    other_id = _booking_in(app, seed, 40, status="pending")
    cancelled = client.post(f"/bookings/{other_id}/cancel", follow_redirects=True)
    assert "预约已取消".encode("utf-8") in cancelled.data
    with app.app_context():
        assert db.session.get(Booking, other_id).status == "cancelled"


def test_cannot_edit_or_cancel_at_exactly_30_minutes(app, client, seed):
    with app.app_context():
        officer = db.session.get(User, seed["officer_id"])
        officer.contact_phone = "13700137001"
        lab = db.session.get(Lab, seed["lab_id"])
        lab.contact_name = "实验室值班室"
        lab.contact_phone = "13500135001"
        db.session.commit()
    booking_id = _booking_in(app, seed, 30, status="pending")
    login(client, "2021001")
    edit = client.get(f"/bookings/{booking_id}/edit", follow_redirects=True)
    html = edit.data.decode("utf-8")
    assert BOOKING_CHANGE_LOCKED_NOTICE in html
    locked = client.post(f"/bookings/{booking_id}/cancel", follow_redirects=True)
    body = locked.data.decode("utf-8")
    assert BOOKING_CHANGE_LOCKED_NOTICE in body
    with app.app_context():
        assert db.session.get(Booking, booking_id).status == "pending"


def test_contact_lookup_prefers_duty_then_reviewer_then_lab(app, seed):
    from tests.conftest import FUTURE_DAY

    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        lab.contact_name = "实验室值班室"
        lab.contact_phone = "13500135001"
        officer = db.session.get(User, seed["officer_id"])
        officer.contact_phone = "13700137001"
        admin = db.session.get(User, seed["admin_id"])
        admin.contact_phone = "13600136001"
        member = db.session.get(User, seed["member_id"])
        booking = add_booking(lab, member, start=time(10, 0), end=time(11, 0), status="approved")
        booking.reviewed_by_id = admin.id
        db.session.commit()

        # no duty → reviewer
        contacts = find_booking_contacts(booking)
        assert len(contacts) == 1
        assert contacts[0]["source"] == "reviewer"
        assert contacts[0]["name"] == "总管理员"
        assert contacts[0]["phone"] == "13600136001"

        booking.reviewed_by_id = None
        db.session.commit()
        contacts = find_booking_contacts(booking)
        assert contacts[0]["source"] == "lab"
        assert contacts[0]["name"] == "实验室值班室"
        assert contacts[0]["phone"] == "13500135001"

        shift = DutyShift(
            lab=lab,
            admin=officer,
            specific_date=FUTURE_DAY,
            weekday=None,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
        db.session.add(shift)
        booking.reviewed_by_id = admin.id
        db.session.commit()
        contacts = find_booking_contacts(booking)
        assert [c["source"] for c in contacts] == ["duty"]
        assert contacts[0]["name"] == "李管理员"
        assert contacts[0]["phone"] == "13700137001"

        # weekday duty also matches
        db.session.delete(shift)
        db.session.commit()
        weekly = DutyShift(
            lab=lab,
            admin=officer,
            specific_date=None,
            weekday=FUTURE_DAY.weekday(),
            start_time=time(10, 30),
            end_time=time(11, 30),
        )
        db.session.add(weekly)
        db.session.commit()
        contacts = find_booking_contacts(booking)
        assert contacts[0]["source"] == "duty"
        assert contacts[0]["name"] == "李管理员"
