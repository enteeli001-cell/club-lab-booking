"""Language options. English is the default for a visitor with no lang cookie."""
import re
from pathlib import Path

import pytest

from app import i18n, toggles
from tests.conftest import login


def text(resp):
    return resp.get_data(as_text=True)


def cjk(s):
    return re.findall(r"[一-鿿]+", s)


@pytest.mark.parametrize("path", ["/login", "/admin/login"])
def test_default_is_english_and_lists_every_language(client, path):
    client.delete_cookie("lang")
    resp = client.get(path)
    page = text(resp)
    assert resp.status_code == 200 and '<html lang="en">' in page
    assert "Club Lab Booking System" in page and "window.APP_I18N" in page
    for code, label in i18n.LANGUAGES:
        assert f"lang={code}" in page and label in page
    assert page.count('aria-current="true"') == 1
    assert not any(c.startswith("lang=") for c in resp.headers.getlist("Set-Cookie"))


def test_english_captain_login_is_fully_translated_and_remembered(client):
    resp = client.get("/login?lang=en")
    page = text(resp)
    assert resp.status_code == 200 and '<html lang="en">' in page
    for s in ("Club Lab Booking System", "Student ID", "Password", "Captain login", "Admin login", "Please note", "window.APP_I18N"):
        assert s in page
    body = page.split("<body", 1)[1]
    body = re.sub(r"<nav class=\"lang-switch\".*?</nav>", "", body, flags=re.S)
    body = re.sub(r"<script>window\.APP_I18N.*?</script>", "", body, flags=re.S)
    body = body.replace('aria-hidden="true">构</span>', "")
    assert cjk(body) == [], cjk(body)
    assert any(c.startswith("lang=en;") for c in resp.headers.getlist("Set-Cookie"))
    assert "Captain login" in text(client.get("/login"))  # cookie remembered
    assert "Admin entrance" in text(client.get("/admin/login"))
    assert "参赛队长登录" in text(client.get("/login?lang=zh-Hans"))  # and can switch back
    assert "参赛队长登录" in text(client.get("/login"))


def test_english_admin_login_is_fully_translated(client):
    page = text(client.get("/admin/login?lang=en"))
    body = page.split("<body", 1)[1]
    body = re.sub(r"<nav class=\"lang-switch\".*?</nav>", "", body, flags=re.S)
    body = re.sub(r"<script>window\.APP_I18N.*?</script>", "", body, flags=re.S)
    body = body.replace('aria-hidden="true">构</span>', "")
    assert cjk(body) == [], cjk(body)
    assert "Enter admin console" in page and "Back to captain login" in page


def test_traditional_chinese(client):
    page = text(client.get("/login?lang=zh-Hant"))
    assert '<html lang="zh-Hant">' in page
    for s in ("社團實驗室預約管理系統", "學號", "密碼", "參賽隊長登入", "注意事項", "管理員登入"):
        assert s in page
    assert "参赛队长登录" not in page and "学号" not in page
    page = text(client.get("/admin/login"))
    assert "進入管理後台" in page and "管理員入口" in page


def test_invalid_language_is_ignored(client):
    client.delete_cookie("lang")
    resp = client.get("/login?lang=xx")
    assert "Captain login" in text(resp) and not any(c.startswith("lang=") for c in resp.headers.getlist("Set-Cookie"))
    client.set_cookie("lang", "klingon")
    assert "Captain login" in text(client.get("/login"))


def test_switch_links_keep_next(client):
    page = text(client.get("/login?next=/schedule&lang=en"))
    assert "/login?lang=zh-Hant&amp;next=/schedule" in page or "/login?lang=zh-Hant&amp;next=%2Fschedule" in page
    assert 'name="next" value="/schedule"' in page


def test_login_messages_are_translated(app, client, seed):
    resp = client.post("/login?lang=en", data={"student_id": "2021001", "password": "wrong"})
    assert "Incorrect student ID or password." in text(resp)
    resp = client.post("/admin/login", data={"password": "nope"})
    assert "Incorrect password." in text(resp)
    with app.app_context():
        toggles.set_feature("captain_login", False)
    assert "temporarily disabled by the administrator" in text(client.get("/login"))
    with app.app_context():
        toggles.set_feature("captain_login", True)
    other = app.test_client()
    resp = other.post("/login?lang=zh-Hant", data={"student_id": "2021001", "password": "wrong"})
    assert "學號或密碼不正確。" in text(resp)


def test_captain_name_lookup_follows_language(client, seed):
    client.delete_cookie("lang")
    assert client.get("/login/captain-name?student_id=2021001").get_json()["greeting"] == "Hello, 张*三"
    client.get("/login?lang=zh-Hans")
    assert client.get("/login/captain-name?student_id=2021001").get_json()["greeting"] == "张*三 同学，您好"
    client.get("/login?lang=en")
    data = client.get("/login/captain-name?student_id=2021001").get_json()
    assert data["greeting"] == "Hello, 张*三" and "only for yourself" in data["hint"]
    assert client.get("/login/captain-name?student_id=nobody").get_json()["error"] == "No captain account found"
    client.get("/login?lang=zh-Hant")
    assert client.get("/login/captain-name?student_id=2021001").get_json()["greeting"] == "张*三 同學，您好"


def test_login_keeps_selected_language_after_redirect(client, seed):
    client.get("/login?lang=en")
    resp = login(client, "2021001")
    page = text(resp)
    assert resp.status_code == 200 and "welcome to the test booking platform" in page
    assert '<html lang="en">' in page and "Club Lab Booking System" in page and "header-language" in page
    assert "window.APP_I18N" in page


@pytest.mark.parametrize(("lang", "label", "phrase"), [
    ("ja", "日本語", "チームリーダーログイン"),
    ("ko", "한국어", "팀장 로그인"),
    ("es", "Español", "Inicio de sesión del capitán"),
    ("fr", "Français", "Connexion du capitaine"),
    ("de", "Deutsch", "Anmeldung für Teamkapitäne"),
])
def test_added_languages_translate_captain_login(client, lang, label, phrase):
    page = text(client.get(f"/login?lang={lang}"))
    assert f'<html lang="{lang}">' in page
    assert label in page and phrase in page
    assert f"lang={lang}" in page


def test_catalog_is_complete():
    root = Path(__file__).resolve().parents[1] / "app"
    required = ["zh-Hant", "en"]
    if (root / "i18n_locales.json").exists() and (root / "i18n_patterns.json").exists():
        required = [code for code in i18n.LANG_CODES if code != i18n.SOURCE_LANG]
    for source, entry in i18n.CATALOG.items():
        assert isinstance(entry, dict), source
        for code in required:
            assert isinstance(entry.get(code), str) and entry[code].strip(), (code, source)
    assert set(i18n.JS_KEYS) <= set(i18n.CATALOG)
    for pattern, translations in i18n.PATTERNS:
        assert isinstance(translations, dict)
        for code in required:
            assert isinstance(translations.get(code), str) and translations[code].strip(), (code, pattern.pattern)
