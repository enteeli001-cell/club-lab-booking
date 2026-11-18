#!/usr/bin/env python3
"""Screenshots of 功能开关 (1280px) and the public maintenance page (390px) on a throw-away database.

Never touches data/app.db or the tunnel. Writes docs/feature_toggles_1280.png and docs/maintenance_page_390.png.
"""
import os
import sys
import tempfile
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))
os.environ["AUTO_REFRESH_SECONDS"] = "0"
(ROOT / ".runtime-tests").mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(ROOT / ".runtime-tests")
tempfile.tempdir = None

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import toggles  # noqa: E402
from capture_claude import Quiet, build  # noqa: E402


def main():
    docs = ROOT / "docs"
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=ROOT) as temp:
        app, creds, _token = build(Path(temp))
        app.config["WTF_CSRF_ENABLED"] = True
        with app.app_context():
            toggles.set_feature("schedule", False)
            toggles.set_feature("tool_borrow", False)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            desk = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
            page = desk.new_page()
            page.goto(base + "/admin/login")
            page.fill("input[name=password]", creds["DEMOSUPER"])
            page.click("form button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.goto(base + "/switches/")
            assert page.locator(".switch-row").count() == len(toggles.FEATURES)
            assert page.locator(".switch-row.is-off").count() == 2
            page.screenshot(path=str(docs / "feature_toggles_1280.png"), full_page=True)

            # switch maintenance mode on through the real form (checkbox + confirm dialog)
            page.goto(base + "/switches/maintenance")
            page.on("dialog", lambda d: d.accept())
            page.check("input[name=confirm]")
            page.click("button.danger")
            page.wait_for_load_state("networkidle")
            assert page.locator(".maint-banner").count() == 1

            phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                        device_scale_factor=2, locale="zh-CN")
            mp = phone.new_page()
            resp = mp.goto(base + "/login")
            assert resp.status == 503 and resp.headers.get("retry-after") == "600", (resp.status, resp.headers)
            assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            mp.screenshot(path=str(docs / "maintenance_page_390.png"))
            for path in ("/switches/", "/switches/maintenance"):  # superadmin pages fit a phone too
                sp = phone.new_page()
                sp.goto(base + "/admin/login")
                if sp.locator("input[name=password]").count():
                    sp.fill("input[name=password]", creds["DEMOSUPER"])
                    sp.click("form button[type=submit]")
                    sp.wait_for_load_state("networkidle")
                sp.goto(base + path)
                assert sp.locator(".maint-banner").count() == 1
                assert sp.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
                sp.screenshot(path=str(Path(temp) / ("m" + path.strip("/").replace("/", "_") + ".png")), full_page=True)
                import shutil
                shutil.copy(str(Path(temp) / ("m" + path.strip("/").replace("/", "_") + ".png")), f"/tmp/toggle_check_{path.strip('/').replace('/', '_')}.png")
            browser.close()
        server.shutdown()
    print("ok: docs/feature_toggles_1280.png docs/maintenance_page_390.png")


if __name__ == "__main__":
    main()
