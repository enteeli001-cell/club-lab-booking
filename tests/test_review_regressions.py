"""Regression coverage for the November handoff review."""
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import event

from app import db
from app.import_preview import save_preview
from app.inventory import inventory_rows
from app.models import AuditLog, Booking, BookingCompanion, DutyShift, Lab, TeamMember, ToolCatalog, ToolLoan, User
from app.utils import apply_team_roster, local_now, safe_local_url
from tests.conftest import FUTURE_DAY, add_booking, api_headers, booking_form, login
from tests.test_tools import _loan_form


@pytest.mark.parametrize('status', ['pending', 'approved'])
@pytest.mark.parametrize('minutes', [30, 29, -1])
def test_api_cannot_cancel_locked_booking(app, client, seed, monkeypatch, status, minutes):
    now = local_now().replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr('app.models.local_now', lambda: now)
    with app.app_context():
        lab = db.session.get(Lab, seed['lab_id'])
        lab.contact_name, lab.contact_phone = '值班联络人', '13800138000'
        start = now + timedelta(minutes=minutes)
        booking = add_booking(lab, db.session.get(User, seed['member_id']), day=start.date(),
                              start=start.time(), end=(start+timedelta(minutes=45)).time(), status=status)
        bid = booking.id
    response = client.delete(f'/api/bookings/{bid}', headers=api_headers('2021001'))
    assert response.status_code == 400
    assert '值班联络人' in response.json['error']
    with app.app_context():
        assert db.session.get(Booking, bid).status == status


@pytest.mark.parametrize('state', ['disabled', 'expired', 'initial_password'])
def test_api_respects_account_state(app, client, seed, state):
    with app.app_context():
        user = db.session.get(User, seed['member_id'])
        if state == 'disabled': user.enabled = False
        elif state == 'expired': user.valid_until = local_now().date() - timedelta(days=1)
        else: user.must_change_password = True
        db.session.commit()
    assert client.get('/api/me', headers=api_headers('2021001')).status_code == 403


@pytest.mark.parametrize('payload', [[], ['bad'], {'purpose': 2}, {'name': {}}, {'enabled': 'false'}])
def test_invalid_api_payload_returns_400(client, seed, payload):
    response = client.post('/api/bookings', headers=api_headers('2021001'), json=payload)
    assert response.status_code == 400
    assert response.json['ok'] is False


def test_api_booking_requires_phone_and_regular_role(app, client, seed):
    data = booking_form(seed['lab_id'])
    data.pop('captain_phone')
    assert client.post('/api/bookings', headers=api_headers('2021001'), json=data).status_code == 400
    assert client.post('/api/bookings', headers=api_headers('officer01'), json=data).status_code == 403
    data['captain_phone'] = '13800138000'
    response = client.post('/api/bookings', headers=api_headers('2021001'), json=data)
    assert response.status_code == 201
    with app.app_context():
        log = AuditLog.query.filter_by(action='booking.create').one()
        assert log.actor_id == seed['member_id']


@pytest.mark.parametrize('value', ['//example.com', '/\\example.com', '/\nevil', 'https://example.com'])
def test_redirect_normalization_rejects_external_paths(value):
    assert not safe_local_url(value)
    assert safe_local_url('/records?tab=tools')


def test_name_lookup_and_password_pages_not_cached(client, seed):
    for path in ['/login', '/login/captain-name?student_id=2021001']:
        assert client.get(path).headers['Cache-Control'] == 'no-store'


def test_schedule_renders_non_aligned_bookings(app, client, seed):
    with app.app_context():
        add_booking(db.session.get(Lab, seed['lab_id']), db.session.get(User, seed['member_id']),
                    start=time(10,15), end=time(11,15), status='approved')
    login(client, '2021001')
    response = client.get(f'/schedule?date={FUTURE_DAY.isoformat()}')
    text = response.get_data(as_text=True)
    assert 'cell approved' in text
    assert '10:15–11:15' in text
    assert 'mobile-agenda' in text


def test_roster_edit_preserves_historical_companion(app, seed):
    with app.app_context():
        user = db.session.get(User, seed['member_id'])
        assert apply_team_roster(user, '原队伍', [{'name':'原队友', 'student_id':'M001'}]) == []
        db.session.commit()
        member = TeamMember.query.filter_by(captain_id=user.id).one()
        booking = add_booking(db.session.get(Lab, seed['lab_id']), user)
        companion = BookingCompanion(booking=booking, name=member.name, student_id=member.student_id,
                                     team_member_id=member.id, source='member')
        db.session.add(companion); db.session.commit()
        assert apply_team_roster(user, '新队伍', [{'name':'新队友', 'student_id':'M002'}]) == []
        db.session.commit(); db.session.expire_all()
        assert companion.name == '原队友'
        assert companion.student_id == 'M001'
        assert companion.team_member_id is None


