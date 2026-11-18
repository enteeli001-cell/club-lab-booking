#!/usr/bin/env python3
"""Three acceptance screenshots on a disposable database. Never opens production credentials."""
import shutil
import sys
import tempfile
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from capture_ui import ROOT, QuietHandler, demo_app
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import db
from app.models import ToolLoan

SHOTS = (
    ('member','en',390,'/captain','i18n_captain_home_en_390.png'),
    ('admin','zh-Hant',1280,'/','i18n_admin_home_zht_1280.png'),
    ('superadmin','zh-Hans',390,None,'return_superadmin_nophoto_390.png'),
)


def main():
    step='create isolated demo application'
    with tempfile.TemporaryDirectory(prefix='.preview-i18n-',dir=ROOT) as directory:
        temp=Path(directory)
        app,credentials,ids,_day=demo_app(temp)
        server=None;thread=None
        try:
            step='start temporary loopback service'
            server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            with patch('app.mailer.send_via_smtp',side_effect=AssertionError('Real email is forbidden')),sync_playwright() as playwright:
                step='launch Chromium'
                browser=playwright.chromium.launch(headless=True)
                for role,lang,width,path,filename in SHOTS:
                    step=f'{role} / {width}px'
                    context=browser.new_context(viewport={'width':width,'height':844 if width==390 else 900},
                                                is_mobile=width==390,has_touch=width==390,locale='zh-CN')
                    page=context.new_page();errors=[]
                    page.on('pageerror',lambda error:errors.append(type(error).__name__))
                    login='/login' if role=='member' else '/admin/login'
                    page.goto(base+login+'?lang='+lang,wait_until='networkidle')
                    sid,password=credentials[role]
                    if role=='member':page.fill('#student_id',sid)
                    page.fill('#password',password)
                    page.locator('button[type=submit]').click()
                    page.wait_for_url(base+('/captain' if role=='member' else '/'))
                    if path is None:path=f'/tools/{ids["loan"]}/return'
                    page.goto(base+path,wait_until='networkidle')
                    assert page.locator('html').get_attribute('lang')=={'en':'en','zh-Hant':'zh-Hant','zh-Hans':'zh-CN'}[lang]
                    assert page.locator('.header-language').is_visible()
                    page.evaluate('document.fonts.ready')
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                    if role=='member':
                        assert 'welcome to the test booking platform' in page.locator('h1').inner_text()
                        page.locator('#nav-toggle').click()
                        assert page.locator('.nav-toggle-label').inner_text()=='Close menu'
                        page.locator('#nav-toggle').click()
                    elif role=='admin':assert '管理員' in page.locator('.userchip-role').inner_text()
                    else:
                        assert page.locator('input[name=return_photo]').count()==0
                        assert '总管理员确认归还无需上传照片。' in page.locator('main').inner_text()
                    assert not errors,errors
                    page.screenshot(path=str(temp/filename),full_page=True)
                    if role=='superadmin':
                        page.locator('form button[type=submit]').last.click()
                        page.wait_for_url('**/tools/admin')
                        with app.app_context():
                            loan=db.session.get(ToolLoan,ids['loan'])
                            assert loan.status=='returned' and loan.return_photo is None
                    context.close()
                    print(f'{filename}: browser checks passed',flush=True)
                browser.close()
            for _role,_lang,_width,_path,filename in SHOTS:
                shutil.copyfile(temp/filename,ROOT/'docs'/filename)
            print('All three screenshots saved to docs/. Production data and tunnel untouched.')
        except Exception as error:
            # No exception details or locals: browser errors can contain typed credentials.
            print(f'Stopped at {step}: {type(error).__name__}',file=sys.stderr)
            return 1
        finally:
            if server:
                server.shutdown()
                if thread:thread.join(timeout=5)
                server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()
    return 0


if __name__=='__main__':raise SystemExit(main())
