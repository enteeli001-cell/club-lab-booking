#!/usr/bin/env python3
"""Verify and screenshot captain/admin/superadmin navigation at 1280px and 390px.

Uses only the isolated demo application. No production credentials or tunnel operations.
"""
import json
import os
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
import shutil
import tempfile
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from capture_ui import ROOT, QuietHandler, demo_app
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import db

ROLES = [('member', 'captain'), ('admin', 'admin'), ('superadmin', 'superadmin')]
VIEWPORTS = [{'width':1280, 'height':900}, {'width':390, 'height':844}]


def layout_metrics(page):
    return page.evaluate('''() => {
      const nav = document.getElementById('site-nav');
      const header = document.querySelector('.topbar');
      const main = document.querySelector('main');
      const style = getComputedStyle(nav);
      const items = [...nav.querySelectorAll('a, button')].map(el => {
        const box = el.getBoundingClientRect();
        return {text:el.textContent.trim(), left:box.left, right:box.right,
                top:box.top, bottom:box.bottom, width:box.width, height:box.height};
      });
      const box = header.getBoundingClientRect();
      return {direction:style.flexDirection, wrap:style.flexWrap, display:style.display,
              header:{left:box.left, width:box.width, bottom:box.bottom, height:box.height},
              main:{left:main.getBoundingClientRect().left, top:main.getBoundingClientRect().top},
              overflow:document.documentElement.scrollWidth > innerWidth + 1, items};
    }''')


def check_desktop(page):
    metrics=layout_metrics(page)
    assert not metrics['overflow'], 'Desktop document overflows horizontally'
    assert metrics['display']=='flex' and metrics['direction']=='row' and metrics['wrap']=='wrap', metrics
    assert not page.locator('#nav-toggle').is_visible()
    assert metrics['header']['left']==0 and abs(metrics['header']['width']-1280)<1
    assert metrics['header']['height']<=200, 'Navigation occupies too much of the first screen'
    assert metrics['main']['top']>=metrics['header']['bottom']-1 and metrics['main']['left']==0
    rows={round(item['top']) for item in metrics['items']}
    assert 1<=len(rows)<=3, f'Unexpected navigation rows: {len(rows)}'
    for item in metrics['items']:
        assert item['width']>0 and item['left']>=0 and item['right']<=1280
        assert item['bottom']<=metrics['header']['bottom']+1
    return metrics


def check_mobile_closed(page):
    metrics=layout_metrics(page)
    assert not metrics['overflow'], 'Mobile document overflows horizontally'
    assert not page.locator('#site-nav').is_visible()
    assert page.locator('#nav-toggle').is_visible()
    assert page.locator('#nav-toggle').get_attribute('aria-expanded')=='false'
    assert metrics['header']['height']<=120
    return metrics


def main():
    manifest={'viewports':VIEWPORTS,'checks':[],'screenshots':[]}
    with tempfile.TemporaryDirectory(prefix='.preview-nav-',dir=ROOT) as directory:
        temp=Path(directory)
        app,credentials,ids,day=demo_app(temp)
        server=None
        try:
            server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            with patch('app.mailer.send_via_smtp',side_effect=AssertionError('Email disabled during capture')),sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                for viewport in VIEWPORTS:
                    width=viewport['width']
                    for role,name in ROLES:
                        context=browser.new_context(viewport=viewport,locale='zh-CN',
                                  is_mobile=width==390,has_touch=width==390,timezone_id='Asia/Shanghai')
                        page=context.new_page();errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        response=page.goto(base+('/login' if role=='member' else '/admin/login'),wait_until='networkidle')
                        assert response.status==200
                        sid,password=credentials[role]
                        if role=='member':page.fill('#student_id',sid)
                        page.fill('#password',password)
                        page.locator('button[type=submit]').click()
                        page.wait_for_url(base+('/captain' if role=='member' else '/'))
                        page.wait_for_load_state('networkidle');page.evaluate('document.fonts.ready')
                        metrics=check_desktop(page) if width==1280 else check_mobile_closed(page)
                        filename=f'{name}-{width}.png'
                        page.screenshot(path=str(temp/filename),full_page=True)
                        manifest['screenshots'].append('docs/navigation/'+filename)
                        if width==1280:
                            # The default remains horizontal even without the authenticated body class.
                            page.locator('body').evaluate('(el)=>el.classList.remove("app-shell")')
                            check_desktop(page)
                            page.locator('body').evaluate('(el)=>el.classList.add("app-shell")')
                        else:
                            page.locator('#nav-toggle').click()
                            assert page.locator('#site-nav').is_visible()
                            assert page.locator('#nav-toggle').get_attribute('aria-expanded')=='true'
                            opened=layout_metrics(page)
                            assert opened['direction']=='column' and not opened['overflow']
                            assert all(item['height']>=44 for item in opened['items'])
                            page.locator('#site-nav button[type=submit]').scroll_into_view_if_needed()
                            page.locator('#site-nav').evaluate('(el)=>el.scrollTop=0')
                            filename=f'{name}-390-menu.png'
                            page.screenshot(path=str(temp/filename),full_page=True)
                            manifest['screenshots'].append('docs/navigation/'+filename)
                            page.locator('#site-nav a').first.focus();page.keyboard.press('Escape')
                            check_mobile_closed(page)
                            assert page.locator('#nav-toggle').evaluate('(el)=>el===document.activeElement')
                            page.locator('#nav-toggle').click()
                            page.set_viewport_size(VIEWPORTS[0]);check_desktop(page)
                            page.set_viewport_size(viewport);check_mobile_closed(page)
                        assert not errors,errors
                        manifest['checks'].append({'role':role,'width':width,'layout':metrics})
                        context.close()
                        print(f'{name} / {width}px: navigation checks passed',flush=True)
                browser.close()
            # A failed check never replaces previous screenshots.
            output=ROOT/'docs'/'navigation';output.mkdir(parents=True,exist_ok=True)
            for relative in manifest['screenshots']:
                shutil.copyfile(temp/Path(relative).name,ROOT/relative)
            (output/'checks.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        finally:
            if server:
                server.shutdown();thread.join(timeout=5);server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()
    print('Captured six role/viewport screenshots plus three expanded mobile menus.')
    return 0


if __name__=='__main__':raise SystemExit(main())