def test_hidden_other_companion_not_submitted(app, client, seed):
    login(client, '2021001')
    data = booking_form(seed['lab_id'], other_name=['未勾选的人员'], other_student_id=['M999'], other_contact=['13800138000'])
    client.post('/bookings/new', data=data)
    with app.app_context():
        assert BookingCompanion.query.count() == 0


def test_invalid_loan_does_not_leave_orphan_photo(app, client, seed):
    login(client, '2021001')
    response = client.post('/tools/new', data=_loan_form(seed['lab_id'], contact='invalid'), content_type='multipart/form-data')
    assert response.status_code == 200
    assert not list(Path(app.config['UPLOAD_DIR']).glob('*'))
    with app.app_context(): assert ToolLoan.query.count() == 0


def test_concurrent_loan_cannot_overdraw_stock(app, seed):
    with app.app_context():
        item = ToolCatalog(name='唯一工具', total_quantity=1, is_major=True)
        db.session.add(item); db.session.commit(); cid=item.id
    barrier = Barrier(2)
    def borrow(uid):
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['_user_id'] = str(uid); session['_fresh'] = True
            barrier.wait(timeout=10)
            return client.post('/tools/new', data=_loan_form(seed['lab_id'], tool_catalog_id=[str(cid)],
                               tool_name=[''], tool_quantity=['1']), content_type='multipart/form-data').status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(borrow, [seed['member_id'], seed['other_id']]))
    assert sorted(responses) == [200,302]
    with app.app_context():
        assert ToolLoan.query.count() == 1
        assert inventory_rows()[0]['remaining'] == 0
        assert len(list(Path(app.config['UPLOAD_DIR']).glob('*'))) == 1


def test_inventory_listing_is_one_aggregate_query(app, seed):
    with app.app_context():
        db.session.add_all([ToolCatalog(name=f'工具{i}', total_quantity=4) for i in range(20)])
        db.session.commit()
        queries=[]
        def capture(conn, cursor, statement, parameters, context, executemany): queries.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try: assert len(inventory_rows()) == 20
        finally: event.remove(db.engine, 'before_cursor_execute', capture)
        assert len(queries) == 1


def test_stock_catalog_cannot_hide_outstanding_loans(app, client, seed):
    with app.app_context():
        item=ToolCatalog(name='卡尺', total_quantity=2); db.session.add(item); db.session.commit(); cid=item.id
    login(client,'2021001')
    client.post('/tools/new',data=_loan_form(seed['lab_id'],tool_catalog_id=[str(cid)],tool_name=[''],tool_quantity=['2']),content_type='multipart/form-data')
    client.post('/logout');login(client,'admin',staff=True)
    response=client.post(f'/tools/catalog/{cid}/update', data={'name':'卡尺','total_quantity':'1'},follow_redirects=True)
    assert '不能少于' in response.get_data(as_text=True)
    response=client.post(f'/tools/catalog/{cid}/delete', follow_redirects=True)
    assert '不能删除' in response.get_data(as_text=True)
    with app.app_context(): assert db.session.get(ToolCatalog,cid).total_quantity == 2


def test_export_does_not_execute_user_text_as_formula(app, client, seed):
    with app.app_context():
        user=db.session.get(User,seed['member_id']); user.name='=HYPERLINK("https://example.com")'
        add_booking(db.session.get(Lab,seed['lab_id']),user,status='approved',purpose='=1+1')
    login(client,'officer01',staff=True)
    response=client.get(f'/stats/export?start_date={FUTURE_DAY}&end_date={FUTURE_DAY}')
    workbook=load_workbook(io.BytesIO(response.data))
    assert workbook['明细']['I2'].value == '=1+1'
    assert workbook['明细']['I2'].data_type == 's'
    assert all(cell.data_type != 'f' for sheet in workbook for row in sheet for cell in row)


def test_large_import_preview_stays_out_of_session(app, client, seed):
    login(client,'admin',staff=True)
    workbook=Workbook();sheet=workbook.active
    sheet.append(['队名','队长姓名','队长学号','队员1姓名','队员1学号'])
    for i in range(50): sheet.append([f'队伍{i}',f'队长{i}',f'IMPORT{i}',f'成员{i}',f'MEMBER{i}'])
    buf=io.BytesIO();workbook.save(buf);buf.seek(0)
    response=client.post('/people/import',data={'file':(buf,'teams.xlsx')},content_type='multipart/form-data')
    assert response.status_code == 200
    with client.session_transaction() as session:
        key=session['people_import_preview']
        assert isinstance(key,str) and len(key)==64
    assert len(response.headers.get('Set-Cookie','')) < 1000
    assert client.post('/people/import/confirm').status_code == 302
    with app.app_context(): assert User.query.filter(User.student_id.like('IMPORT%')).count()==50
    assert not list((Path(app.config['DATA_DIR'])/'import_previews').glob('*.json'))


