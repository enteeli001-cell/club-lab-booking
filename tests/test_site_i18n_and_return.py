"""Site-wide language persistence and the narrowly scoped return-photo exemption."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from html.parser import HTMLParser

import pytest

from app import db, i18n, toggles
from app.models import AuditLog, Lab, ToolCatalog, ToolLoan, ToolLoanItem, User
from app.utils import local_now, MINIMAL_PNG_BYTES
from tests.conftest import login
from tests.test_tools import _png


@pytest.fixture
def loan_id(app, seed):
    with app.app_context():
        user=db.session.get(User,seed['member_id'])
        lab=db.session.get(Lab,seed['lab_id'])
        item=ToolCatalog(name='工具',total_quantity=2,is_major=True)
        loan=ToolLoan(user=user,lab=lab,contact='13800138000',borrow_at=local_now(),
                      expected_return_at=local_now(),borrow_photo='borrow.png',status='borrowing')
        db.session.add_all([item,loan]);db.session.flush()
        db.session.add(ToolLoanItem(loan=loan,catalog_id=item.id,name=item.name,quantity=1,is_other=False))
        db.session.commit()
        path=Path(app.config['UPLOAD_DIR']);path.mkdir(parents=True,exist_ok=True)
        (path/'borrow.png').write_bytes(MINIMAL_PNG_BYTES)
        return loan.id


@pytest.mark.parametrize(('sid','staff','exempt'), [('2021001',False,False),('officer01',True,False),('admin',True,True)])
def test_only_superadmin_form_omits_photo(client,seed,loan_id,sid,staff,exempt):
    login(client,sid,staff=staff)
    page=client.get(f'/tools/{loan_id}/return').get_data(as_text=True)
    assert ('name="return_photo"' not in page)==exempt
    assert ('总管理员确认归还无需上传照片。' in page)==exempt
    if not exempt:assert 'capture="environment" required' in page


@pytest.mark.parametrize(('sid','staff','exempt'), [('2021001',False,False),('officer01',True,False),('admin',True,True)])
def test_photo_exemption_is_enforced_server_side(app,client,seed,loan_id,sid,staff,exempt):
    login(client,sid,staff=staff)
    response=client.post(f'/tools/{loan_id}/return',data={'is_superadmin':'1','photo_exempt':'1'})
    assert response.status_code==(302 if exempt else 200)
    with app.app_context():
        loan=db.session.get(ToolLoan,loan_id)
        assert loan.status==('returned' if exempt else 'borrowing')
        assert loan.return_photo is None
        assert bool(loan.actual_return_at)==exempt
        audits=AuditLog.query.filter_by(action='tool.return').all()
        assert len(audits)==int(exempt)
        if exempt:
            assert audits[0].actor_id==seed['admin_id']
            assert json.loads(audits[0].payload)['photo_exempt'] is True
            from app.inventory import inventory_rows
            assert inventory_rows()[0]['remaining']==2


@pytest.mark.parametrize(('sid','staff'), [('2021001',False),('officer01',True),('admin',True)])
def test_return_with_valid_photo_still_works(app,client,seed,loan_id,sid,staff):
    login(client,sid,staff=staff)
    response=client.post(f'/tools/{loan_id}/return',data={'return_photo':_png('return.png')},content_type='multipart/form-data')
    assert response.status_code==302
    with app.app_context():
        loan=db.session.get(ToolLoan,loan_id)
        assert loan.status=='returned' and loan.return_photo
        assert json.loads(AuditLog.query.filter_by(action='tool.return').one().payload)['photo_exempt'] is False


def test_other_captain_cannot_return_someone_elses_loan(app,client,seed,loan_id):
    login(client,'2021002')
    response=client.post(f'/tools/{loan_id}/return')
    assert response.status_code==302
    with app.app_context():assert db.session.get(ToolLoan,loan_id).status=='borrowing'


def test_superadmin_optional_invalid_photo_is_not_saved(app,client,seed,loan_id):
    import io
    login(client,'admin',staff=True)
    response=client.post(f'/tools/{loan_id}/return',data={'return_photo':(io.BytesIO(b'invalid'),'photo.png')},content_type='multipart/form-data')
    assert response.status_code==200
    with app.app_context():assert db.session.get(ToolLoan,loan_id).status=='borrowing'


@pytest.mark.parametrize(('lang','phrase','html'), [('en','Test booking','en'),('zh-Hant','測試預約','zh-Hant'),('zh-Hans','测试预约','zh-CN')])
def test_login_choice_persists_across_site_and_logout(client,seed,lang,phrase,html):
    client.get('/login?lang='+lang)
    response=login(client,'2021001')
    assert response.status_code==200
    assert f'<html lang="{html}">' in response.get_data(as_text=True)
    for path in ['/captain','/bookings/new','/records','/tools/new','/password','/announcements/']:
        response=client.get(path)
        assert response.status_code==200,path
        assert f'<html lang="{html}">' in response.get_data(as_text=True)
        assert phrase in response.get_data(as_text=True)
        assert response.headers['Content-Language']==lang
        assert 'header-language' in response.get_data(as_text=True)
    response=client.post('/logout',follow_redirects=True)
    assert f'<html lang="{html}">' in response.get_data(as_text=True)


@pytest.mark.parametrize(('sid','lang','expected'), [('officer01','en','Admin console'),('officer01','zh-Hant','管理後台'),('admin','en','People and teams'),('admin','zh-Hant','人員信息管理')])
def test_admin_pages_keep_language(client,seed,sid,lang,expected):
    client.get('/admin/login?lang='+lang)
    response=login(client,sid,staff=True)
    assert expected in response.get_data(as_text=True)
    response=client.get('/tools/admin')
    assert ('Major tool inventory' if lang=='en' else '主要工具庫存') in response.get_data(as_text=True)


class SwitchLinks(HTMLParser):
    def __init__(self):super().__init__();self.urls=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a' and attrs.get('hreflang')=='en':self.urls.append(attrs['href'])


def test_switch_preserves_resource_and_filter_parameters(app,client,seed,loan_id):
    login(client,'admin',staff=True)
    response=client.get(f'/tools/{loan_id}/return?page=2&status=borrowing&next=%2Frecords')
    parser=SwitchLinks();parser.feed(response.get_data(as_text=True))
    assert len(parser.urls)==1
    target=urlsplit(parser.urls[0])
    assert target.path==f'/tools/{loan_id}/return'
    assert parse_qs(target.query)=={'page':['2'],'status':['borrowing'],'next':['/records'],'lang':['en']}
    response=client.get(parser.urls[0])
    assert 'Super administrators can confirm returns without uploading a photo.' in response.get_data(as_text=True)
    assert any(cookie.startswith('lang=en;') for cookie in response.headers.getlist('Set-Cookie'))


@pytest.mark.parametrize('lang',['en','zh-Hant'])
def test_default_maintenance_page_and_switch_are_translated(app,client,lang):
    with app.app_context():toggles.set_maintenance(True)
    response=client.get('/login?lang='+lang)
    page=response.get_data(as_text=True)
    assert response.status_code==503
    assert ('Site maintenance' if lang=='en' else '網站升級中') in page
    assert ('The site is being upgraded.' if lang=='en' else '網站正在升級') in page
    assert 'lang-switch' in page
    assert any(cookie.startswith('lang='+lang+';') for cookie in response.headers.getlist('Set-Cookie'))


def test_custom_maintenance_text_is_not_translated(app,client):
    with app.app_context():
        toggles.set_maintenance_message('测试预约')
        toggles.set_maintenance(True)
    page=client.get('/login?lang=en').get_data(as_text=True)
    assert '<p class="maint-msg">测试预约</p>' in page


@pytest.mark.parametrize('json_request',[False,True])
def test_disabled_feature_message_follows_language(app,client,seed,json_request):
    login(client,'2021001');client.set_cookie('lang','en')
    with app.app_context():toggles.set_feature('booking_create',False)
    response=client.get('/bookings/new',headers={'Accept':'application/json'} if json_request else {})
    assert response.status_code==403
    if json_request:assert 'temporarily disabled' in response.json['message']
    else:assert '<p>This feature has been temporarily disabled by the administrator.</p>' in response.get_data(as_text=True)


def test_names_lab_names_and_tools_are_never_translated(app,client,seed,loan_id):
    with app.app_context():
        db.session.get(User,seed['member_id']).name='测试预约'
        db.session.get(Lab,seed['lab_id']).name='实验室'
        db.session.commit()
    login(client,'2021001');client.set_cookie('lang','en')
    page=client.get('/captain').get_data(as_text=True)
    assert '<h1>测试预约, welcome to the test booking platform</h1>' in page
    page=client.get(f'/tools/{loan_id}/return').get_data(as_text=True)
    assert '</strong>实验室</p>' in page
    assert '工具 × 1' in page


def test_announcement_title_and_body_stay_original(app,client,seed):
    from app.models import Announcement
    with app.app_context():
        a=Announcement(title='测试预约',body='工具借用',level=1,created_by_id=seed['admin_id'])
        db.session.add(a);db.session.commit();aid=a.id
    login(client,'2021001');client.set_cookie('lang','en')
    page=client.get(f'/announcements/{aid}').get_data(as_text=True)
    assert '<h1>测试预约</h1>' in page
    assert '<div class="announce-body">工具借用</div>' in page


def test_unknown_language_defaults_and_query_overrides_cookie(client,seed):
    login(client,'2021001');client.set_cookie('lang','unknown')
    assert '<html lang="zh-CN">' in client.get('/captain').get_data(as_text=True)
    client.set_cookie('lang','en')
    assert '<html lang="zh-Hant">' in client.get('/captain?lang=zh-Hant').get_data(as_text=True)
    assert '<html lang="zh-Hant">' in client.get('/records').get_data(as_text=True)


def test_interface_translation_values_and_escaping(app,client,seed):
    with app.app_context():
        user=db.session.get(User,seed['member_id']);user.name='<b>测试预约</b>';db.session.commit()
    login(client,'2021001');client.set_cookie('lang','en')
    page=client.get('/captain').get_data(as_text=True)
    assert '&lt;b&gt;测试预约&lt;/b&gt;, welcome' in page
    assert '<h1><b>' not in page
    assert 'window.APP_I18N' in page
    assert '"Menu"' in page


def test_all_templates_compile_in_all_languages(app):
    for lang in i18n.LANG_CODES:
        with app.test_request_context('/captain?lang='+lang):
            for name in app.jinja_env.list_templates():app.jinja_env.get_template(name)
