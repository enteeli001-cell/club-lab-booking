"""Captains never see who occupies a slot (HTML + API); server-side required-field messages."""
import re
from datetime import time

import pytest

from app import db
from app.models import Announcement, Booking, TeamMember, ToolLoan, User
from tests.conftest import (
    ADMIN_PASSWORD,
    CAPTAIN_PHONE,
    FUTURE_DAY,
    SUPER_PASSWORD,
    add_booking,
    api_headers,
    booking_form,
    create_lab,
    create_user,
    login,
)
from tests.test_tools import _loan_form

SECRET_PURPOSE = "机密用途XYZ"
MY_PURPOSE = "我自己的用途ABC"


@pytest.fixture
def occupied(app, seed):
    """张三 (my own) approved slot 10-11, 王五 pending slot 14-15 with every identifying field filled."""
    with app.app_context():
        mine = db.session.get(User, seed["member_id"])
        other = db.session.get(User, seed["other_id"])
        other.team_name = "雷霆结构队"
        other.last_email = "wangwu-private@example.com"
        other.last_phone = "13912345678"
        db.session.add(TeamMember(captain=other, name="队员赵六", student_id="2099777"))
        db.session.commit()
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"])
        a = add_booking(lab, mine, start=time(10), end=time(11), purpose=MY_PURPOSE, status="approved")
        b = add_booking(lab, other, start=time(14), end=time(15), purpose=SECRET_PURPOSE, status="pending")
        b.captain_email = "wangwu-private@example.com"
        b.captain_phone = "13912345678"
        db.session.commit()
    return seed


LEAKS = ("王五", "2021002", "雷霆结构队", SECRET_PURPOSE, "wangwu-private@example.com", "13912345678", "队员赵六", "2099777")


def _schedule(client, seed, view):
    return client.get(f"/schedule?lab_id={seed['lab_id']}&date={FUTURE_DAY.isoformat()}&view={view}").get_data(as_text=True)


@pytest.mark.parametrize("view", ["day", "week"])
def test_captain_schedule_hides_other_captains(app, client, occupied, view):
    login(client, "2021001")
    html = _schedule(client, occupied, view)
    for leak in LEAKS:
        assert leak not in html, f"{leak!r} leaked into the occupancy view"
    # The slot is still shown as occupied, with its status and no booking id in any attribute.
    assert "已占用" in html and "14:00–15:00" in html and "待审核" in html
    assert 'title="已占用 14:00–15:00 待审核"' in html
    assert not re.search(r'data-[a-z-]*(user|name|student|booking|id)', html.split('class="mobile-agenda"', 1)[1])
    # My own booking keeps its details and is marked.
    assert "我的预约" in html and MY_PURPOSE in html and "张三" in html and "10:00–11:00" in html


def test_captain_schedule_other_roles_special_accounts_also_anonymised(app, client, occupied):
    with app.app_context():
        create_user("sp001", "特批甲", "approved_member")
    login(client, "sp001")
    html = _schedule(client, occupied, "day")
    for leak in LEAKS + ("张三", MY_PURPOSE):
        assert leak not in html
    assert html.count("已占用") >= 2


def test_admin_and_superadmin_still_see_everything(app, client, occupied):
    login(client, "officer01", staff=True)
    html = _schedule(client, occupied, "day")
    for expected in ("王五", "张三", SECRET_PURPOSE, MY_PURPOSE):
        assert expected in html
    boss = app.test_client()
    login(boss, "admin", SUPER_PASSWORD, staff=True)
    assert "王五" in _schedule(boss, occupied, "week")


def test_checkin_filter_is_staff_only_and_does_not_unlock_details(app, client, occupied):
    login(client, "2021001")
    html = client.get(f"/schedule?lab_id={occupied['lab_id']}&date={FUTURE_DAY.isoformat()}&checkin=missing").get_data(as_text=True)
    assert "王五" not in html and SECRET_PURPOSE not in html


def test_api_conflict_endpoints_do_not_name_other_captains(app, client, occupied):
    import json

    with app.app_context():
        lab = db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, occupied["lab_id"])
        add_booking(lab, db.session.get(User, occupied["other_id"]), start=time(16), end=time(17), purpose="另一个", status="approved")
    headers = api_headers("2021001")
    payload = {"lab_id": occupied["lab_id"], "date": FUTURE_DAY.isoformat(), "start_time": "16:00", "end_time": "17:00", "people_count": 1}

    def dump(resp):  # ensure_ascii=False: Chinese names would show up verbatim if they leaked
        return json.dumps(resp.get_json(), ensure_ascii=False)

    resp = client.post("/api/conflicts", json=payload, headers=headers)
    body = dump(resp)
    assert resp.get_json()["approved_conflicts"], body
    for leak in ("王五", "2021002", "user_name", "student_id", '"id"'):
        assert leak not in body, leak
    assert resp.get_json()["approved_conflicts"][0]["occupied"] is True
    # Pending overlap reported on API booking creation must not name the other captain either.
    payload.update(start_time="14:10", end_time="14:50", purpose="p", captain_phone=CAPTAIN_PHONE, captain_email="c@example.com")
    created = client.post("/api/bookings", json=payload, headers=headers)
    text = dump(created)
    assert created.status_code == 201 and created.get_json()["pending_conflicts"], text
    assert "王五" not in text and "2021002" not in text
    # Staff still get the full conflict details.
    staff = client.post("/api/conflicts", json={**payload, "start_time": "14:10", "end_time": "14:50"}, headers=api_headers("officer01"))
    assert "王五" in dump(staff)


