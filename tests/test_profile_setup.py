"""Mandatory profile completion for existing and newly issued regular accounts."""
from datetime import timedelta

import pytest

from app import db, verification as vc
from app.models import EmailVerification, MailLog, MaterialPurchase, User
from app.utils import local_now
from tests.conftest import api_headers, login
from tests.test_materials import CODE, session_as


@pytest.fixture(autouse=True)
def require_profile(app, monkeypatch):
    app.config["PROFILE_COMPLETION_REQUIRED"] = True
    monkeypatch.setattr(vc, "generate_code", lambda: CODE)


def setup(client, email="captain@example.com", **extra):
    return client.post("/profile", data={"name": "陈同学", "phone": "13800138000", "email": email, **extra})


@pytest.mark.parametrize("role", ["member", "approved_member", "special_guest"])
def test_existing_accounts_forced_through_profile(app, client, seed, role):
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        user.role = role
        user.last_phone = "13800138000"
        user.last_email = "captain@example.com"  # imported contacts do not prove ownership
        db.session.commit()
    response = login(client, "2021001")
    assert "完善个人信息" in response.get_data(as_text=True)
    for path in ("/", "/captain", "/bookings/new", "/materials/claim", "/materials/buy", "/schedule", "/no-such-page"):
        response = client.get(path)
        assert response.status_code == 302 and "/profile" in response.location, path
    response = client.get("/profile?lang=en")
    assert response.status_code == 200
    assert "Complete your profile" in response.get_data(as_text=True)
    assert client.get("/static/materials.js").status_code == 200
    assert client.post("/logout").status_code == 302


def test_complete_profile_code_is_hashed_single_use_and_mail_redacted(app, client, seed):
    session_as(client, seed["member_id"])
    assert setup(client).location.endswith("/profile/verify")
    assert client.get("/captain").status_code == 302
    with app.app_context():
        row = EmailVerification.query.one()
        assert row.code_hash and row.code_hash != CODE
        assert row.sent_at and row.expires_at - row.sent_at <= timedelta(minutes=5, seconds=1)
        assert CODE not in MailLog.query.one().body
    client.post("/profile/verify", data={"code": CODE})
    assert client.get("/captain").status_code == 200
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert user.profile_complete and user.profile_completed_at
        assert user.verified_email == user.last_email == "captain@example.com"
        assert EmailVerification.query.one().code_hash is None
        assert EmailVerification.query.one().status == "verified"
    assert client.post("/profile/verify", data={"code": CODE}).location.endswith("/profile")


@pytest.mark.parametrize("overrides", [{"name": ""}, {"name": "甲" * 65}, {"phone": "12800138000"}, {"phone": "1380013800"}, {"phone": "+8613800138000"}, {"email": "not-an-email"}])
def test_profile_validation(app, client, seed, overrides):
    session_as(client, seed["member_id"])
    response = setup(client, **overrides)
    assert response.status_code == 200
    with app.app_context(): assert EmailVerification.query.count() == 0


@pytest.mark.parametrize("case", ["expired", "locked", "wrong_shape"])
def test_profile_code_expiry_attempt_limit_and_shape(app, client, seed, case):
    session_as(client, seed["member_id"])
    setup(client)
    if case == "expired":
        with app.app_context():
            EmailVerification.query.one().expires_at = local_now() - timedelta(seconds=1)
            db.session.commit()
    if case == "locked":
        for _ in range(5): client.post("/profile/verify", data={"code": "000000"})
    client.post("/profile/verify", data={"code": "abcdef" if case == "wrong_shape" else CODE})
    with app.app_context():
        assert not db.session.get(User, seed["member_id"]).profile_complete
        if case == "locked": assert EmailVerification.query.one().attempts == 5


def test_profile_resend_has_cooldown_and_cap(app, client, seed):
    session_as(client, seed["member_id"])
    setup(client)
    client.post("/profile/resend")
    with app.app_context():
        row = EmailVerification.query.one()
        assert row.send_count == 1
        row.sent_at = local_now() - timedelta(seconds=61)
        db.session.commit()
    client.post("/profile/resend")
    with app.app_context():
        row = EmailVerification.query.one()
        assert row.send_count == 2
        row.sent_at = local_now() - timedelta(seconds=61)
        row.send_count = 5
        db.session.commit()
    client.post("/profile/resend")
    with app.app_context(): assert EmailVerification.query.one().send_count == 5


def test_changed_email_blocks_system_and_cancels_old_purchase(app, client, seed):
    session_as(client, seed["member_id"])
    setup(client)
    client.post("/profile/verify", data={"code": CODE})
    with app.app_context():
        db.session.add(MaterialPurchase(user_id=seed["member_id"], email_to="captain@example.com", status="pending"))
        db.session.commit()
    setup(client, "new@example.com")
    assert client.get("/captain").status_code == 302
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert not user.email_verified and not user.profile_complete
        assert MaterialPurchase.query.one().status == "cancelled"
    client.post("/profile/verify", data={"code": CODE})
    with app.app_context():
        assert db.session.get(User, seed["member_id"]).verified_email == "new@example.com"
        assert db.session.get(User, seed["member_id"]).profile_complete


def test_api_cannot_bypass_and_staff_are_exempt(app, client, seed):
    response = client.get("/api/me", headers=api_headers("2021001"))
    assert response.status_code == 403
    assert "完善个人信息" in response.json["error"]
    for key in ("admin_id", "officer_id"):
        session_as(client, seed[key])
        assert client.get("/profile/people").status_code == 200
        assert client.get("/").status_code == 200


def test_admin_sees_contacts_and_verification_status(app, client, seed):
    session_as(client, seed["member_id"])
    setup(client)
    client.post("/profile/verify", data={"code": CODE})
    session_as(client, seed["officer_id"])
    response = client.get("/profile/people")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "13800138000" in text and "captain@example.com" in text and "邮箱已验证" in text


def test_profile_safe_next_and_persistent_send_limit(app, client, seed):
    session_as(client, seed["member_id"])
    setup(client, next="https://example.net/escape")
    response = client.post("/profile/verify", data={"code": CODE, "next": "https://example.net/escape"})
    assert "example.net" not in response.location
    with app.app_context():
        row = EmailVerification.query.one()
        row.send_count = 10
        row.sent_at = local_now()
        db.session.commit()
    response = setup(client, "changed@example.com")
    assert response.status_code == 200
    with app.app_context(): assert EmailVerification.query.count() == 1


def test_shared_codes_are_bound_to_purpose_record_and_address(app):
    with app.app_context():
        digest = vc.code_digest("profile-email", 1, CODE, "a@example.com")
        assert vc.digest_matches(digest, "profile-email", 1, CODE, "a@example.com")
        assert not vc.digest_matches(digest, "material-purchase", 1, CODE, "a@example.com")
        assert not vc.digest_matches(digest, "profile-email", 2, CODE, "a@example.com")
        assert not vc.digest_matches(digest, "profile-email", 1, CODE, "b@example.com")


def test_wipe_clears_new_profile_and_codes(app, client, seed):
    session_as(client, seed["member_id"])
    setup(client)
    client.post("/profile/verify", data={"code": CODE})
    with app.app_context():
        from app.wipe import wipe_personal_data
        wipe_personal_data()
        user = db.session.get(User, seed["member_id"])
        assert user.verified_email is user.email_verified_at is user.profile_completed_at is None
        assert EmailVerification.query.count() == 0
