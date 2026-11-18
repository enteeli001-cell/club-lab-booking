from app import db
from app.models import User
from app.utils import evaluate_password_rules, validate_new_password
from tests.conftest import ADMIN_PASSWORD, STAFF_NEW_PASSWORD, STRONG_PASSWORD, login


def _member(app, seed):
    with app.app_context():
        return db.session.get(User, seed["member_id"])


def test_evaluate_password_rules_each_condition():
    sid = "2021001"
    old = "Old#pass1"
    good = STRONG_PASSWORD
    results = evaluate_password_rules(good, student_id=sid, current_plain=old, confirm=good)
    assert all(results.values())

    assert evaluate_password_rules("Ab#1", student_id=sid, current_plain=old, confirm="Ab#1")["min_length"] is False
    assert evaluate_password_rules("abcdef!", student_id=sid, current_plain=old, confirm="abcdef!")["has_upper"] is False
    assert evaluate_password_rules("ABCDEF!", student_id=sid, current_plain=old, confirm="ABCDEF!")["has_lower"] is False
    assert evaluate_password_rules("Abcdef", student_id=sid, current_plain=old, confirm="Abcdef")["has_special"] is False
    assert evaluate_password_rules(sid, student_id=sid, current_plain=old, confirm=sid)["not_student_id"] is False
    assert evaluate_password_rules(old, student_id=sid, current_plain=old, confirm=old)["not_old_password"] is False
    assert evaluate_password_rules(good, student_id=sid, current_plain=old, confirm="other#1A")["confirmed"] is False


def test_validate_new_password_messages(app, seed):
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        user.set_password(STRONG_PASSWORD, require_change=False)
        db.session.commit()

        assert validate_new_password(user, "wrong-old", STRONG_PASSWORD, STRONG_PASSWORD) == ["当前密码不正确。"]

        errors = validate_new_password(user, STRONG_PASSWORD, STRONG_PASSWORD, STRONG_PASSWORD)
        assert errors == ["新密码不能与旧密码相同。"]

        errors = validate_new_password(user, STRONG_PASSWORD, "2021001", "2021001")
        assert "新密码不能与学号相同。" in errors
        assert "新密码必须包含大写字母。" in errors
        assert "新密码必须包含小写字母。" in errors
        assert "新密码必须包含至少一个特殊符号。" in errors

        assert "新密码长度不能少于 6 位。" in validate_new_password(user, STRONG_PASSWORD, "Ab#1", "Ab#1")
        assert "新密码必须包含大写字母。" in validate_new_password(user, STRONG_PASSWORD, "abcdef!", "abcdef!")
        assert "新密码必须包含小写字母。" in validate_new_password(user, STRONG_PASSWORD, "ABCDEF!", "ABCDEF!")
        assert "新密码必须包含至少一个特殊符号。" in validate_new_password(user, STRONG_PASSWORD, "Abcdef", "Abcdef")
        assert "两次输入的新密码不一致。" in validate_new_password(user, STRONG_PASSWORD, STRONG_PASSWORD + "x", STRONG_PASSWORD)

        assert validate_new_password(user, STRONG_PASSWORD, "Other#2b", "Other#2b") == []


def test_http_change_password_rejects_each_rule(app, client, seed):
    login(client, "2021001")
    current = "password123"

    def post(new, confirm=None):
        return client.post(
            "/password",
            data={
                "current_password": current,
                "new_password": new,
                "confirm_password": new if confirm is None else confirm,
            },
            follow_redirects=True,
        )

    # 6-char minimum
    resp = post("Ab#1", "Ab#1")
    assert "长度不能少于 6 位".encode("utf-8") in resp.data
    # missing uppercase
    resp = post("abcdef!", "abcdef!")
    assert "必须包含大写字母".encode("utf-8") in resp.data
    # missing lowercase
    resp = post("ABCDEF!", "ABCDEF!")
    assert "必须包含小写字母".encode("utf-8") in resp.data
    # missing special
    resp = post("Abcdef1", "Abcdef1")
    assert "必须包含至少一个特殊符号".encode("utf-8") in resp.data
    # equals student id (also fails composition; message must appear)
    resp = post("2021001", "2021001")
    assert "不能与学号相同".encode("utf-8") in resp.data
    # same as old password, via hash
    resp = post(current, current)
    assert "不能与旧密码相同".encode("utf-8") in resp.data
    # mismatch confirm
    resp = post(STRONG_PASSWORD, "Other#2b")
    assert "两次输入的新密码不一致".encode("utf-8") in resp.data
    # wrong current
    resp = client.post(
        "/password",
        data={
            "current_password": "not-the-old",
            "new_password": STRONG_PASSWORD,
            "confirm_password": STRONG_PASSWORD,
        },
        follow_redirects=True,
    )
    assert "当前密码不正确".encode("utf-8") in resp.data
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert user.check_password(current)

    # valid change
    resp = post(STRONG_PASSWORD, STRONG_PASSWORD)
    assert "密码已修改".encode("utf-8") in resp.data
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        assert user.check_password(STRONG_PASSWORD)
        assert user.must_change_password is False


def test_admin_password_page_is_blocked(app, client, seed):
    login(client, "officer01", staff=True)
    resp = client.post(
        "/password",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_password": STAFF_NEW_PASSWORD,
            "confirm_password": STAFF_NEW_PASSWORD,
        },
        follow_redirects=True,
    )
    assert "管理员不能自行修改密码".encode("utf-8") in resp.data
    with app.app_context():
        user = db.session.get(User, seed["officer_id"])
        assert user.check_password(ADMIN_PASSWORD)


def test_change_password_page_lists_rules(app, client, seed):
    login(client, "2021001")
    page = client.get("/password")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    assert "长度不少于 6 位" in html
    assert "包含大写字母" in html
    assert "包含小写字母" in html
    assert "特殊符号" in html
    assert "不能与学号相同" in html
    assert "不能与当前密码相同" in html
    assert "pw-rules" in html
