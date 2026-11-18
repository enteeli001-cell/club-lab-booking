import io

from openpyxl import Workbook

from app import db
from app.models import User
from tests.conftest import login


def _csv_file(content: str, name="users.csv"):
    return (io.BytesIO(content.encode("utf-8-sig")), name)


def _xlsx_file(rows, name="users.xlsx"):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return (buf, name)


def test_member_cannot_import(app, client, seed):
    login(client, "2021001")
    resp = client.post(
        "/users/import",
        data={"file": _csv_file("姓名,学号,角色\n李四,2021999,普通成员\n")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403


def test_csv_import_with_auto_and_given_password(app, client, seed):
    login(client, "admin", staff=True)
    content = "姓名,学号,角色,初始密码\n李四,2021999,普通成员,\n陈干部,2021888,干部,Given#Pass12x\n"
    resp = client.post(
        "/users/import",
        data={"file": _csv_file(content)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "成功导入".encode("utf-8") in resp.data
    with app.app_context():
        u1 = User.query.filter_by(student_id="2021999").first()
        u2 = User.query.filter_by(student_id="2021888").first()
        assert u1 is not None and u1.role == "member"
        assert u2 is not None and u2.role == "admin"
        assert u2.check_password("Given#Pass12x")
        assert u1.check_password("2021999")
        assert u1.must_change_password is True
        assert u2.must_change_password is False


def test_excel_import(app, client, seed):
    login(client, "admin", staff=True)
    rows = [
        ["姓名", "学号", "角色", "初始密码"],
        ["孙七", "2021777", "member", "excelpass"],
    ]
    resp = client.post(
        "/users/import",
        data={"file": _xlsx_file(rows)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        user = User.query.filter_by(student_id="2021777").first()
        assert user is not None
        assert user.name == "孙七"
        assert user.check_password("excelpass")


def test_import_skips_invalid_role_and_duplicates(app, client, seed):
    login(client, "admin", staff=True)
    content = (
        "姓名,学号,角色\n"
        "张三,2021001,普通成员\n"
        "错误,2021666,超级用户\n"
        "新成员,2021555,普通成员\n"
    )
    resp = client.post(
        "/users/import",
        data={"file": _csv_file(content)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "跳过".encode("utf-8") in resp.data
    with app.app_context():
        assert User.query.filter_by(student_id="2021555").first() is not None
        assert User.query.filter_by(student_id="2021666").first() is None
        assert User.query.filter_by(student_id="2021001").count() == 1


def test_manual_add_user(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post(
        "/users/new",
        data={"name": "手工账号", "student_id": "2021444", "role": "member", "password": "manual123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        user = User.query.filter_by(student_id="2021444").one()
        assert user.check_password("manual123")


def test_manual_add_captain_default_password(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.post(
        "/users/new",
        data={"name": "周八", "student_id": "2021666", "role": "member", "password": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "初始密码等于学号".encode("utf-8") in resp.data
    with app.app_context():
        user = User.query.filter_by(student_id="2021666").one()
        assert user.check_password("2021666")
        assert user.must_change_password is True


def test_import_captain_with_team_roster(app, client, seed):
    login(client, "admin", staff=True)
    content = (
        "姓名,学号,角色,队名,队员1姓名,队员1学号,队员2姓名,队员2学号\n"
        "周队长,2021333,参赛队长,结构先锋队,李四,2021011,周七,2021012\n"
    )
    resp = client.post(
        "/users/import",
        data={"file": _csv_file(content)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "成功导入".encode("utf-8") in resp.data
    with app.app_context():
        from app.models import TeamMember

        captain = User.query.filter_by(student_id="2021333").one()
        assert captain.team_name == "结构先锋队"
        members = TeamMember.query.filter_by(captain_id=captain.id).order_by(TeamMember.student_id).all()
        assert [(m.name, m.student_id) for m in members] == [("李四", "2021011"), ("周七", "2021012")]


def test_download_import_template(app, client, seed):
    login(client, "admin", staff=True)
    resp = client.get("/users/import/template.xlsx")
    assert resp.status_code == 200
    assert "spreadsheet" in (resp.mimetype or "")
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(resp.data))
    header = [cell.value for cell in next(wb.active.iter_rows(max_row=1))]
    assert "队名" in header
    assert "队员1姓名" in header
    assert "队员1学号" in header
