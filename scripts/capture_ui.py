#!/usr/bin/env python3
"""Capture desktop/mobile UI with an isolated database; never read production credentials.

No production app connection is made. All demo passwords are random and in memory.
The temporary service listens only on loopback and is shut down automatically.
"""
import argparse
import json
import logging
import os
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
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
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.playwright-browsers'))
# Chromium's profile, crash data, and temp files stay inside this project too.
os.environ['TMPDIR'] = str(ROOT / '.runtime-tests')
(ROOT / '.runtime-tests').mkdir(exist_ok=True)
tempfile.tempdir = None

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server
from app import create_app, db
from app.models import (AuditLog, Booking, BookingCompanion, DutyShift, Lab, LoginLog,
                        SecurityEvent, TeamMember, ToolCatalog, ToolLoan, ToolLoanItem, User)
from app.utils import local_now, write_minimal_png


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def demo_app(directory):
    app = create_app({'TESTING':True, 'DATA_DIR':str(directory/'data'),
                      'LOG_DIR':str(directory/'logs'), 'SECRET_KEY':secrets.token_hex(32),
                      'API_TOKEN':secrets.token_hex(32),
                      'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(directory/'demo.db'),
                      'CAPTAIN_LOGIN_FORGOT_AFTER':3})
    credentials = {}
    with app.app_context():
        users = {}
        for sid,name,role in [('DEMO001','陈同学','member'),('DEMOADMIN','李老师','admin'),('DEMOSUPER','总管理员','superadmin')]:
            password=secrets.token_urlsafe(18)+'Aa!'
            credentials[role]=(sid,password)
            user=User(student_id=sid,name=name,role=role,team_name='逐光结构队' if role=='member' else None,
                      contact_phone='13800138000',last_phone='13800138000',last_email='captain@example.com')
            user.set_password(password,require_change=False);db.session.add(user);users[role]=user
        db.session.flush()
        captain=users['member'];admin=users['admin']
        captain.verified_email = captain.last_email
        captain.email_verified_at = captain.profile_completed_at = local_now()
        teammate=TeamMember(captain=captain,name='林同学',student_id='DEMO002',phone='13900139000')
        lab=Lab(name='结构测试实验室',capacity=12,open_start=time(8),open_end=time(22),enabled=True,
                contact_name='实验室值班老师',contact_phone='13800138000')
        db.session.add_all([teammate,lab]);db.session.flush()
        now=local_now().replace(second=0,microsecond=0)
        day=now.date()+timedelta(days=7)
        bookings=[]
        for start,status,purpose in [(time(9,15),'pending','桁架承载测试'),(time(13),'approved','模型振动测试'),(time(15),'rejected','节点连接测试')]:
            end=(__import__('datetime').datetime.combine(day,start)+timedelta(minutes=45)).time()
            b=Booking(lab=lab,user=captain,date=day,start_time=start,end_time=end,purpose=purpose,
                      people_count=2,captain_phone='13800138000',captain_email='captain@example.com',status=status,
                      reject_reason='设备维护，请选择其他时段' if status=='rejected' else None)
            db.session.add(b);db.session.flush()
            db.session.add(BookingCompanion(booking=b,name=teammate.name,student_id=teammate.student_id,source='member',team_member_id=teammate.id))
            bookings.append(b)
        locked_start=now+timedelta(minutes=20)
        locked_end=locked_start+timedelta(minutes=30)
        if locked_end.date()!=locked_start.date():
            locked_start=now-timedelta(minutes=10);locked_end=now+timedelta(minutes=10)
        locked=Booking(lab=lab,user=captain,date=locked_start.date(),start_time=locked_start.time(),end_time=locked_end.time(),
                       purpose='临近开始的测试',people_count=1,captain_phone='13800138000',captain_email='captain@example.com',status='approved')
        db.session.add(locked)
        db.session.add(DutyShift(lab=lab,admin=admin,specific_date=locked.date,start_time=time(0),end_time=time(23,59)))
        tools=[]
        for name,qty in [('游标卡尺',8),('扳手',12),('电钻',4),('夹具',20)]:
            item=ToolCatalog(name=name,total_quantity=qty,is_major=True);db.session.add(item);tools.append(item)
        photo=write_minimal_png(directory/'data'/'uploads'/'demo.png').name
        loan=ToolLoan(user=captain,lab=lab,team_name=captain.team_name,contact='13800138000',borrow_at=now,
                      expected_return_at=now+timedelta(hours=3),borrow_photo=photo,status='borrowing')
        db.session.add(loan);db.session.flush()
        db.session.add(ToolLoanItem(loan=loan,catalog_id=tools[0].id,name=tools[0].name,quantity=2,is_other=False))
        db.session.add(LoginLog(user_id=captain.id,student_id=captain.student_id,role='member',ip='192.0.2.10',
                               browser='Chrome',os_name='Android',success=True,device_id='演示设备'))
        db.session.add(AuditLog(actor_id=admin.id,actor_role='admin',actor_student_id=admin.student_id,action='booking.approve',
                               summary='通过模型振动测试预约。',target_type='booking',target_id=bookings[1].id,reversible=True))
        db.session.add(SecurityEvent(event_type='path_scan',severity='high',ip='192.0.2.20',method='GET',path='/.env',
                                     detail='被动记录：访问敏感路径（演示数据）',count=3))
        db.session.commit()
        ids={'lab':lab.id,'booking':bookings[0].id,'loan':loan.id,'captain':captain.id}
    return app,credentials,ids,day


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',nargs='*',help='Optional screenshot basenames')
    args=parser.parse_args()
    manifest={'viewports':{'desktop':{'width':1280,'height':1000},'mobile':{'width':390,'height':844}},'screenshots':[], 'checks':[]}
    with tempfile.TemporaryDirectory(prefix='.preview-',dir=ROOT) as temp:
        app,credentials,ids,day=demo_app(Path(temp))
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        # Any accidental SMTP call is a hard failure; reserved recipients also stay local.
        try:
            with patch('app.mailer.send_via_smtp',side_effect=AssertionError('Screenshot must not send email')),sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                errors=[]
                for device,viewport in manifest['viewports'].items():
                    out=Path(temp)/'screenshots'/device;out.mkdir(parents=True,exist_ok=True)
                    context=browser.new_context(viewport=viewport,is_mobile=device=='mobile',has_touch=device=='mobile',locale='zh-CN',timezone_id='Asia/Shanghai')
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    def goto(path):
                        response=page.goto(base+path,wait_until='networkidle')
                        assert response.status==200,(path,response.status)
                    def shot(name):
                        if args.only and name not in args.only:return
                        page.evaluate('document.fonts.ready')
                        overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                        assert not overflow, f'Horizontal overflow: {device}/{name}'
                        page.screenshot(path=str(out/(name+'.png')),full_page=True)
                        manifest['screenshots'].append(f'docs/{device}/{name}.png')
                    def signin(role):
                        sid,password=credentials[role]
                        if role=='member':
                            goto('/login');page.fill('#student_id',sid)
                        else:goto('/admin/login')
                        page.fill('#password',password)
                        page.locator('button[type=submit]').click();page.wait_for_url('**/captain' if role=='member' else base+'/')
                    def logout():
                        # Submit through a real CSRF-protected UI form even when the menu is collapsed.
                        page.locator('#site-nav form').evaluate('(form)=>form.requestSubmit()')
                        page.wait_for_url('**/login')
                    goto('/login');shot('login')
                    page.fill('#student_id',credentials['member'][0]);page.wait_for_selector('#captain-greeting.ok');shot('login_name')
                    for _ in range(3):
                        page.fill('#password',secrets.token_urlsafe(12));page.locator('.btn-captain').click();page.wait_for_load_state('networkidle')
                    page.wait_for_selector('#forgot-prompt');page.fill('#password','');shot('captain_forgot_prompt')
                    goto('/forgot-password');shot('forgot_password')
                    goto('/admin/login');shot('admin_login')
                    signin('member');shot('captain_home')
                    goto('/password');shot('change_password')
                    toggle=page.locator('.pw-toggle').first;toggle.click();assert page.locator('input[name=current_password]').get_attribute('type')=='text';shot('password_toggle')
                    goto('/bookings/new');page.fill('#captain_phone','13800138000');page.fill('#captain_email','captain@example.com')
                    page.check('#include_other');page.fill('[name=other_name]','周同学');page.fill('[name=other_student_id]','DEMO003');page.fill('[name=other_contact]','13900139000');shot('booking_form')
                    goto(f'/bookings/{ids["booking"]}/submitted');shot('booking_submitted')
                    goto('/tools/new');shot('tool_borrow')
                    # Added tool names must stay literal even when containing HTML punctuation.
                    page.locator('#add-tool').click();assert page.locator('.tool-row').count()==2
                    goto('/records');shot('records')
                    if device=='mobile':
                        assert page.locator('.responsive-table tr').last.evaluate('(el)=>getComputedStyle(el).display')=='block'
                        page.locator('#nav-toggle').click();assert page.locator('#site-nav').is_visible();shot('mobile_navigation');page.locator('#nav-toggle').click()
                    trigger=page.locator('.js-locked-cancel').first;trigger.click();page.wait_for_selector('#lock-modal:not([hidden])');shot('cancel_locked');page.keyboard.press('Escape');assert not page.locator('#lock-modal').is_visible();assert trigger.evaluate('(el)=>el===document.activeElement')
                    goto('/records?tab=tools');shot('tool_records')
                    goto(f'/tools/{ids["loan"]}/return');shot('tool_return')
                    goto(f'/schedule?date={day}&view=week');shot('schedule')
                    goto('/bookings');shot('my_bookings')
                    logout();signin('admin');shot('admin_home')
                    goto('/review/');shot('review_list')
                    page.locator('.js-open-reject').first.click();page.wait_for_selector('#reject-modal:not([hidden])');page.fill('#reject-reason','设备维护，请调整测试时段。');shot('reject_dialog');page.keyboard.press('Escape')
                    goto('/tools/admin');shot('admin_tools');shot('admin_tools_inventory')
                    logout();signin('superadmin');shot('super_home')
                    for name,path in [('super_people','/people/'),('people_import','/people/import'),('super_admins','/managers/'),('super_user_activity','/activity/users'),('super_admin_activity','/activity/admins'),('security_center','/security/'),('super_admin_accounts','/users/'),('labs','/labs/'),('duties','/duties/'),('tool_catalog','/tools/catalog'),('mail','/mail/'),('stats',f'/stats/?start_date={day}&end_date={day}')]:
                        goto(path);shot(name)
                    # Audit the smallest supported phone width on key high-density pages.
                    if device=='mobile':
                        page.set_viewport_size({'width':320,'height':740})
                        for path in ['/people/','/managers/','/security/','/tools/admin']:
                            goto(path)
                            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'),f'320px overflow: {path}'
                        manifest['checks'].append('320px phone layout: personnel, managers, security, loans')
                    context.close()
                    manifest['checks'].append(f'{device}: login, name masking, forgot prompt, password toggle, nav, cards, dialogs, all screenshot routes')
                    print(f'{device}: screenshots and browser checks passed',flush=True)
                assert not errors,errors
                browser.close()
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()
        # Publish only after the entire browser run passes; failed runs keep previous docs intact.
        for relative in manifest['screenshots']:
            target=ROOT/relative
            source=Path(temp)/'screenshots'/target.parent.name/target.name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            if target.parent.name=='desktop':shutil.copyfile(source,ROOT/'docs'/target.name)
            elif target.name=='login.png':shutil.copyfile(source,ROOT/'docs'/'login_mobile.png')
    (ROOT/'docs'/'screenshots.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(f'Captured {len(manifest["screenshots"])} screenshots; production data and tunnel untouched.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
