#!/usr/bin/env python3
"""Screenshots + real-browser checks of the captain flow, on a throw-away database.

Never touches data/app.db, production credentials, the tunnel or real mail; demo passwords
are random and only kept in memory. Writes docs/claude_<page>_<1280|390>.png.

Checks (fail loudly before any image is written):
  * captain login shows the masked name after typing the student ID;
  * scanning a check-in QR code while logged out, logging in with the initial password,
    and completing the forced password change lands back on the check-in page and checks in;
  * desktop navigation stays a single horizontal top bar; the phone menu opens and closes;
  * no page overflows horizontally at 390px.
"""
import os
import secrets
import shutil
import sys
import tempfile
from datetime import time, timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
(ROOT / ".runtime-tests").mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(ROOT / ".runtime-tests")
tempfile.tempdir = None

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import WSGIRequestHandler, make_server  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import AppSetting, Booking, Lab, TeamMember, ToolCatalog, ToolLoan, ToolLoanItem, User  # noqa: E402
from app.utils import local_now  # noqa: E402

SHOTS = [  # (name, role, path)
    ("captain_login", None, "/login"),
    ("captain_home", "captain", "/captain"),
    ("booking_form", "captain", "/bookings/new"),
    ("admin_home", "admin", "/"),
    ("superadmin_home", "super", "/"),
]
WIDTHS = {1280: (1280, 900, False), 390: (390, 844, True)}


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def build(directory: Path):
    app = create_app({"TESTING": True, "DATA_DIR": str(directory / "data"), "LOG_DIR": str(directory / "logs"),
                      "SECRET_KEY": secrets.token_hex(32), "API_TOKEN": secrets.token_hex(32),
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(directory / "demo.db")})
    creds = {}
    with app.app_context():
        db.session.get(AppSetting, "checkin_enabled_at").value = "2000-01-01T00:00:00"
        users = {}
        for sid, name, role, team in [("DEMO001", "陈思远", "member", "逐光结构队"), ("DEMO002", "林雨桐", "member", "飞梁结构队"),
                                      ("DEMOADMIN", "李老师", "admin", None), ("DEMOSUPER", "总管理员", "superadmin", None)]:
            password = secrets.token_urlsafe(18) + "Aa!"
            creds[sid] = password
            user = User(student_id=sid, name=name, role=role, team_name=team, last_email="captain@example.com",
                        last_phone="13800138000")
            user.set_password(password, require_change=False)
            db.session.add(user)
            users[sid] = user
        # Fresh captain with the initial password (= student ID) for the scan → forced change flow.
        fresh = User(student_id="DEMO003", name="周子航", role="member", team_name="穿梭桁架队")
        fresh.set_password("DEMO003", require_change=True)
        db.session.add(fresh)
        users["DEMO003"] = fresh
        for i, (n, sid) in enumerate([("王一鸣", "2023101"), ("赵可欣", "2023102"), ("孙浩然", "2023103"), ("钱思琪", "2023104")]):
            db.session.add(TeamMember(captain=users["DEMO001"], name=n, student_id=sid))
        labs = [Lab(name="结构测试实验室", capacity=12, open_start=time(0, 0), open_end=time(23, 59), enabled=True,
                    contact_name="张老师", contact_phone="13911112222"),
                Lab(name="材料力学实验室", capacity=8, open_start=time(8, 0), open_end=time(22, 0), enabled=True)]
        db.session.add_all(labs)
        tools = [ToolCatalog(name=n, total_quantity=q, is_major=True) for n, q in (("电钻", 6), ("游标卡尺", 12), ("热熔胶枪", 10))]
        db.session.add_all(tools)
        db.session.flush()
        now = local_now().replace(second=0, microsecond=0)

        def book(user, lab, start, minutes, status="approved", purpose="模型承载测试"):
            end = start + timedelta(minutes=minutes)
            b = Booking(lab=lab, user=user, date=start.date(), start_time=start.time(), end_time=end.time(), purpose=purpose,
                        people_count=2, captain_phone="13800138000", captain_email="captain@example.com", status=status)
            db.session.add(b)
            return b

        book(users["DEMO001"], labs[0], now + timedelta(days=1, hours=1), 60, purpose="节点连接测试")
        book(users["DEMO001"], labs[1], now + timedelta(days=2), 45, status="pending", purpose="振动台测试")
        book(users["DEMO002"], labs[0], now + timedelta(days=1, hours=3), 60, status="pending")
        book(users["DEMO003"], labs[0], now - timedelta(minutes=5), 60, purpose="桁架加载测试")   # open for check-in now
        loan = ToolLoan(user=users["DEMO001"], lab=labs[0], team_name="逐光结构队", contact="13800138000", borrow_at=now - timedelta(hours=1),
                        expected_return_at=now + timedelta(hours=2), borrow_photo="demo.png", status="borrowing")
        db.session.add(loan)
        db.session.add(ToolLoanItem(loan=loan, catalog_id=tools[0].id, name="电钻", quantity=1))
        db.session.commit()
        token = labs[0].checkin_token
    return app, creds, token


