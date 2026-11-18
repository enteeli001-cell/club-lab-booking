import io

from datetime import timedelta

from app import db
from app.models import ToolLoan, User
from app.utils import MINIMAL_PNG_BYTES, PHOTO_MAX_BYTES, local_now
from tests.conftest import CAPTAIN_PHONE, login


def _png(name="borrow.png"):
    return (io.BytesIO(MINIMAL_PNG_BYTES), name)


def _jpeg_oversized():
    payload = b"\xff\xd8\xff" + b"\x00" * (PHOTO_MAX_BYTES + 1)
    return (io.BytesIO(payload), "big.jpg")


def _loan_form(lab_id, **overrides):
    now = local_now().replace(second=0, microsecond=0)
    data = {
        "lab_id": str(lab_id),
        "borrow_at": now.strftime("%Y-%m-%dT%H:%M"),
        "expected_return_at": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "contact": CAPTAIN_PHONE,
        "tool_name": ["扳手", "卡尺"],
        "tool_quantity": ["2", "1"],
        "borrow_photo": _png(),
    }
    data.update(overrides)
    return data


def test_create_loan_and_return(app, client, seed):
    login(client, "2021001")
    resp = client.post("/tools/new", data=_loan_form(seed["lab_id"]), follow_redirects=True, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert "工具借用已登记".encode("utf-8") in resp.data
    with app.app_context():
        loan = ToolLoan.query.filter_by(user_id=seed["member_id"]).one()
        assert loan.status == "borrowing"
        assert loan.items_label.startswith("扳手")
        loan_id = loan.id
        filename = loan.borrow_photo
    photo = client.get(f"/media/{filename}")
    assert photo.status_code == 200

    missing = client.post(f"/tools/{loan_id}/return", data={}, follow_redirects=True)
    assert "请上传照片".encode("utf-8") in missing.data

    done = client.post(
        f"/tools/{loan_id}/return",
        data={"return_photo": _png("back.png")},
        follow_redirects=True,
        content_type="multipart/form-data",
    )
    assert "已登记归还".encode("utf-8") in done.data
    with app.app_context():
        loan = db.session.get(ToolLoan, loan_id)
        assert loan.status == "returned"
        assert loan.return_photo
        assert loan.actual_return_at is not None


def test_photo_type_and_size_rejected(app, client, seed):
    login(client, "2021001")
    bad_type = _loan_form(seed["lab_id"], borrow_photo=(io.BytesIO(b"not-an-image"), "notes.txt"))
    resp = client.post("/tools/new", data=bad_type, follow_redirects=True, content_type="multipart/form-data")
    assert "仅支持 jpg、png、webp".encode("utf-8") in resp.data

    gif = _loan_form(seed["lab_id"], borrow_photo=(io.BytesIO(b"GIF89a........"), "pic.gif"))
    resp = client.post("/tools/new", data=gif, follow_redirects=True, content_type="multipart/form-data")
    assert "仅支持 jpg、png、webp".encode("utf-8") in resp.data

    huge = _loan_form(seed["lab_id"], borrow_photo=_jpeg_oversized())
    resp = client.post("/tools/new", data=huge, follow_redirects=True, content_type="multipart/form-data")
    assert resp.status_code in (200, 413)
    if resp.status_code == 200:
        assert "不能超过 5MB".encode("utf-8") in resp.data
    with app.app_context():
        assert ToolLoan.query.count() == 0


def test_photo_permission(app, client, seed):
    login(client, "2021001")
    client.post("/tools/new", data=_loan_form(seed["lab_id"]), content_type="multipart/form-data")
    with app.app_context():
        loan = ToolLoan.query.one()
        filename = loan.borrow_photo
    client.post("/logout")
    login(client, "2021002")
    denied = client.get(f"/media/{filename}")
    assert denied.status_code == 403
    client.post("/logout")
    login(client, "officer01", staff=True)
    allowed = client.get(f"/media/{filename}")
    assert allowed.status_code == 200
    listing = client.get("/tools/admin")
    assert listing.status_code == 200
    assert "扳手".encode("utf-8") in listing.data
