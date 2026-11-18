"""Completion of the system-owned strings left after the original i18n rollout."""
from html.parser import HTMLParser
from pathlib import Path
import json
import re

import pytest

from app import db, i18n
from app.models import Lab
from tests.conftest import login, api_headers


@pytest.mark.parametrize(('path','english','traditional'),[
    ('/users/import','Bulk import accounts','批量導入帳號'),
    ('/users/new','Add a team member','添加一名隊員'),
    ('/people/import','Import competition teams','導入參賽隊伍'),
    ('/labs/new','Use current location','使用當前位置'),
    ('/duties/','No duty shifts.','暫無值班安排。'),
    ('/mail/','No delivery records.','暫無發送記錄。'),
    ('/security/','Detection is passive only','只做被動檢測與記錄'),
    ('/security/wipe','Permanently erase all personal information','永久清除所有個人信息'),
    ('/bookings','My test bookings','我的測試預約'),
])
@pytest.mark.parametrize('language',['en','zh-Hant'])
def test_management_help_and_labels_are_translated(client,seed,path,english,traditional,language):
    login(client,'admin',staff=True);client.set_cookie('lang',language)
    response=client.get(path)
    assert response.status_code==200
    assert (english if language=='en' else traditional) in response.get_data(as_text=True)


def test_runtime_messages_translate_without_translating_inserted_names(app):
    name="测试预约 O'Brien <tag>\n甲"
    with app.test_request_context('/?lang=en'):
        message=f'已添加实验室：{name}。'
        assert i18n.t(message)==f'Lab added: {name}.'
        assert i18n.t('导入完成：新增 2 支队伍，更新 3 支，跳过 1 行。')=='Import complete: 2 teams created, 3 updated, 1 rows skipped.'
        assert i18n.t('当前状态为「已通过」，无法撤销审核。')=='The current status is Approved; the review cannot be undone.'
        assert i18n.t('已关闭「测试预约」。')=='Test booking: disabled.'
        assert i18n.t('选择“其他”时，其他人员的姓名、学号和联系方式须全部填写。请填写 姓名、学号。').endswith('Complete Name, Student ID.')
    with app.test_request_context('/?lang=zh-Hant'):
        assert i18n.t('已关闭「测试预约」。')=='已關閉「測試預約」。'


class ConfirmForms(HTMLParser):
    def __init__(self):super().__init__();self.confirm=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form' and 'data-confirm' in attrs:self.confirm.append(attrs)


def test_lab_name_in_confirmation_is_text_not_executable_javascript(app,client,seed):
    name="测试预约'); window.bad = 1; //"
    with app.app_context():db.session.get(Lab,seed['lab_id']).name=name;db.session.commit()
    login(client,'admin',staff=True);client.set_cookie('lang','en')
    response=client.get('/admin/checkin')
    # Resolve the route through Flask rather than assuming a path used by a prior release.
    if response.status_code==404:
        with app.test_request_context():
            from flask import url_for
            path=url_for('checkin.admin_codes')
        response=client.get(path)
    parser=ConfirmForms();parser.feed(response.get_data(as_text=True))
    assert response.status_code==200
    assert any(name in form['data-confirm'] for form in parser.confirm)
    assert all('onsubmit' not in form for form in parser.confirm)
    assert 'window.bad' not in ''.join(re.findall(r'<script[^>]*>(.*?)</script>',response.get_data(as_text=True),re.S))


def test_fixed_erasure_confirmation_phrase_is_never_localized(client,seed):
    from app.wipe import WIPE_PHRASE
    login(client,'admin',staff=True);client.set_cookie('lang','en')
    page=client.get('/security/wipe').get_data(as_text=True)
    assert WIPE_PHRASE in page
    assert 'Permanently erase all personal information' in page


def test_api_validation_messages_follow_site_language(client,seed):
    client.set_cookie('lang','en')
    response=client.post('/api/bookings',headers=api_headers('2021001'),json=[])
    assert response.status_code==400
    assert response.json['error']=='The JSON request body must be an object.'


def test_all_generated_message_templates_keep_translations_and_fields(app):
    from string import Formatter
    for source,_pattern,translations in i18n._FORMAT_MESSAGES:
        required={field for _literal,field,_spec,_conv in Formatter().parse(source) if field is not None}
        for translated in translations:
            actual={field for _literal,field,_spec,_conv in Formatter().parse(translated) if field is not None}
            assert actual==required,source
    with app.test_request_context('/?lang=en'):
        assert i18n.duration_label(125)=='2h 5m'
    with app.test_request_context('/?lang=zh-Hant'):
        assert i18n.duration_label(125)=='2小時5分鐘'