def test_captain_cannot_open_other_bookings_through_any_view(app, client, occupied):
    login(client, "2021001")
    with app.app_context():
        other_id = Booking.query.filter_by(purpose=SECRET_PURPOSE).one().id
    for url in (f"/bookings/{other_id}/edit", f"/review/{other_id}/approve", f"/api/bookings/{other_id}"):
        resp = client.get(url, headers=api_headers("2021001")) if url.startswith("/api") else client.get(url)
        assert SECRET_PURPOSE not in resp.get_data(as_text=True)
        assert "王五" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------- server-side required messages

def test_booking_missing_purpose_and_lab_returns_hints_and_keeps_input(app, client, seed):
    login(client, "2021001")
    data = booking_form(seed["lab_id"], purpose="", captain_phone="13800138000", captain_email="keep@example.com")
    data["lab_id"] = ""
    html = client.post("/bookings/new", data=data).get_data(as_text=True)
    assert "请选择实验室。" in html and "请填写用途。" in html
    assert 'value="keep@example.com"' in html and 'value="13800138000"' in html
    with app.app_context():
        assert Booking.query.count() == 0


def test_booking_other_companion_missing_fields_are_named(app, client, seed):
    login(client, "2021001")
    data = booking_form(seed["lab_id"], include_other="on")
    data["other_name"] = ["钱九"]
    data["other_student_id"] = [""]
    data["other_contact"] = [""]
    html = client.post("/bookings/new", data=data).get_data(as_text=True)
    assert "请填写 学号、联系方式。" in html
    assert 'value="钱九"' in html and "checked" in html.split('name="include_other"', 1)[1][:80]


def test_booking_form_marks_required_fields(app, client, seed):
    login(client, "2021001")
    html = client.get("/bookings/new").get_data(as_text=True)
    for name in ("lab_id", "date", "start_time", "end_time", "captain_phone", "captain_email", "purpose"):
        assert re.search(rf'name="{name}"[^>]*required|required[^>]*name="{name}"', html), name


def test_tool_form_missing_photo_lab_contact_and_tool_choice(app, client, seed):
    login(client, "2021001")
    data = _loan_form(seed["lab_id"], contact="")
    data.pop("borrow_photo")
    data["lab_id"] = ""
    data["tool_name"] = [""]
    data["tool_quantity"] = ["1"]
    resp = client.post("/tools/new", data=data, content_type="multipart/form-data")
    html = resp.get_data(as_text=True)
    assert "请上传照片" in html and "请选择实验室。" in html and "请填写有效的手机号" in html
    assert "请选择工具，或选择「其他」后填写名称。" in html
    with app.app_context():
        assert ToolLoan.query.count() == 0
    other = _loan_form(seed["lab_id"])
    other["tool_catalog_id"] = ["other"]
    other["tool_name"] = [""]
    other["tool_quantity"] = ["1"]
    html = client.post("/tools/new", data=other, content_type="multipart/form-data").get_data(as_text=True)
    assert "选择「其他」时请填写工具名称。" in html


def test_announcement_form_missing_title_and_body_keeps_input(app, client, seed):
    login(client, "officer01", staff=True)
    html = client.post("/announcements/new", data={"publisher_name": "测试发布人", "title": "", "body": "写了一半的内容"}).get_data(as_text=True)
    assert "请填写公告标题。" in html and "写了一半的内容" in html
    html = client.post("/announcements/new", data={"publisher_name": "测试发布人", "title": "只有标题", "body": ""}).get_data(as_text=True)
    assert "请填写公告内容。" in html and 'value="只有标题"' in html
    with app.app_context():
        assert Announcement.query.count() == 0


def test_reject_without_reason_and_other_admin_forms(app, client, seed):
    with app.app_context():
        booking = add_booking(db.session.get(__import__("app.models", fromlist=["Lab"]).Lab, seed["lab_id"]),
                              db.session.get(User, seed["member_id"]))
        bid = booking.id
    login(client, "admin", SUPER_PASSWORD, staff=True)
    resp = client.post(f"/review/{bid}/reject", data={"reason": "   "}, follow_redirects=True)
    assert "拒绝时请填写理由。" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Booking, bid).status == "pending"
    assert "请填写实验室名称。" in client.post("/labs/new", data={"name": "", "capacity": "5", "open_start": "08:00", "open_end": "20:00"}).get_data(as_text=True)
    assert "请填写拉黑原因。" in client.post("/activity/blacklist", data={"kind": "ip", "value": "9.9.9.9", "reason": ""}, follow_redirects=True).get_data(as_text=True)


def test_every_template_form_is_handled_by_the_shared_script(app):
    """The hints live in app.js; make sure it ships the pieces the templates rely on."""
    js = (app.root_path and __import__("pathlib").Path(app.root_path) / "static" / "app.js").read_text(encoding="utf-8")
    assert "requiredHints" in js and "noValidate = true" in js
    for needle in ("请选择 ", "请填写 ", "还有 ", " 项未填写", "form-summary", "input-error", "scrollIntoView", "formnovalidate"):
        assert needle in js, needle
    css = (__import__("pathlib").Path(app.root_path) / "static" / "style.css").read_text(encoding="utf-8")
    assert ".req{" in css and ".field-msg" in css and ".form-summary" in css
