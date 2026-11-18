#!/usr/bin/env python3
"""Real-browser check of the required-field hints (throw-away DB, never data/app.db / tunnel / real mail).

Forms exercised: booking (incl. conditional 其他 companion fields), tool borrowing (incl. file + conditional
tool name), announcement publishing (superadmin) and the reject dialog. Writes docs/required_hint_booking_390.png.
"""
import os
import shutil
import sys
import tempfile
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

from capture_claude import Quiet, build  # noqa: E402

PNG = ROOT / "app" / "static" / "favicon.svg"


def main():
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=ROOT) as temp:
        temp = Path(temp)
        app, creds, _ = build(temp)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        with patch("app.mailer.send_via_smtp", side_effect=AssertionError("no real mail")), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            app_ctx = app.app_context(); app_ctx.push()

            def session(width, sid, staff=False):
                mobile = width < 600
                ctx = browser.new_context(viewport={"width": width, "height": 844 if mobile else 900}, is_mobile=mobile,
                                          has_touch=mobile, device_scale_factor=2 if mobile else 1, locale="zh-CN",
                                          timezone_id="Asia/Shanghai")
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                if staff:
                    page.goto(base + "/admin/login", wait_until="networkidle")
                    page.fill("#password", creds[sid])
                else:
                    page.goto(base + "/login", wait_until="networkidle")
                    page.fill("#student_id", sid)
                    page.fill("#password", creds[sid])
                page.locator("button[type=submit]").first.click()
                page.wait_for_load_state("networkidle")
                return ctx, page, errors

            def msgs(page):
                return [t.strip() for t in page.locator(".field-msg").all_inner_texts()]

            # ---- booking form, phone viewport
            ctx, page, errors = session(390, "DEMO001")
            page.goto(base + "/bookings/new", wait_until="networkidle")
            page.evaluate("document.querySelectorAll('.js-dismiss, #announce-overlay').forEach(e => e.remove())")
            assert page.locator("#booking-form").evaluate("f => f.noValidate") is True
            stars = page.locator("#booking-form label .req").count()
            assert stars >= 7, f"red asterisks missing ({stars})"
            for sel in ("#purpose", "#captain_phone", "#captain_email"):
                page.fill(sel, "")
            page.select_option("#lab_id", "")
            page.click("#booking-form button[type=submit]")
            page.wait_for_timeout(700)
            text = page.locator(".form-summary").inner_text()
            assert text.startswith("还有 ") and text.endswith(" 项未填写"), text
            m = msgs(page)
            assert "请选择 实验室" in m and "请填写 用途" in m and any(x.startswith("请填写 队长") for x in m), m
            assert page.locator(".input-error").count() == int(text.split()[1])
            assert page.evaluate("document.activeElement.id") == "lab_id", "cursor must go to the first missing field"
            box = page.locator("#lab_id").bounding_box()
            assert 0 <= box["y"] < 844, "first missing field must be scrolled into view"
            overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, "form overflows at 390px"
            page.screenshot(path=str(temp / "required_hint_booking_390.png"))
            before = page.locator(".input-error").count()
            page.select_option("#lab_id", index=1)
            assert page.locator("#lab_id.input-error").count() == 0, "red frame must vanish once filled"
            page.fill("#purpose", "节点测试")
            assert page.locator(".input-error").count() == before - 2
            assert page.locator(".form-summary").inner_text() == f"还有 {before - 2} 项未填写"
            # conditional 其他: hidden fields are ignored, visible ones are required
            for sel, v in (("#captain_phone", "13800138000"), ("#captain_email", "a@example.com")):
                page.fill(sel, v)
            page.click("#include_other + span")
            if page.locator("#other-rows .other-row").count() == 0:
                page.click("#add-other")
            assert page.locator("#other-rows .other-row").count() == 1
            page.click("#booking-form button[type=submit]")
            page.wait_for_timeout(500)
            m = msgs(page)
            assert m == ["请填写 姓名", "请填写 学号", "请填写 联系方式"], m
            page.fill('input[name="other_name"]', "钱九")
            assert page.locator(".form-summary").inner_text() == "还有 2 项未填写"
            page.click("#include_other + span")  # untick: hidden rows no longer count
            from datetime import timedelta
            from app.utils import today
            page.fill("#date", (today() + timedelta(days=3)).isoformat())
            page.fill("#start_time", "10:00")
            page.fill("#end_time", "10:45")
            page.click("#booking-form button[type=submit]")
            try:
                page.wait_for_url("**/submitted**", timeout=8000)
            except Exception:
                raise AssertionError("booking did not submit: " + " | ".join(page.locator(".flash li, .field-msg").all_inner_texts()))
            assert not errors, errors
            ctx.close()

            # ---- tool borrowing form
            ctx, page, errors = session(390, "DEMO001")
            page.goto(base + "/tools/new", wait_until="networkidle")
            page.fill("#contact", "")
            page.select_option("#lab_id", "")
            page.click("#tool-form button[type=submit]")
            page.wait_for_timeout(500)
            m = msgs(page)
            assert "请选择 实验室" in m and "请填写 本人联系方式" in m and "请选择 借用时照片" in m, m
            assert page.locator("#borrow_photo.input-error").count() == 1
            page.select_option(".js-tool-choice", "other")
            page.click("#tool-form button[type=submit]")
            page.wait_for_timeout(300)
            assert "请填写 其他工具名称" in msgs(page)
            page.set_input_files("#borrow_photo", str(ROOT / "app" / "static" / "favicon.svg"))
            assert page.locator("#borrow_photo.input-error").count() == 0
            assert not errors, errors
            ctx.close()

            # ---- announcement form (superadmin) + reject dialog (admin)
            ctx, page, errors = session(1280, "DEMOSUPER", staff=True)
            page.goto(base + "/announcements/new", wait_until="networkidle")
            page.click("main form button[type=submit]")
            page.wait_for_timeout(300)
            m = msgs(page)
            assert m == ["请填写 标题", "请填写 内容"], m
            assert page.locator(".form-summary").inner_text() == "还有 2 项未填写"
            page.fill("#title", "标题")
            page.fill("#body", "内容")
            assert page.locator(".form-summary").count() == 0
            ctx.close()
            ctx, page, errors = session(390, "DEMOADMIN", staff=True)
            page.goto(base + "/review/", wait_until="networkidle")
            page.evaluate("document.querySelector('#announce-overlay') && document.querySelector('#announce-overlay').remove()")
            page.locator(".js-open-reject").first.click()
            page.click("#reject-form button[type=submit]")
            page.wait_for_timeout(300)
            assert msgs(page) == ["请填写 拒绝理由"], msgs(page)
            assert not errors, errors
            ctx.close()
            browser.close()
        server.shutdown()
        shutil.copy(temp / "required_hint_booking_390.png", ROOT / "docs" / "required_hint_booking_390.png")
        print("OK: all required-hint checks passed; wrote docs/required_hint_booking_390.png")


if __name__ == "__main__":
    main()
