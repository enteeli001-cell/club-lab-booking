from datetime import time

from app import db
from app.models import Booking, BookingCompanion, TeamMember, User
from app.notices import BOOKING_SUBMITTED_NOTICE
from app.utils import apply_team_roster
from tests.conftest import CAPTAIN_PHONE, FUTURE_DAY, add_booking, booking_form, login


def _roster(app, seed):
    with app.app_context():
        captain = db.session.get(User, seed["member_id"])
        apply_team_roster(
            captain,
            "结构先锋队",
            [
                {"name": "李四", "student_id": "2021011"},
                {"name": "周七", "student_id": "2021012"},
            ],
        )
        db.session.commit()
        return [m.id for m in TeamMember.query.filter_by(captain_id=captain.id).all()]


def test_captain_home_shows_welcome_and_entries(client, seed):
    login(client, "2021001")
    for path in ("/", "/captain"):
        page = client.get(path, follow_redirects=True)
        html = page.data.decode("utf-8")
        assert page.status_code == 200
        assert "张三，欢迎你进入测试预约平台" in html
        assert "测试预约" in html
        assert "向实验室借工具使用登记" in html
        assert "申请记录" in html


def test_staff_home_is_not_captain_workbench(client, seed):
    login(client, "officer01", staff=True)
    home = client.get("/", follow_redirects=True)
    html = home.data.decode("utf-8")
    assert "欢迎你进入测试预约平台" not in html
    assert "管理后台" in html
    assert client.get("/captain", follow_redirects=True).request.path != "/captain" or "欢迎你进入测试预约平台" not in client.get("/captain", follow_redirects=True).data.decode("utf-8")


def test_booking_over_one_hour_not_saved(app, client, seed):
    login(client, "2021001")
    client.post("/bookings/new", data=booking_form(seed["lab_id"], start_time="09:00", end_time="10:01"))
    with app.app_context():
        assert Booking.query.count() == 0


def test_overlap_30_allowed_31_rejected(app, client, seed):
    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        other = db.session.get(User, seed["other_id"])
        add_booking(lab, other, start=time(10, 0), end=time(11, 0), status="approved")
    login(client, "2021001")
    ok = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="10:30", end_time="11:30", purpose="三十分钟"),
        follow_redirects=True,
    )
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in ok.data
    bad = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], start_time="10:29", end_time="11:30", purpose="三十一分钟"),
        follow_redirects=True,
    )
    assert "已通过的预约时间冲突".encode("utf-8") in bad.data
    assert "31 分钟".encode("utf-8") in bad.data


def test_other_companions_required_and_validated(app, client, seed):
    member_ids = _roster(app, seed)
    login(client, "2021001")
    missing = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], include_other="on"),
        follow_redirects=True,
    )
    html = missing.data.decode("utf-8")
    assert "选择“其他”时，请至少填写一名其他人员" in html

    incomplete = client.post(
        "/bookings/new",
        data=booking_form(seed["lab_id"], include_other="on")
        | {},
        follow_redirects=True,
    )
    # rebuild with list fields
    data = booking_form(seed["lab_id"], include_other="on")
    data["other_name"] = ["钱九"]
    data["other_student_id"] = [""]
    data["other_contact"] = ["13800138001"]
    incomplete = client.post("/bookings/new", data=data, follow_redirects=True)
    assert "姓名、学号和联系方式须全部填写".encode("utf-8") in incomplete.data

    bad_contact = booking_form(seed["lab_id"], include_other="on")
    bad_contact["other_name"] = ["钱九"]
    bad_contact["other_student_id"] = ["2021999"]
    bad_contact["other_contact"] = ["not-a-contact"]
    resp = client.post("/bookings/new", data=bad_contact, follow_redirects=True)
    assert "须为手机号或邮箱".encode("utf-8") in resp.data

    ok = booking_form(seed["lab_id"], include_other="on", purpose="带其他人员")
    ok["companion_member"] = [str(member_ids[0])]
    ok["other_name"] = ["钱九"]
    ok["other_student_id"] = ["2021999"]
    ok["other_contact"] = ["13900139000"]
    created = client.post("/bookings/new", data=ok, follow_redirects=True)
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in created.data
    with app.app_context():
        booking = Booking.query.filter_by(user_id=seed["member_id"]).one()
        assert booking.people_count == 3  # captain + teammate + other
        assert booking.captain_phone == CAPTAIN_PHONE
        names = sorted(c.name for c in booking.companions)
        assert names == ["李四", "钱九"]
        other = BookingCompanion.query.filter_by(booking_id=booking.id, source="other").one()
        assert other.contact == "13900139000"
        user = db.session.get(User, seed["member_id"])
        assert user.last_phone == CAPTAIN_PHONE


def test_submitted_page_is_private(app, client, seed):
    login(client, "2021001")
    resp = client.post("/bookings/new", data=booking_form(seed["lab_id"]), follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/submitted" in location
    page = client.get(location)
    assert BOOKING_SUBMITTED_NOTICE.encode("utf-8") in page.data
    client.post("/logout")
    login(client, "2021002")
    denied = client.get(location, follow_redirects=True)
    assert "只能查看自己的预约".encode("utf-8") in denied.data
