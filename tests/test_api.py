from datetime import time

from app import db
from app.models import Booking, Lab, User
from tests.conftest import API_TOKEN, FUTURE_DAY, add_booking, api_headers


def test_health_is_public(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["status"] == "up"


def test_api_requires_bearer_token(client, seed):
    resp = client.get("/api/labs", headers={"X-Student-Id": "admin"})
    assert resp.status_code == 401
    resp = client.get("/api/labs", headers=api_headers("admin", token="wrong-token"))
    assert resp.status_code == 401


def test_api_labs_crud_and_permissions(app, client, seed):
    member_headers = api_headers("2021001")
    listed = client.get("/api/labs", headers=member_headers)
    assert listed.status_code == 200
    assert len(listed.get_json()["labs"]) == 1

    created = client.post(
        "/api/labs",
        headers=member_headers,
        json={"name": "禁建实验室", "capacity": 5, "open_start": "08:00", "open_end": "18:00"},
    )
    assert created.status_code == 403

    admin_headers = api_headers("admin")
    created = client.post(
        "/api/labs",
        headers=admin_headers,
        json={"name": "新实验室", "capacity": 5, "open_start": "08:00", "open_end": "18:00", "enabled": True},
    )
    assert created.status_code == 201
    lab_id = created.get_json()["lab"]["id"]

    updated = client.put(
        f"/api/labs/{lab_id}",
        headers=admin_headers,
        json={"capacity": 6, "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.get_json()["lab"]["capacity"] == 6
    assert updated.get_json()["lab"]["enabled"] is False

    # members should not see disabled labs
    listed = client.get("/api/labs", headers=member_headers)
    names = [lab["name"] for lab in listed.get_json()["labs"]]
    assert "新实验室" not in names

    deleted = client.delete(f"/api/labs/{lab_id}", headers=admin_headers)
    assert deleted.status_code == 200
    with app.app_context():
        assert db.session.get(Lab, lab_id) is None


def test_api_booking_crud_conflicts_review_and_stats(app, client, seed):
    member = api_headers("2021001")
    other = api_headers("2021002")
    officer = api_headers("officer01")
    payload = {
        "lab_id": seed["lab_id"],
        "date": FUTURE_DAY.isoformat(),
        "start_time": "10:00",
        "end_time": "11:00",
        "purpose": "接口预约",
        "people_count": 2,
        "captain_email": "captain@example.com",
        "captain_phone": "13800138000",
    }
    created = client.post("/api/bookings", headers=member, json=payload)
    assert created.status_code == 201
    booking_id = created.get_json()["booking"]["id"]
    assert created.get_json()["booking"]["status"] == "pending"

    # adjacent is not a conflict
    check_adj = client.post(
        "/api/conflicts",
        headers=member,
        json={
            "lab_id": seed["lab_id"],
            "date": FUTURE_DAY.isoformat(),
            "start_time": "11:00",
            "end_time": "12:00",
            "people_count": 1,
        },
    )
    assert check_adj.status_code == 200
    assert check_adj.get_json()["valid"] is True
    assert check_adj.get_json()["approved_conflicts"] == []

    # pending overlap is valid with pending_conflicts
    overlap_pending = client.post(
        "/api/bookings",
        headers=other,
        json=payload | {"purpose": "同样时段待审"},
    )
    assert overlap_pending.status_code == 201
    assert "warning" in overlap_pending.get_json()
    other_id = overlap_pending.get_json()["booking"]["id"]

    # member cannot see other's booking
    hidden = client.get(f"/api/bookings/{other_id}", headers=member)
    assert hidden.status_code == 403

    # member cannot approve
    assert client.post(f"/api/bookings/{booking_id}/approve", headers=member).status_code == 403

    approved = client.post(f"/api/bookings/{booking_id}/approve", headers=officer)
    assert approved.status_code == 200
    assert approved.get_json()["booking"]["status"] == "approved"

    # now the overlapping pending cannot be approved
    blocked = client.post(f"/api/bookings/{other_id}/approve", headers=officer)
    assert blocked.status_code == 400
    assert "冲突" in blocked.get_json()["error"]

    # conflict endpoint reports approved overlap; adjacent still ok
    check_overlap = client.post(
        "/api/conflicts",
        headers=officer,
        json={
            "lab_id": seed["lab_id"],
            "date": FUTURE_DAY.isoformat(),
            "start_time": "10:29",
            "end_time": "11:29",
            "people_count": 1,
        },
    )
    data = check_overlap.get_json()
    assert data["valid"] is False
    assert data["approved_conflicts"]

    rejected = client.post(
        f"/api/bookings/{other_id}/reject",
        headers=officer,
        json={"reason": "时间冲突"},
    )
    assert rejected.status_code == 200
    assert rejected.get_json()["booking"]["status"] == "rejected"

    # another booking to cancel
    later = client.post(
        "/api/bookings",
        headers=member,
        json=payload | {"start_time": "14:00", "end_time": "15:00", "purpose": "稍后取消"},
    )
    cancel_id = later.get_json()["booking"]["id"]
    cancelled = client.delete(f"/api/bookings/{cancel_id}", headers=member)
    assert cancelled.status_code == 200
    assert cancelled.get_json()["booking"]["status"] == "cancelled"

    stats = client.get(
        f"/api/stats?start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}",
        headers=officer,
    )
    assert stats.status_code == 200
    body = stats.get_json()
    assert body["totals"]["count"] == 1
    assert body["totals"]["minutes"] == 60

    member_stats = client.get(
        f"/api/stats?start_date={FUTURE_DAY.isoformat()}&end_date={FUTURE_DAY.isoformat()}",
        headers=member,
    )
    assert member_stats.status_code == 403


def test_api_create_user(app, client, seed):
    resp = client.post(
        "/api/users",
        headers=api_headers("admin"),
        json={"name": "接口成员", "student_id": "2021333", "role": "member"},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["password_auto_generated"] is True
    assert body["user"]["student_id"] == "2021333"
    assert body["initial_password"] == "2021333"
    with app.app_context():
        user = User.query.filter_by(student_id="2021333").one()
        assert user.check_password("2021333")
        assert user.must_change_password is True
