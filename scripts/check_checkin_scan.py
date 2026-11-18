#!/usr/bin/env python3
"""Browser check of the in-page QR check-in, pledge, location mode and auto refresh (throw-away DB only).

Uses Chromium's fake camera (a y4m clip of the lab QR code), the file-upload fallback, and context geolocation.
Writes docs/checkin_pledge_390.png, checkin_button_390.png, checkin_geo_390.png, wipe_confirm_1280.png,
lab_geo_settings_1280.png and login_privacy_390.png. Never touches data/app.db, the tunnel or real mail.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import patch

os.environ["AUTO_REFRESH_SECONDS"] = "0"  # switched on per phase below
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import db  # noqa: E402
from app.checkin import qr_png_bytes  # noqa: E402
from app.models import Booking, Lab, User  # noqa: E402
from app.utils import local_now  # noqa: E402
from capture_claude import Quiet, build  # noqa: E402

LAT, LNG = 31.230416, 121.473701
M_PER_DEG = 111194.93


def wait_marker_gone(page, seconds):
    """Poll with evaluate (wait_for_function needs eval, which the page CSP forbids)."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if page.evaluate("window.__marker === undefined"):
                return
        except Exception:
            pass  # page is mid-reload
        time.sleep(0.3)
    raise AssertionError("page did not reload; blocked by: %r" % page.evaluate("window.__autoRefresh.blockedReason()"))


