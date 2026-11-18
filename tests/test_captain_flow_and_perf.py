"""Captain flow continuity (next is kept through login and the forced password change),
static asset caching / compression, and query-count guards for the list pages."""
import gzip
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import event, inspect, text

from app import db
from app.models import Blacklist, Booking, Lab, TeamMember, ToolLoan, ToolLoanItem, User
from tests.conftest import FUTURE_DAY, STRONG_PASSWORD, add_booking, booking_form, create_user, login


@pytest.fixture
def freeze(monkeypatch):
    def _freeze(hour, minute=0, day=FUTURE_DAY):
        fake = datetime(day.year, day.month, day.day, hour, minute)
        monkeypatch.setattr("app.utils.local_now", lambda: fake)
        monkeypatch.setattr("app.routes.checkin.local_now", lambda: fake)
        return fake
    return _freeze


def _initial_password_captain(app, seed):
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        user.set_password("2021001", require_change=True)
        add_booking(db.session.get(Lab, seed["lab_id"]), user, status="approved")
        db.session.commit()
        return db.session.get(Lab, seed["lab_id"]).checkin_token


def _next_of(location):
    return parse_qs(urlsplit(location).query).get("next", [None])[0]


# ------------------------------------------------------------------ captain flow

def test_scan_then_forced_password_change_returns_to_checkin(app, client, seed, freeze):
    token = _initial_password_captain(app, seed)
    scan = f"/checkin/{token}"
    freeze(10, 5)
    resp = client.get(scan)
    assert resp.status_code == 302 and _next_of(resp.headers["Location"]) == scan

    login_page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "扫码签到页面" in login_page and f'name="next" value="{scan}"' in login_page

    resp = client.post(f"/login?next={scan}", data={"student_id": "2021001", "password": "2021001", "next": scan})
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/password") and _next_of(resp.headers["Location"]) == scan

    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert f'name="next" value="{scan}"' in page and "修改初始密码" in page and "flow-steps" in page

    resp = client.post("/password", data={"current_password": "2021001", "new_password": STRONG_PASSWORD,
                                         "confirm_password": STRONG_PASSWORD, "next": scan})
    assert resp.status_code == 302 and resp.headers["Location"] == scan
    assert "确认签到" in client.get(scan).get_data(as_text=True)
    assert "签到成功" in client.post(scan, data={"pledge": "1"}).get_data(as_text=True)


def test_failed_password_change_keeps_next(app, client, seed):
    token = _initial_password_captain(app, seed)
    scan = f"/checkin/{token}"
    client.post("/login", data={"student_id": "2021001", "password": "2021001", "next": scan})
    page = client.post("/password", data={"current_password": "2021001", "new_password": "weak",
                                         "confirm_password": "weak", "next": scan}).get_data(as_text=True)
    assert f'name="next" value="{scan}"' in page
    with app.app_context():
        assert db.session.get(User, seed["member_id"]).must_change_password


def test_pending_password_change_redirect_remembers_requested_page(app, client, seed):
    _initial_password_captain(app, seed)
    client.post("/login", data={"student_id": "2021001", "password": "2021001"})
    resp = client.get("/records?tab=tools")
    assert resp.status_code == 302 and _next_of(resp.headers["Location"]) == "/records?tab=tools"
    # Nav only offers the password page and logout while the change is pending.
    page = client.get("/password").get_data(as_text=True)
    assert "/bookings/new" not in page and 'action="/logout"' in page


@pytest.mark.parametrize("bad", ["https://evil.example.com/", "//evil.example.com", "/logout", "/password",
                                 "/login?next=/x", "/forgot-password", "/static/style.css"])
def test_unsafe_next_values_are_ignored(app, client, seed, bad):
    resp = client.post("/login", data={"student_id": "2021001", "password": "password123", "next": bad})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"


def test_logged_in_login_page_follows_next(app, client, seed):
    login(client, "2021001")
    resp = client.get("/login?next=/records?tab=tools")
    assert resp.status_code == 302 and resp.headers["Location"] == "/records?tab=tools"


