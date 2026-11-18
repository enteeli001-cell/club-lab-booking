from tests.conftest import ADMIN_PASSWORD, STRONG_PASSWORD, SUPER_PASSWORD, login


def test_login_page_shows_system_name_notices_and_two_entrances(client):
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 302
    assert "/login" in home.headers["Location"]

    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "社团实验室预约管理系统" in html
    assert "<title>社团实验室预约管理系统</title>" in html
    assert "注意事项" in html
    assert "仅允许参赛队长进行预约，队员请联系本队队长" in html
    assert "请勿对本网站进行网络攻击、恶意扫描或刷接口" in html
    assert "请妥善保管账号密码，勿借给他人" in html
    assert "预约需经管理员审核，请按时使用、如实填写" in html
    assert "参赛队长登录" in html
    assert 'class="btn-captain"' in html
    assert "管理员登录" in html
    assert "/admin/login" in html


def test_captain_login_success_and_failure(app, client, seed):
    bad = client.post("/login", data={"student_id": "2021001", "password": "wrongpass"}, follow_redirects=True)
    assert bad.status_code == 200
    assert "学号或密码不正确".encode("utf-8") in bad.data

    ok = login(client, "2021001")
    assert ok.status_code == 200
    assert "张三".encode("utf-8") in ok.data
    assert "参赛队长".encode("utf-8") in ok.data
    assert "管理后台".encode("utf-8") not in ok.data


def test_staff_cannot_use_captain_entrance(app, client, seed):
    for student_id, password in (("admin", SUPER_PASSWORD), ("officer01", ADMIN_PASSWORD)):
        resp = client.post(
            "/login",
            data={"student_id": student_id, "password": password},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "管理员登录".encode("utf-8") in resp.data
        assert "管理员登录".encode("utf-8") in resp.data
        assert "请使用下方的「管理员登录」入口".encode("utf-8") in resp.data
        # still on captain login page, not authenticated
        assert client.get("/review/").status_code in (302, 401, 403)


def test_captain_cannot_use_admin_entrance(app, client, seed):
    resp = client.post(
        "/admin/login",
        data={"password": "password123"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "密码不正确".encode("utf-8") in resp.data
    assert client.get("/bookings").status_code == 302


def test_admin_entrance_enters_backend(app, client, seed):
    admin = login(client, "admin", staff=True)
    assert admin.status_code == 200
    assert "管理后台".encode("utf-8") in admin.data
    client.post("/logout")
    officer = login(client, "officer01", staff=True)
    assert officer.status_code == 200
    assert "管理后台".encode("utf-8") in officer.data


def test_unauthenticated_redirects(client):
    resp = client.get("/bookings")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_no_self_registration(client):
    resp = client.get("/register")
    assert resp.status_code == 404
    resp = client.post("/users/new", data={"name": "黑客", "student_id": "x", "role": "admin"}, follow_redirects=False)
    assert resp.status_code in (302, 401, 403, 405)


def test_member_cannot_access_admin_or_review(app, client, seed):
    login(client, "2021001")
    assert client.get("/users/").status_code == 403
    assert client.get("/labs/").status_code == 403
    assert client.get("/review/").status_code == 403
    assert client.get("/stats/").status_code == 403
    assert client.get("/bookings").status_code == 200
    assert client.get("/schedule").status_code == 200


def test_officer_can_review_but_not_manage_users(app, client, seed):
    login(client, "officer01", staff=True)
    assert client.get("/review/").status_code == 200
    assert client.get("/stats/").status_code == 200
    assert client.get("/users/").status_code == 403
    assert client.get("/labs/").status_code == 200


def test_admin_can_access_all(app, client, seed):
    login(client, "admin", staff=True)
    for path in ("/users/", "/labs/", "/review/", "/stats/", "/bookings", "/schedule"):
        assert client.get(path).status_code == 200


def test_change_password(app, client, seed):
    login(client, "2021001")
    resp = client.post(
        "/password",
        data={
            "current_password": "password123",
            "new_password": STRONG_PASSWORD,
            "confirm_password": STRONG_PASSWORD,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    client.post("/logout", follow_redirects=True)
    assert "张三".encode("utf-8") not in login(client, "2021001", "password123").data
    ok = login(client, "2021001", STRONG_PASSWORD)
    assert "张三".encode("utf-8") in ok.data