def main():
    docs = ROOT / "docs"
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=ROOT) as temp:
        temp = Path(temp)
        app, creds, token = build(temp)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        out = temp / "shots"
        out.mkdir()
        try:
            with patch("app.mailer.send_via_smtp", side_effect=AssertionError("no real mail")), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)

                def new(width):
                    w, h, mobile = WIDTHS[width]
                    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile,
                                              device_scale_factor=2 if mobile else 1, locale="zh-CN",
                                              timezone_id="Asia/Shanghai")
                    page = ctx.new_page()
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    return ctx, page, errors

                def sign_in(page, role):
                    if role == "captain":
                        page.goto(base + "/login", wait_until="networkidle")
                        page.fill("#student_id", "DEMO001")
                        page.fill("#password", creds["DEMO001"])
                    else:
                        page.goto(base + "/admin/login", wait_until="networkidle")
                        page.fill("#password", creds["DEMOADMIN" if role == "admin" else "DEMOSUPER"])
                    page.locator("button[type=submit]").first.click()
                    page.wait_for_load_state("networkidle")

                def no_overflow(page, label):
                    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    assert overflow <= 1, f"{label}: horizontal overflow {overflow}px"

                # 1) Real-browser flow: scan while logged out -> initial-password login -> forced change -> check-in.
                ctx, page, errors = new(390)
                scan = "/checkin/" + token
                page.goto(base + scan, wait_until="networkidle")
                assert "/login" in page.url and "next=" in page.url, page.url
                assert page.locator(".next-banner.next-checkin").is_visible()
                page.fill("#student_id", "DEMO003")
                page.locator("#captain-greeting.ok").wait_for(timeout=5000)
                assert "周*航" in page.locator("#captain-greeting").inner_text()
                page.fill("#password", "DEMO003")
                page.locator("button[type=submit]").click()
                page.wait_for_load_state("networkidle")
                assert "/password" in page.url and "next=" in page.url, page.url
                assert page.locator(".flow-steps .current").is_visible()
                new_password = "Scan#" + secrets.token_hex(4) + "Ab"
                page.fill("#current_password", "DEMO003")
                page.fill("#new_password", new_password)
                page.fill("#confirm_password", new_password)
                page.locator("#pw-submit").click()
                page.wait_for_load_state("networkidle")
                assert page.url.endswith(scan), page.url
                assert page.locator(".checkin-submit").is_disabled()  # safety pledge must be ticked first
                page.locator("#ck-direct-pledge").check()
                page.locator(".checkin-submit").click()
                page.wait_for_load_state("networkidle")
                assert "签到成功" in page.content()
                assert not errors, errors
                ctx.close()

                # 2) Screenshots at both widths.
                for name, role, path in SHOTS:
                    for width in WIDTHS:
                        ctx, page, errors = new(width)
                        if role:
                            sign_in(page, role)
                        page.goto(base + path, wait_until="networkidle")
                        if name == "captain_login":
                            page.fill("#student_id", "DEMO001")
                            page.locator("#captain-greeting.ok").wait_for(timeout=5000)
                            page.locator("#student_id").blur()
                        if width == 1280 and role:
                            nav = page.locator("#site-nav").bounding_box()
                            assert nav and nav["width"] > 900, f"{name}: desktop nav not horizontal {nav}"
                            tops = page.evaluate("Array.from(document.querySelectorAll('#site-nav > a')).map(a => a.getBoundingClientRect().top)")
                            assert tops and min(tops) < 140, f"{name}: nav not in the top bar"
                        page.screenshot(path=str(out / f"claude_{name}_{width}.png"), full_page=True)
                        if width == 390:
                            no_overflow(page, name)
                            if role:
                                assert not page.locator("#site-nav").is_visible()
                                page.locator("#nav-toggle").click()
                                assert page.locator("#site-nav").is_visible()
                                if name == "captain_home":
                                    page.screenshot(path=str(out / "claude_captain_menu_390.png"))
                                page.keyboard.press("Escape")
                                assert not page.locator("#site-nav").is_visible()
                        assert not errors, (name, width, errors)
                        ctx.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
        for png in sorted(out.glob("claude_*.png")):
            shutil.copyfile(png, docs / png.name)
            print("written", (docs / png.name).relative_to(ROOT))
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    main()