def test_forgot_password_keeps_next_back_to_login(app, client, seed):
    with app.app_context():
        db.session.add(TeamMember(captain_id=seed["member_id"], name="李四", student_id="2021009"))
        db.session.commit()
    page = client.get("/forgot-password?student_id=2021001&next=/checkin/abc").get_data(as_text=True)
    assert 'name="next" value="/checkin/abc"' in page
    resp = client.post("/forgot-password", data={"name": "张三", "student_id": "2021001", "teammate_name": "李四",
                                                 "teammate_student_id": "2021009", "new_password": STRONG_PASSWORD,
                                                 "confirm_password": STRONG_PASSWORD, "next": "/checkin/abc"})
    assert resp.status_code == 302 and _next_of(resp.headers["Location"]) == "/checkin/abc"


def test_captain_home_summary_and_entries(app, client, seed):
    with app.app_context():
        user = db.session.get(User, seed["member_id"])
        lab = db.session.get(Lab, seed["lab_id"])
        add_booking(lab, user, status="pending")
        add_booking(lab, user, start=time(14), end=time(15), status="approved")
    login(client, "2021001")
    text_ = client.get("/captain").get_data(as_text=True)
    assert "张三，欢迎你进入测试预约平台" in text_
    for entry in ("测试预约", "向实验室借工具使用登记", "申请记录"):
        assert entry in text_
    assert "summary-tile" in text_ and "下一场测试" in text_


def test_booking_submitted_links_back_into_the_flow(app, client, seed):
    login(client, "2021001")
    resp = client.post("/bookings/new", data=booking_form(seed["lab_id"]), follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert 'id="booking-submitted"' in page
    assert "/records?tab=bookings" in page and "/bookings/new" in page


# ------------------------------------------------------------------ static assets & caching

def test_versioned_static_assets_are_immutable_and_gzipped(app, client, seed):
    html = client.get("/login").get_data(as_text=True)
    import re
    css = re.search(r'href="(/static/style\.css\?v=topnav-[0-9a-f]{12})"', html).group(1)
    js = re.search(r'src="(/static/app\.js\?v=topnav-[0-9a-f]{12})"', html).group(1)
    for url in (css, js):
        resp = client.get(url, headers={"Accept-Encoding": "gzip, br"})
        assert resp.status_code == 200
        assert "immutable" in resp.headers["Cache-Control"] and "max-age=31536000" in resp.headers["Cache-Control"]
        assert resp.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in resp.headers["Vary"]
        assert len(gzip.decompress(resp.data)) > len(resp.data)
    plain = client.get(css)
    assert "Content-Encoding" not in plain.headers and plain.data.startswith(b"/*")
    # Unversioned URLs keep ETag revalidation (no year-long cache).
    assert "immutable" not in client.get("/static/style.css").headers.get("Cache-Control", "")


def test_html_gzip_keeps_no_store(app, client, seed):
    resp = client.get("/login", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Encoding"] == "gzip"
    assert "参赛队长登录" in gzip.decompress(resp.data).decode("utf-8")


def test_tool_photos_are_privately_cached_and_lazy(app, client, seed):
    with app.app_context():
        upload = __import__("pathlib").Path(app.config["UPLOAD_DIR"])
        upload.mkdir(parents=True, exist_ok=True)
        (upload / "abc123.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        loan = ToolLoan(user_id=seed["member_id"], lab_id=seed["lab_id"], contact="13800138000",
                        borrow_at=datetime(2099, 1, 1, 9), expected_return_at=datetime(2099, 1, 1, 12),
                        borrow_photo="abc123.png", status="borrowing")
        db.session.add(loan)
        db.session.commit()
    login(client, "2021001")
    page = client.get("/records?tab=tools").get_data(as_text=True)
    assert 'loading="lazy"' in page
    resp = client.get("/media/abc123.png")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "private, max-age=86400"
    client.post("/logout")
    login(client, "2021002")
    denied = client.get("/media/abc123.png")
    assert denied.status_code == 403 and denied.headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ query counts / indexes

class _Counter:
    def __init__(self, engine):
        self.n = 0
        self.engine = engine
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *a, **k):
        self.n += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._hit)


