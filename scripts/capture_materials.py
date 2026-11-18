#!/usr/bin/env python3
"""Capture material/profile screens and exercise the UI using isolated demo data.

No production database or credentials are opened. The loopback server is always stopped.
"""
import json
import argparse
from fractions import Fraction
from pathlib import Path
import secrets
import sys
import tempfile
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.capture_ui import demo_app, QuietHandler
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import db
from app.models import MaterialClaim, MaterialClaimItem, MaterialItem, MaterialPurchase, User
from app.utils import MINIMAL_PNG_BYTES, local_now
from app.quantities import parse_quantity, amount_cents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buy-only", action="store_true", help="Only replace the purchase screenshot")
    args = parser.parse_args()
    captures = []
    with tempfile.TemporaryDirectory(prefix="materials-ui-") as directory:
        app, credentials, ids, _ = demo_app(Path(directory))
        with app.app_context():
            free = MaterialItem(kind="free", name="竹皮", spec="1250×430×0.35mm", unit="张", stock=98, per_team_qty=2)
            sale = MaterialItem(kind="sale", name="竹皮", spec="按需分张", unit="张", stock=Fraction(7, 2), unit_price_cents=235)
            glue = MaterialItem(kind="sale", name="加厚竹皮", spec="按需分张", unit="张", stock=Fraction(2, 3), unit_price_cents=800)
            empty = MaterialItem(kind="sale", name="连接片", spec="标准型", unit="包", stock=0, unit_price_cents=500)
            fresh = User(student_id="DEMONEXT", name="待完善信息", role="member", team_name="星桥结构队")
            password = secrets.token_urlsafe(18) + "Aa!"
            fresh.set_password(password, require_change=False)
            credentials["fresh"] = (fresh.student_id, password)
            db.session.add_all([free, sale, glue, empty, fresh]); db.session.flush()
            row = MaterialClaim(user_id=ids["captain"], team_name="逐光结构队", photo="demo.png", ip="192.0.2.10")
            row.items.append(MaterialClaimItem(item_id=free.id, name=free.name, spec=free.spec, unit=free.unit, quantity=2))
            db.session.add(row); db.session.commit()
            sale_id, glue_id, empty_id = sale.id, glue.id, empty.id
        code = f"{secrets.randbelow(1_000_000):06d}"
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        errors = []
        try:
            with patch("app.mailer.send_via_smtp", side_effect=AssertionError("UI tests must not send SMTP")), \
                 patch("app.verification.generate_code", return_value=code), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                def new_page(width):
                    context = browser.new_context(viewport={"width": width, "height": 844 if width == 390 else 1000},
                                                  is_mobile=width == 390, has_touch=width == 390, locale="zh-CN")
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    return page
                def goto(page, path):
                    response = page.goto(base + path, wait_until="networkidle")
                    assert response.status == 200, (path, response.status)
                def signin(page, role):
                    sid, password = credentials[role]
                    goto(page, "/login" if role in ("member", "fresh") else "/admin/login")
                    if role in ("member", "fresh"): page.fill("#student_id", sid)
                    page.fill("#password", password)
                    page.locator("button[type=submit]").click()
                    page.wait_for_load_state("networkidle")
                def shot(page, filename):
                    if args.buy_only and filename != "materials_buy_390.png":
                        return
                    page.evaluate("window.scrollTo(0, 0)")
                    page.evaluate("document.fonts.ready")
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), filename
                    page.screenshot(path=str(ROOT / "docs" / filename), full_page=True)
                    captures.append("docs/" + filename)

                mobile = new_page(390); signin(mobile, "member"); goto(mobile, "/materials/buy")
                # Compare both parsers and cent rounding through the actual browser.
                for raw in ("0.5", ".5", "1.25", "1/3", "2/3", "1 1/2", "1 / 3", "0.000000001"):
                    mobile.fill(f"#qty_{sale_id}", raw)
                    assert mobile.locator(f"#qty_{sale_id}").evaluate("el => el.validity.valid"), raw
                    cents = amount_cents(235, parse_quantity(raw))
                    assert mobile.locator("#mat-total").inner_text() == f"{cents // 100}.{cents % 100:02d}", raw
                for raw in ("-1", "1/0", "0/3", "NaN", "1..2", "1 3/2", "3 2/3", "0.0000000001", "1/1000000001", "1e-3", "１２"):
                    mobile.fill(f"#qty_{sale_id}", raw)
                    assert mobile.locator(f"#qty_{sale_id}").evaluate("el => el.validity.customError"), raw
                    assert mobile.locator("#mat-total").inner_text() == "—"
                mobile.fill(f"#qty_{sale_id}", "1/3"); mobile.fill(f"#qty_{glue_id}", "0.5")
                assert mobile.locator("#mat-total").inner_text() == "4.78"
                assert mobile.locator(f"#qty_{empty_id}").is_disabled()
                mobile.locator(f"#qty_{sale_id}").locator("..").locator('[data-step="1"]').click()
                assert mobile.locator("#mat-total").inner_text() == "7.13"
                assert mobile.locator(f"#qty_{sale_id}").input_value() == "4/3"
                mobile.fill(f"#qty_{sale_id}", "1/3")
                mobile.set_input_files("#photo", {"name": "materials.png", "mimeType": "image/png", "buffer": MINIMAL_PNG_BYTES})
                mobile.check('[name="confirm"]')
                shot(mobile, "materials_buy_390.png")
                mobile.locator("#buy-submit").click(); mobile.wait_for_url("**/verify")
                mobile.fill("#code", code)
                mobile.locator('#code').locator('xpath=ancestor::form').locator('button[type="submit"]').click()
                mobile.wait_for_url("**/materials/buy")
                with app.app_context():
                    row = MaterialPurchase.query.filter_by(status="confirmed").one()
                    assert row.total_cents == 478
                    assert db.session.get(MaterialItem, sale_id).stock == Fraction(19, 6)
                    assert db.session.get(MaterialItem, glue_id).stock == Fraction(1, 6)

                admin = new_page(1280); signin(admin, "admin"); goto(admin, "/materials/admin")
                shot(admin, "materials_admin_1280.png")
                assert admin.locator('a[href*="/edit"]').count() == 0
                superadmin = new_page(1280); signin(superadmin, "superadmin"); goto(superadmin, "/materials/settings")
                for raw in ("1 1/2", "2/3", "0.5", "0"):
                    superadmin.fill("#sale_stock", raw)
                    assert superadmin.locator("#sale_stock").evaluate("el => el.validity.valid"), raw
                for raw in ("1/0", "-0.5", "NaN"):
                    superadmin.fill("#sale_stock", raw)
                    assert superadmin.locator("#sale_stock").evaluate("el => el.validity.customError"), raw
                superadmin.fill("#sale_stock", "0")
                shot(superadmin, "materials_settings_1280.png")
                profile = new_page(390); signin(profile, "fresh")
                assert profile.url.endswith("/profile")
                shot(profile, "profile_setup_390.png")
                profile.fill("#name", "林同学"); profile.fill("#phone", "13900139000"); profile.fill("#email", "demo-next@example.com")
                profile.locator('#email').locator('xpath=ancestor::form').locator('button[type="submit"]').click(); profile.wait_for_url("**/profile/verify")
                profile.fill("#code", code)
                profile.locator('#code').locator('xpath=ancestor::form').locator('button[type="submit"]').click()
                profile.wait_for_load_state("networkidle")
                goto(profile, "/captain")
                for lang in ("en", "zh-Hant"):
                    goto(mobile, "/materials/buy?lang=" + lang)
                    assert not mobile.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context(): db.session.remove(); db.engine.dispose()
    print(json.dumps({"screenshots": captures, "checks": ["fraction parser and BigInt rounding match backend", "mixed-number and fraction quantity buttons", "admin fractional stock validation", "sold-out disabled", "photo and email purchase confirmation", "profile completion", "admin read-only", "no overflow in three languages", "no browser errors", "server stopped"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