def test_import_confirm_revalidates_accounts(app, client, seed):
    login(client,'admin',staff=True)
    with app.test_request_context():
        save_preview([{'row':2,'name':'导入队长','student_id':'COLLISION','team_name':'队伍','members':[], 'phone':'','email':''}])
        from flask import session
        key=session['people_import_preview']
    with client.session_transaction() as session: session['people_import_preview']=key
    with app.app_context():
        user=User(student_id='COLLISION',name='新管理员',role='admin');user.set_password('Example#Dummy123')
        db.session.add(user);db.session.commit()
    response=client.post('/people/import/confirm',follow_redirects=True)
    assert '跳过 1 行' in response.get_data(as_text=True)
    with app.app_context(): assert User.query.filter_by(student_id='COLLISION').one().is_admin


def test_superadmin_primary_entries_follow_confirmed_order(client, seed):
    login(client,'admin',staff=True)
    text=client.get('/').get_data(as_text=True)
    entries=['人员信息管理','管理员管理','普通登录人员操作详情','管理员操作信息','安全防护中心']
    positions=[text.index('<h2>'+entry) for entry in entries]
    assert positions == sorted(positions)


def test_json_security_summary_redacts_nested_credentials(app, client):
    from app.models import SecurityEvent
    response=client.post('/api/bookings',headers={'Authorization':'Bearer wrong'},
                         json={'purpose':'union select','nested':{'password':'dummy-sensitive-value'}})
    assert response.status_code == 401
    with app.app_context():
        log=SecurityEvent.query.filter_by(event_type='injection').one()
        assert 'dummy-sensitive-value' not in log.query_summary
        assert '[redacted]' in log.query_summary


def test_account_deletion_retains_login_and_audit_snapshots(app, client, seed):
    from app.models import LoginLog
    login(client,'officer01',staff=True);client.post('/logout')
    with app.app_context():
        db.session.add(AuditLog(actor_id=seed['officer_id'],actor_student_id='officer01',actor_role='admin',
                                action='example',summary='历史操作'))
        db.session.commit()
    login(client,'admin',staff=True)
    response=client.post(f'/managers/{seed["officer_id"]}/delete',follow_redirects=True)
    assert '已删除' in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User,seed['officer_id']) is None
        assert LoginLog.query.filter_by(student_id='officer01').one().user_id is None
        assert AuditLog.query.filter_by(action='example').one().actor_student_id == 'officer01'


def test_cannot_delete_user_with_tool_history(app, client, seed):
    login(client,'2021001')
    client.post('/tools/new',data=_loan_form(seed['lab_id']),content_type='multipart/form-data')
    client.post('/logout');login(client,'admin',staff=True)
    response=client.post(f'/users/{seed["member_id"]}/delete',follow_redirects=True)
    assert '不能删除' in response.get_data(as_text=True)
    with app.app_context():assert db.session.get(User,seed['member_id']) is not None


def test_all_screenshot_routes_render_without_production_data(tmp_path):
    from scripts.capture_ui import demo_app
    app, credentials, ids, day=demo_app(tmp_path)
    with app.test_client() as client:
        for path in ['/login','/forgot-password','/admin/login']:
            assert client.get(path).status_code == 200
        with client.session_transaction() as session:session['_user_id']=str(ids['captain'])
        for path in ['/captain','/password','/bookings/new',f'/bookings/{ids["booking"]}/submitted',
                     '/tools/new','/records','/records?tab=tools',f'/tools/{ids["loan"]}/return',
                     f'/schedule?date={day}&view=week','/bookings']:
            assert client.get(path).status_code==200,path
        with app.app_context():uid=User.query.filter_by(role='superadmin').one().id
        with client.session_transaction() as session:session['_user_id']=str(uid)
        for path in ['/','/review/','/tools/admin','/people/','/people/import','/managers/','/activity/users',
                     '/activity/admins','/security/','/users/','/labs/','/duties/','/tools/catalog','/mail/',
                     f'/stats/?start_date={day}&end_date={day}']:
            assert client.get(path).status_code==200,path


