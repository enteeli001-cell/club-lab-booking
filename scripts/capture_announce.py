#!/usr/bin/env python3
"""Announcement screenshots on a throw-away database (never data/app.db, the tunnel or real mail).

Writes docs/announce_{list_captain_390,popup_390,admin_reads_1280,super_manage_1280,form_super_1280}.png.
Also asserts a few things in the real browser (popup blocks until "我已知晓", no horizontal overflow at 390px).
"""
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
(ROOT / ".runtime-tests").mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(ROOT / ".runtime-tests")
tempfile.tempdir = None

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import db  # noqa: E402
from app.announcements import scan_announcement_mail  # noqa: E402
from app.models import Announcement, AnnouncementAck, AnnouncementRead, User  # noqa: E402
from app.utils import local_now  # noqa: E402
from capture_claude import Quiet, build  # noqa: E402


def seed(app):
    with app.app_context():
        u = {x.student_id: x for x in User.query.all()}
        now = local_now()

        def add(title, body, who, level, pinned, ago_hours, **kw):
            a = Announcement(title=title, body=body, level=level, pinned=pinned, created_by_id=u[who].id,
                             created_at=now - timedelta(hours=ago_hours), **kw)
            db.session.add(a)
            db.session.flush()
            return a

        a1 = add("本周六实验室开放时间调整", "本周六实验室 9:00–17:00 开放，需提前在系统里预约，使用完毕请整理场地。", "DEMOADMIN", 1, False, 30, publisher_name="实验室办公室")
        a2 = add("安全提醒：使用电钻必须佩戴护目镜", "借用电钻、热熔胶枪等工具时，必须佩戴护目镜和手套。\n违规将暂停借用资格。", "DEMOSUPER", 2, True, 20)
        a3 = add("【重要】测试时间安排", "测试预约窗口：11 月 20 日起开放。\n请各队长在 11 月 18 日前确认队员名单，逾期视为放弃。", "DEMOSUPER", 3, True, 10, mail_revision=1)
        a4 = add("工具归还提醒", "借用的工具请在预计归还时间前归还，逾期会计入记录。", "DEMOADMIN", 1, False, 3, publisher_name="结构工程协会")
        a4.updated_by_id = u["DEMOSUPER"].id
        a4.updated_at = now - timedelta(hours=1)
        # DEMO001 has read/confirmed everything but a4; DEMO002 has confirmed nothing (sees the popup).
        for a in (a1, a2, a3):
            db.session.add(AnnouncementRead(announcement_id=a.id, user_id=u["DEMO001"].id, read_at=now - timedelta(hours=2)))
        for a in (a2, a3):
            db.session.add(AnnouncementAck(announcement_id=a.id, user_id=u["DEMO001"].id, revision=1, acked_at=now - timedelta(hours=2)))
        db.session.add(AnnouncementRead(announcement_id=a1.id, user_id=u["DEMO003"].id, read_at=now - timedelta(minutes=40)))
        db.session.commit()
        scan_announcement_mail()  # example.com only: lands in the throw-away outbox


def main():
    docs = ROOT / "docs"
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=ROOT) as temp:
        temp = Path(temp)
        app, creds, _token = build(temp)
        seed(app)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        out = temp / "shots"
        out.mkdir()
        with patch("app.mailer.send_via_smtp", side_effect=AssertionError("no real mail")), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)

            def session(width, sid, staff=False):
                mobile = width < 600
                ctx = browser.new_context(viewport={"width": width, "height": 844 if mobile else 900}, is_mobile=mobile,
                                          has_touch=mobile, device_scale_factor=2 if mobile else 1, locale="zh-CN",
                                          timezone_id="Asia/Shanghai")
                page = ctx.new_page()
                if staff:
                    page.goto(base + "/admin/login", wait_until="networkidle")
                    page.fill("#password", creds[sid])
                else:
                    page.goto(base + "/login", wait_until="networkidle")
                    page.fill("#student_id", sid)
                    page.fill("#password", creds[sid])
                page.locator("button[type=submit]").first.click()
                page.wait_for_load_state("networkidle")
                return ctx, page

            def overflow(page):
                return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")

            # 1. captain list (DEMO001 already confirmed everything: no popup)
            ctx, page = session(390, "DEMO001")
            page.goto(base + "/announcements/", wait_until="networkidle")
            assert page.locator("#announce-overlay").count() == 0
            assert overflow(page) <= 1, "list overflows at 390px"
            page.screenshot(path=str(out / "announce_list_captain_390.png"), full_page=True)
            ctx.close()
            # 2. forced popup for DEMO002, must block until acknowledged
            ctx, page = session(390, "DEMO002")
            page.goto(base + "/captain", wait_until="networkidle")
            assert page.locator("#announce-overlay").is_visible()
            assert overflow(page) <= 1, "popup overflows at 390px"
            page.screenshot(path=str(out / "announce_popup_390.png"))
            page.get_by_role("button", name="我已知晓").click()
            page.wait_for_load_state("networkidle")
            assert page.locator("#announce-overlay").count() == 0
            page.goto(base + "/announcements/", wait_until="networkidle")
            assert page.locator("#announce-overlay").count() == 0, "popup came back after confirming"
            ctx.close()
            # 3. admin read report (own level-1 notice)
            ctx, page = session(1280, "DEMOADMIN", staff=True)
            page.goto(base + "/announcements/manage", wait_until="networkidle")
            # Admins get the forced popup too (level 2/3 from the superadmin); confirm it first.
            assert page.locator("#announce-overlay").is_visible()
            page.get_by_role("button", name="我已知晓").click()
            page.wait_for_load_state("networkidle")
            assert page.locator("#announce-overlay").count() == 0
            link = page.locator("a", has_text="阅读情况").first
            assert page.locator("tbody tr").count() == 2, "admin must only see own announcements"
            link.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(out / "announce_admin_reads_1280.png"), full_page=True)
            ctx.close()
            # 4/5. superadmin manage + form
            ctx, page = session(1280, "DEMOSUPER", staff=True)
            page.goto(base + "/announcements/manage", wait_until="networkidle")
            assert page.locator("tbody tr").count() == 4
            page.screenshot(path=str(out / "announce_super_manage_1280.png"), full_page=True)
            page.goto(base + "/announcements/new", wait_until="networkidle")
            page.check('input[name="level"][value="2"]')
            page.screenshot(path=str(out / "announce_form_super_1280.png"), full_page=True)
            ctx.close()
            browser.close()
        server.shutdown()
        for shot in out.glob("*.png"):
            shutil.copy(shot, docs / shot.name)
            print("written", docs / shot.name)


if __name__ == "__main__":
    main()
