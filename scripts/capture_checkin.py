#!/usr/bin/env python3
"""Screenshots for check-in / no-show reminder / footer, using a throw-away database.

Never touches production data or the tunnel; demo passwords are random and in memory only.
Outputs docs/checkin_*.png, docs/noshow_alert.png and docs/reminder_email_sample.txt.
"""
import email
import os
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, time, timedelta
from email import policy
from pathlib import Path
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.playwright-browsers'))
os.environ['TMPDIR'] = str(ROOT / '.runtime-tests')
(ROOT / '.runtime-tests').mkdir(exist_ok=True)
tempfile.tempdir = None

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from app import create_app, db
from app.jobs import send_start_reminders
from app.models import AppSetting, Booking, Lab, User
from app.noshow import scan_noshow_alerts
from app.utils import local_now


class Quiet(WSGIRequestHandler):
    def log(self, *a, **k):
        pass


def build(directory):
    app = create_app({'TESTING': True, 'DATA_DIR': str(directory / 'data'), 'LOG_DIR': str(directory / 'logs'),
                      'SECRET_KEY': secrets.token_hex(32), 'API_TOKEN': secrets.token_hex(32),
                      'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(directory / 'demo.db')})
    creds = {}
    with app.app_context():
        users = {}
        for sid, name, role, team in [('DEMO001', '陈同学', 'member', '逐光结构队'), ('DEMO002', '林同学', 'member', '飞梁结构队'),
                                      ('DEMOADMIN', '李老师', 'admin', None), ('DEMOSUPER', '总管理员', 'superadmin', None)]:
            password = secrets.token_urlsafe(18) + 'Aa!'
            creds[sid] = password
            user = User(student_id=sid, name=name, role=role, team_name=team, last_email='captain@example.com',
                        last_phone='13800138000')
            user.set_password(password, require_change=False)
            db.session.add(user)
            users[sid] = user
        labs = [Lab(name='结构测试实验室', capacity=12, open_start=time(0, 0), open_end=time(23, 59), enabled=True),
                Lab(name='材料力学实验室', capacity=8, open_start=time(0, 0), open_end=time(23, 59), enabled=True)]
        db.session.add_all(labs)
        db.session.flush()
        db.session.get(AppSetting, 'checkin_enabled_at').value = '2000-01-01T00:00:00'
        now = local_now().replace(second=0, microsecond=0)

        def book(user, lab, start, minutes, status='approved', purpose='模型承载测试'):
            end = start + timedelta(minutes=minutes)
            b = Booking(lab=lab, user=user, date=start.date(), start_time=start.time(), end_time=end.time(), purpose=purpose,
                        people_count=2, captain_phone='13800138000', captain_email='captain@example.com', status=status)
            db.session.add(b)
            return b

        book(users['DEMO001'], labs[0], now - timedelta(minutes=10), 60)             # open now
        book(users['DEMO001'], labs[1], now + timedelta(days=1, hours=1), 60, purpose='节点连接测试')  # early
        book(users['DEMO002'], labs[0], now + timedelta(minutes=20), 60, purpose='振动台测试')       # reminder mail
        for i in range(3):                                                             # three no-shows
            book(users['DEMO002'], labs[0], now - timedelta(days=i + 1, hours=-1), 60, purpose='桁架测试')
        db.session.commit()
        tokens = {lab.name: lab.checkin_token for lab in labs}
        ids = {'lab0': labs[0].id}
    return app, creds, tokens, ids


def main():
    docs = ROOT / 'docs'
    with tempfile.TemporaryDirectory(prefix='.preview-', dir=ROOT) as temp:
        temp = Path(temp)
        app, creds, tokens, ids = build(temp)
        with app.app_context():
            now = local_now()
            print('reminders:', send_start_reminders(now), 'noshow:', scan_noshow_alerts(now))
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        out = temp / 'shots'
        out.mkdir()
        try:
            with patch('app.mailer.send_via_smtp', side_effect=AssertionError('no real mail')), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)

                def new(width, height, mobile):
                    ctx = browser.new_context(viewport={'width': width, 'height': height}, is_mobile=mobile, has_touch=mobile,
                                              locale='zh-CN', timezone_id='Asia/Shanghai')
                    return ctx, ctx.new_page()

                def captain_login(page, sid, nxt=None):
                    page.goto(base + '/login' + (f'?next={nxt}' if nxt else ''), wait_until='networkidle')
                    page.fill('#student_id', sid)
                    page.fill('#password', creds[sid])
                    page.locator('button[type=submit]').click()
                    page.wait_for_load_state('networkidle')

                # Desktop: admin QR page + print view
                ctx, page = new(1280, 900, False)
                page.goto(base + '/admin/login', wait_until='networkidle')
                page.fill('#password', creds['DEMOSUPER'])
                page.locator('button[type=submit]').click()
                page.wait_for_load_state('networkidle')
                page.goto(base + '/checkin-codes/', wait_until='networkidle')
                assert page.locator('.qr-img').first.evaluate('(i)=>i.naturalWidth>0')
                page.screenshot(path=str(out / 'checkin_qr_admin.png'), full_page=True)
                ctx.close()
                ctx, page = new(794, 1123, False)
                page.goto(base + '/admin/login', wait_until='networkidle')
                page.fill('#password', creds['DEMOSUPER'])
                page.locator('button[type=submit]').click()
                page.wait_for_load_state('networkidle')
                page.goto(base + f'/checkin-codes/{ids["lab0"]}/print', wait_until='networkidle')
                assert page.locator('.site-footer').count() == 0
                page.screenshot(path=str(out / 'checkin_qr_print.png'), full_page=True)
                ctx.close()

                # Mobile: captain card, scan-and-login flow, success
                ctx, page = new(390, 844, True)
                captain_login(page, 'DEMO001')
                page.goto(base + '/captain', wait_until='networkidle')
                assert page.locator('.checkin-pin').count() == 1
                page.screenshot(path=str(out / 'checkin_captain_card.png'), full_page=True)
                page.locator('#nav-toggle').click()
                page.locator('#site-nav form').evaluate('(f)=>f.requestSubmit()')
                page.wait_for_url('**/login')
                scan = '/checkin/' + tokens['结构测试实验室']
                page.goto(base + scan, wait_until='networkidle')
                assert '/login' in page.url and 'next=' in page.url
                page.fill('#student_id', 'DEMO001')
                page.fill('#password', creds['DEMO001'])
                page.locator('button[type=submit]').click()
                page.wait_for_load_state('networkidle')
                assert page.url.endswith(scan), page.url
                page.locator('.checkin-submit').click()
                page.wait_for_load_state('networkidle')
                assert '签到成功' in page.content()
                page.screenshot(path=str(out / 'checkin_success.png'), full_page=True)
                ctx.close()

                # Mobile: forced no-show overlay for DEMO002
                ctx, page = new(390, 844, True)
                captain_login(page, 'DEMO002')
                page.goto(base + '/captain', wait_until='networkidle')
                assert page.locator('#noshow-overlay').is_visible()
                page.screenshot(path=str(out / 'noshow_alert.png'))
                page.locator('#noshow-overlay button').click()
                page.wait_for_load_state('networkidle')
                assert page.locator('#noshow-overlay').count() == 0
                page.screenshot(path=str(out / 'noshow_footer_after_ack.png'), full_page=True)
                ctx.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
        # Reminder email sample from the temporary outbox.
        emls = sorted((temp / 'data' / 'outbox').glob('*.eml'))
        sample = None
        for path in emls:
            msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
            if '即将开始' in str(msg['Subject']):
                sample = msg
                break
        assert sample is not None, 'no reminder mail in the temporary outbox'
        text = f"主题：{sample['Subject']}\n收件人：{sample['To']}\n\n{sample.get_content()}"
        for name in ('checkin_qr_admin', 'checkin_qr_print', 'checkin_captain_card', 'checkin_success', 'noshow_alert', 'noshow_footer_after_ack'):
            shutil.copyfile(out / f'{name}.png', docs / f'{name}.png')
        (docs / 'reminder_email_sample.txt').write_text(text, encoding='utf-8')
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    print('done')


if __name__ == '__main__':
    main()