def main():
    docs = ROOT / "docs"
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=ROOT) as temp:
        temp = Path(temp)
        app, creds, _t = build(temp)
        app.config["CHECKIN_RATE_LIMIT"] = 10000
        with app.app_context():
            lab = Lab.query.filter_by(name="材料力学实验室").one()
            lab.geo_lat, lab.geo_lng, lab.geo_radius_m, lab.geo_required = LAT, LNG, 30, True
            now = local_now().replace(second=0, microsecond=0)
            for sid in ("DEMO001", "DEMO002"):
                user = User.query.filter_by(student_id=sid).one()
                start = now - timedelta(minutes=5)
                db.session.add(Booking(lab=lab, user=user, date=start.date(), start_time=start.time(),
                                       end_time=(start + timedelta(minutes=55)).time(), purpose="模型承载测试", people_count=2,
                                       status="approved"))
            db.session.commit()
            token = lab.checkin_token
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        qr_png = temp / "qr.png"
        qr_png.write_bytes(qr_png_bytes(f"{base}/checkin/{token}", scale=8))
        y4m = temp / "qr.y4m"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(qr_png), "-vf",
                        "scale=480:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:white,format=yuv420p",
                        "-t", "20", "-r", "10", str(y4m)], check=True)
        out = temp / "shots"
        out.mkdir()
        with patch("app.mailer.send_via_smtp", side_effect=AssertionError("no real mail")), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-video-capture={y4m}"])

            def ctx_for(width, geo=None, camera=True):
                mobile = width < 600
                perms = (["camera"] if camera else []) + (["geolocation"] if geo else [])
                ctx = browser.new_context(viewport={"width": width, "height": 844 if mobile else 900}, is_mobile=mobile, has_touch=mobile,
                                          device_scale_factor=2 if mobile else 1, locale="zh-CN", timezone_id="Asia/Shanghai",
                                          permissions=perms, geolocation=geo)
                return ctx, ctx.new_page()

            def login(page, sid, staff=False):
                if staff:
                    page.goto(base + "/admin/login", wait_until="networkidle")
                    page.fill("#password", creds[sid])
                else:
                    page.goto(base + "/login", wait_until="networkidle")
                    page.fill("#student_id", sid)
                    page.fill("#password", creds[sid])
                page.locator("button[type=submit]").first.click()
                page.wait_for_load_state("networkidle")

            def overflow(page):
                return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")

            # ---- login privacy notice (390px)
            ctx, page = ctx_for(390)
            page.goto(base + "/login", wait_until="networkidle")
            assert page.locator(".privacy-note").is_visible() and overflow(page) <= 1
            page.locator(".privacy-note").scroll_into_view_if_needed()
            page.screenshot(path=str(out / "login_privacy_390.png"), full_page=True)
            page.goto(base + "/admin/login", wait_until="networkidle")
            assert page.locator(".privacy-note").is_visible()
            ctx.close()

            # ---- camera scan, pledge gate, location (far first, then near)
            far = {"latitude": LAT + 200 / M_PER_DEG, "longitude": LNG, "accuracy": 5}
            near = {"latitude": LAT + 10 / M_PER_DEG, "longitude": LNG, "accuracy": 8}
            ctx, page = ctx_for(390, geo=far)
            login(page, "DEMO001")
            page.goto(base + "/bookings", wait_until="networkidle")
            btn = page.locator(".ck-open-btn")
            assert btn.count() == 1 and btn.is_enabled()
            page.locator(".checkin-pin").scroll_into_view_if_needed()
            page.screenshot(path=str(out / "checkin_button_390.png"), full_page=False)
            btn.click()
            assert page.locator("#ck-modal").is_visible() and page.locator("#ck-start").is_disabled()
            assert page.locator(".ck-pledge-list li").count() == 8
            page.screenshot(path=str(out / "checkin_pledge_390.png"))
            page.check("#ck-pledge")
            assert page.locator("#ck-start").is_enabled()
            page.click("#ck-start")
            page.wait_for_selector("#ck-result.ck-error", timeout=20000)
            msg = page.locator("#ck-result").inner_text()
            assert "超出签到范围" in msg, msg
            with app.app_context():
                assert Booking.query.filter(Booking.checked_in_at.isnot(None)).count() == 0
            page.screenshot(path=str(out / "checkin_geo_390.png"))
            ctx.set_geolocation(near)
            page.click("#ck-retry")
            page.wait_for_selector("#ck-result.ck-success", timeout=20000)
            print("scan success:", page.locator("#ck-result").inner_text())
            with app.app_context():
                done = Booking.query.filter(Booking.checked_in_at.isnot(None)).one()
                assert done.pledge_accepted_at and 5 < done.checkin_distance_m < 15, done.checkin_distance_m
            page.click("#ck-done")
            page.wait_for_load_state("networkidle")
            assert page.locator(".ck-open-btn").count() == 0
            ctx.close()

            # ---- camera denied -> photo fallback (DEMO002), pledge still required first
            ctx, page = ctx_for(390, geo=near, camera=False)
            page.add_init_script("navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied','NotAllowedError'))")
            login(page, "DEMO002")
            page.goto(base + "/captain", wait_until="networkidle")
            page.click(".ck-open-btn")
            assert page.locator("#ck-step-scan").is_hidden() and page.locator("#ck-file").count() == 1
            page.check("#ck-pledge")
            page.click("#ck-start")
            page.wait_for_selector("#ck-fallback", state="visible", timeout=10000)
            assert "相机" in page.locator("#ck-status").inner_text()
            page.set_input_files("#ck-file", str(qr_png))
            try:
                page.wait_for_selector("#ck-result.ck-success", timeout=15000)
            except Exception:
                print("FALLBACK STATE:", page.locator("#ck-status").inner_text(), "|", page.locator("#ck-result").inner_text())
                raise
            print("fallback success:", page.locator("#ck-result").inner_text())
            ctx.close()

            # ---- auto refresh: clean page reloads (scroll kept), dirty form / open dialog do not
            app.config["AUTO_REFRESH_SECONDS"] = 3
            ctx, page = ctx_for(1280)
            login(page, "DEMOSUPER", staff=True)
            page.goto(base + "/security/", wait_until="networkidle")
            assert page.evaluate("window.__autoRefresh && window.__autoRefresh.seconds") == 3
            page.evaluate("window.__marker = 1; window.scrollTo(0, 40)")
            wait_marker_gone(page, 10)
            page.wait_for_load_state("networkidle")
            print("clean page reloaded; scrollY after reload:", page.evaluate("window.scrollY"))
            page.goto(base + "/labs/new", wait_until="networkidle")
            page.fill("#name", "草稿实验室")
            page.evaluate("window.__marker = 1; document.activeElement.blur()")
            time.sleep(7)
            assert page.evaluate("window.__marker") == 1, "dirty form was reloaded"
            assert page.evaluate("window.__autoRefresh.blockedReason()") == "dirty"
            page.fill("#name", "")
            page.evaluate("document.getElementById('name').value = ''; document.activeElement.blur()")
            wait_marker_gone(page, 10)
            ctx.close()
            app.config["AUTO_REFRESH_SECONDS"] = 0

            # ---- lab settings + wipe page screenshots (superadmin, 1280)
            ctx, page = ctx_for(1280)
            login(page, "DEMOSUPER", staff=True)
            page.goto(base + "/labs/", wait_until="networkidle")
            page.locator("a", has_text="编辑").last.click()
            page.wait_for_load_state("networkidle")
            assert page.locator("#geo-use-here").is_visible()
            page.screenshot(path=str(out / "lab_geo_settings_1280.png"), full_page=True)
            page.goto(base + "/security/wipe", wait_until="networkidle")
            page.fill("#wipe-phrase", "确定消除所有个人信息")
            page.screenshot(path=str(out / "wipe_confirm_1280.png"), full_page=True)
            ctx.close()
            browser.close()
        for shot in out.glob("*.png"):
            shutil.copy(shot, docs / shot.name)
            print("written", docs / shot.name)
        server.shutdown()


if __name__ == "__main__":
    main()