def _bulk(app, seed, n=40):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        captains = [create_user(f"30{i:03d}", f"队长{i}", "member") for i in range(8)]
        for c in captains:
            db.session.add(TeamMember(captain_id=c.id, name=f"{c.name}队员", student_id=c.student_id + "9"))
        for i in range(n):
            u = captains[i % len(captains)]
            add_booking(lab, u, day=FUTURE_DAY + timedelta(days=i % 5), start=time(8 + i % 10), end=time(9 + i % 10),
                        status="pending" if i % 2 else "approved")
            loan = ToolLoan(user_id=u.id, lab_id=lab.id, contact="13800138000", borrow_at=datetime(2099, 1, 1, 9),
                            expected_return_at=datetime(2099, 1, 1, 12), borrow_photo="x.png", status="borrowing")
            db.session.add(loan)
            db.session.flush()
            db.session.add(ToolLoanItem(loan_id=loan.id, name="电钻", quantity=1, is_other=True))
        db.session.commit()


@pytest.mark.parametrize("path,limit", [("/review/?status=all", 20), ("/review/", 20), ("/tools/admin", 20), ("/stats/?start_date=2099-01-01&end_date=2099-12-31", 20)])
def test_staff_list_pages_have_bounded_queries(app, client, seed, path, limit):
    _bulk(app, seed)
    login(client, "officer01", staff=True)
    client.get(path)
    with app.app_context():
        counter = _Counter(db.engine)
        try:
            assert client.get(path).status_code == 200
        finally:
            counter.close()
    assert counter.n <= limit, f"{path}: {counter.n} SQL statements"


def test_review_list_still_flags_overlapping_pending(app, client, seed):
    with app.app_context():
        lab = db.session.get(Lab, seed["lab_id"])
        a = db.session.get(User, seed["member_id"])
        b = db.session.get(User, seed["other_id"])
        add_booking(lab, a, start=time(10), end=time(11), status="pending")
        add_booking(lab, b, start=time(10, 15), end=time(11, 15), status="pending")
        add_booking(lab, b, start=time(15), end=time(16), status="pending")
    login(client, "officer01", staff=True)
    assert client.get("/review/").get_data(as_text=True).count("与其他待审核预约重叠超过 30 分钟") == 2


def test_performance_indexes_created_additively(app):
    from app import PERFORMANCE_INDEXES, _ensure_performance_indexes
    with app.app_context():
        _ensure_performance_indexes()  # idempotent
        names = {idx["name"] for table in ("bookings", "tool_loans", "security_events", "blacklist_entries")
                 for idx in inspect(db.engine).get_indexes(table)}
        for name, table, _cols in PERFORMANCE_INDEXES:
            if table in ("bookings", "tool_loans", "security_events", "blacklist_entries"):
                assert name in names
        plan = " ".join(str(r) for r in db.session.execute(text(
            "EXPLAIN QUERY PLAN SELECT * FROM bookings WHERE lab_id=1 AND date='2099-06-15' AND status IN ('pending','approved')")))
        assert "ix_bookings_lab_date_status" in plan


def test_blacklist_single_query_blocks_ip_and_device(app, client, seed):
    with app.app_context():
        db.session.add(Blacklist(kind="device", value="dev-blocked", reason="测试", active=True))
        db.session.commit()
    client.set_cookie("lab_device_id", "dev-blocked")
    assert client.get("/login").status_code == 403
    client.set_cookie("lab_device_id", "dev-ok")
    assert client.get("/login").status_code == 200
    with app.app_context():
        db.session.add(Blacklist(kind="ip", value="127.0.0.1", reason="测试", active=True))
        db.session.commit()
    assert client.get("/login").status_code == 403
