from app import db
from app.models import PasswordResetLog, TeamMember, User
from app.utils import apply_team_roster
from tests.conftest import STRONG_PASSWORD, login

RESET_PASSWORD = "Fresh#2b"


def _attach_roster(app, seed):
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


def _reset_data(name="张三", student_id="2021001", teammate_name="李四", teammate_student_id="2021011", new=RESET_PASSWORD, confirm=None):
    return {
        "name": name,
        "student_id": student_id,
        "teammate_name": teammate_name,
        "teammate_student_id": teammate_student_id,
        "new_password": new,
        "confirm_password": RESET_PASSWORD if confirm is None else confirm,
    }


def test_forgot_prompt_after_consecutive_failures(app, client, seed):
    _attach_roster(app, seed)
    for i in range(2):
        resp = client.post("/login", data={"student_id": "2021001", "password": "wrong-pass"})
        html = resp.data.decode("utf-8")
        assert "学号或密码不正确" in html
        assert "是否忘记密码" not in html
        assert 'id="forgot-prompt"' not in html

    resp = client.post("/login", data={"student_id": "2021001", "password": "wrong-pass"})
    html = resp.data.decode("utf-8")
    assert "是否忘记密码" in html
    assert 'id="forgot-prompt"' in html
    assert "忘记密码" in html
    assert "forgot-password" in html


def test_forgot_prompt_not_shown_for_unknown_student(client, seed):
    for _ in range(3):
        resp = client.post("/login", data={"student_id": "9999999", "password": "wrong-pass"})
    html = resp.data.decode("utf-8")
    assert "是否忘记密码" not in html
    assert 'id="forgot-prompt"' not in html


def test_forgot_password_success_sets_new_password(app, client, seed):
    _attach_roster(app, seed)
    page = client.get("/forgot-password?student_id=2021001")
    assert page.status_code == 200
    assert "重置密码" in page.data.decode("utf-8")
    resp = client.post("/forgot-password", data=_reset_data(), follow_redirects=True)
    html = resp.data.decode("utf-8")
    assert resp.status_code == 200
    assert "密码已重置" in html
    with app.app_context():
        user = User.query.filter_by(student_id="2021001").one()
        assert user.check_password(RESET_PASSWORD)
        assert user.must_change_password is False
        logs = PasswordResetLog.query.filter_by(student_id="2021001").all()
        assert any(log.success for log in logs)
    # can log in with the new password
    login_resp = login(client, "2021001", RESET_PASSWORD)
    assert login_resp.status_code == 200
    assert "退出" in login_resp.data.decode("utf-8") or "预约" in login_resp.data.decode("utf-8")


def test_forgot_password_mismatch_is_generic(app, client, seed):
    _attach_roster(app, seed)
    cases = [
        _reset_data(name="错误姓名"),
        _reset_data(student_id="2021002"),
        _reset_data(teammate_name="不存在"),
        _reset_data(teammate_student_id="0000000"),
    ]
    for data in cases:
        resp = client.post("/forgot-password", data=data)
        html = resp.data.decode("utf-8")
        assert "信息不匹配" in html
        assert "哪一项" not in html
        assert "姓名不正确" not in html
        assert "学号不正确" not in html
        assert "队友不存在" not in html
        assert "密码已重置" not in html
    with app.app_context():
        user = User.query.filter_by(student_id="2021001").one()
        assert user.check_password("password123")
        failures = PasswordResetLog.query.filter_by(student_id="2021001", success=False).count()
        assert failures >= 3
        # one of the cases used 2021002
        assert PasswordResetLog.query.filter_by(student_id="2021002", success=False).count() >= 1


def test_forgot_password_rate_limited(app, client, seed):
    _attach_roster(app, seed)
    app.config["CAPTAIN_RESET_MAX_ATTEMPTS"] = 5
    app.config["CAPTAIN_RESET_WINDOW"] = 3600
    data = _reset_data(name="错误姓名")
    last = None
    for _ in range(5):
        last = client.post("/forgot-password", data=data)
        assert last.status_code == 200
        assert "信息不匹配".encode("utf-8") in last.data
    limited = client.post("/forgot-password", data=data)
    assert limited.status_code == 429
    assert "过于频繁".encode("utf-8") in limited.data
    with app.app_context():
        assert PasswordResetLog.query.filter_by(student_id="2021001").count() >= 6


def test_staff_can_view_reset_audit_captain_cannot(app, client, seed):
    _attach_roster(app, seed)
    client.post("/forgot-password", data=_reset_data(name="错误姓名"))
    login(client, "officer01", staff=True)
    page = client.get("/audit/password-resets")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    assert "密码重置日志" in html
    assert "2021001" in html
    assert "失败" in html
    client.post("/logout")
    login(client, "2021001")
    denied = client.get("/audit/password-resets")
    assert denied.status_code == 403


def test_manual_create_captain_with_team(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post(
        "/users/new",
        data={
            "name": "钱队长",
            "student_id": "2021445",
            "role": "member",
            "password": "",
            "team_name": "创新桥队",
            "member_name": ["郑九", ""],
            "member_student_id": ["2021022", ""],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        user = User.query.filter_by(student_id="2021445").one()
        assert user.team_name == "创新桥队"
        members = TeamMember.query.filter_by(captain_id=user.id).all()
        assert [(m.name, m.student_id) for m in members] == [("郑九", "2021022")]