def test_reset_test_data_never_touches_production_paths(tmp_path,monkeypatch):
    import importlib.util
    import sys
    root=Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root/'scripts'))
    spec=importlib.util.spec_from_file_location('isolated_reset',root/'scripts'/'reset_test_data.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    data=tmp_path/'data';data.mkdir();logs=tmp_path/'logs';logs.mkdir()
    production=data/'app.db';production.write_bytes(b'production sentinel')
    log=logs/'app.log';log.write_text('production log')
    demo=data/'testing';demo.mkdir();(demo/'app.db').write_bytes(b'old testing data')
    monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'DEMO_DATA',demo)
    monkeypatch.setattr(module,'DEMO_LOGS',logs/'testing')
    from app import create_app
    monkeypatch.setattr(module,'create_demo_app',lambda:create_app({
        "TESTING":True,"SECRET_KEY":"test-only","DATA_DIR":str(demo),
        "LOG_DIR":str(logs/"testing"),"API_TOKEN":"test-only"}))
    assert module.main()==0
    assert production.read_bytes()==b'production sentinel'
    assert log.read_text()=='production log'
    assert (demo/"app.db").is_file()


def test_reset_refuses_symlink_to_production(tmp_path,monkeypatch):
    import importlib.util
    root=Path(__file__).resolve().parents[1];monkeypatch.syspath_prepend(str(root/'scripts'))
    spec=importlib.util.spec_from_file_location('isolated_reset_symlink',root/'scripts'/'reset_test_data.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    data=tmp_path/'data';data.mkdir();(data/'app.db').write_bytes(b'production sentinel')
    demo=data/'testing';demo.symlink_to(data,target_is_directory=True)
    monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'DEMO_DATA',demo)
    monkeypatch.setattr(module,'DEMO_LOGS',tmp_path/'logs'/'testing')
    with pytest.raises(SystemExit):module.main()
    assert (data/'app.db').read_bytes()==b'production sentinel'


def test_seed_user_rerun_preserves_password(app,monkeypatch):
    import sys
    root=Path(__file__).resolve().parents[1];monkeypatch.syspath_prepend(str(root/'scripts'))
    from scripts.seed_test_data import get_or_create_user
    with app.app_context():
        user=get_or_create_user('SEED001','队长','member');db.session.commit()
        user.set_password('Dummy#Changed123',require_change=False);db.session.commit()
        assert get_or_create_user('SEED001','队长','member').check_password('Dummy#Changed123')


def test_restart_calls_stop_then_start(tmp_path):
    import subprocess
    root=Path(__file__).resolve().parents[1]
    scripts=tmp_path/'scripts';scripts.mkdir();(tmp_path/'data').mkdir()
    python=tmp_path/'.venv'/'bin'/'python';python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nexit 0\n');python.chmod(0o700)
    runner=scripts/'restart_app.sh';runner.write_text((root/'scripts'/'restart_app.sh').read_text())
    log=tmp_path/'calls.txt'
    for name in ['stop_all.sh','start_all.sh']:
        (scripts/name).write_text(f'#!/bin/sh\necho {name} >> "{log}"\n')
    result=subprocess.run(['bash',str(runner)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert log.read_text().split()==['stop_all.sh','start_all.sh']


def test_security_referrer_redacts_secret_query(app, client):
    from app.models import SecurityEvent
    client.get('/.env',headers={'Referer':'https://example.com/login?password=dummy-credential&next=%2Frecords'})
    with app.app_context():
        log=SecurityEvent.query.filter_by(event_type='path_scan').one()
        assert 'dummy-credential' not in log.referer
        assert 'next=%2Frecords' in log.referer


def test_two_concurrent_approvals_cannot_double_book(app,seed):
    with app.app_context():
        lab=db.session.get(Lab,seed['lab_id'])
        one=add_booking(lab,db.session.get(User,seed['member_id']))
        two=add_booking(lab,db.session.get(User,seed['other_id']))
        ids=[one.id,two.id]
    barrier=Barrier(2)
    def approve(bid):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            return client.post(f'/api/bookings/{bid}/approve',headers=api_headers('officer01')).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(approve,ids))
    assert sorted(results)==[200,400]
    with app.app_context():assert Booking.query.filter_by(status='approved').count()==1


def test_old_audit_cannot_undo_later_review(app,client,seed):
    with app.app_context():
        booking=add_booking(db.session.get(Lab,seed['lab_id']),db.session.get(User,seed['member_id']),status='approved')
        booking.reviewed_at=local_now();booking.reviewed_by_id=seed['officer_id']
        log=AuditLog(actor_id=seed['officer_id'],actor_role='admin',action='booking.approve',
                     target_id=booking.id,summary='早前审核',reversible=True,
                     created_at=local_now()-timedelta(hours=1))
        db.session.add(log);db.session.commit();bid,lid=booking.id,log.id
    login(client,'admin',staff=True)
    response=client.post(f'/activity/logs/{lid}/undo',follow_redirects=True)
    assert '后续审核' in response.get_data(as_text=True)
    with app.app_context():assert db.session.get(Booking,bid).status=='approved'
