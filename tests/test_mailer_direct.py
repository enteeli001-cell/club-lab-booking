"""Mail proxy is optional: used only when MAIL_PROXY_HOST is configured."""
from unittest.mock import MagicMock, patch

import pytest

from app import mailer


def _write_cfg(path, extra=""):
    path.write_text(
        "MAIL_HOST=smtp.example.com\nMAIL_PORT=465\nMAIL_USERNAME=sender@example.com\n"
        "MAIL_FROM=sender@example.com\nMAIL_USE_SSL=true\n" + extra,
        encoding="utf-8",
    )


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    for key in ("MAIL_PROXY_HOST", "MAIL_PROXY_PORT", "MAIL_USE_PROXY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "mail_config"
    monkeypatch.setattr(mailer, "_config_paths", lambda: (path, tmp_path / "outbox"))
    return path


def test_no_proxy_host_means_direct(cfg_file):
    _write_cfg(cfg_file)
    assert mailer.load_mail_config().use_proxy is False


def test_empty_proxy_host_means_direct(cfg_file):
    _write_cfg(cfg_file, "MAIL_PROXY_HOST=\nMAIL_PROXY_PORT=8080\n")
    assert mailer.load_mail_config().use_proxy is False


def test_configured_proxy_is_used(cfg_file):
    _write_cfg(cfg_file, "MAIL_PROXY_HOST=127.0.0.1\nMAIL_PROXY_PORT=8080\n")
    cfg = mailer.load_mail_config()
    assert cfg.use_proxy is True
    assert (cfg.proxy_host, cfg.proxy_port) == ("127.0.0.1", 8080)


def test_use_proxy_false_overrides_host(cfg_file):
    _write_cfg(cfg_file, "MAIL_PROXY_HOST=127.0.0.1\nMAIL_USE_PROXY=false\n")
    assert mailer.load_mail_config().use_proxy is False


def test_direct_ssl_does_not_touch_proxy(cfg_file):
    _write_cfg(cfg_file, "MAIL_USE_PROXY=false\n")
    cfg = mailer.load_mail_config()
    fake = MagicMock()
    with patch.object(mailer.smtplib, "SMTP_SSL", return_value=fake) as ssl_cls, \
         patch.object(mailer.socket, "create_connection", side_effect=AssertionError("proxy used")), \
         patch.object(mailer, "resolve_host_ip", side_effect=AssertionError("DoH used")):
        client = mailer._open_smtp(cfg)
    assert client is fake
    args, kwargs = ssl_cls.call_args
    assert args[:2] == ("smtp.example.com", 465)


def test_direct_starttls(cfg_file):
    _write_cfg(cfg_file, "MAIL_USE_SSL=false\nMAIL_USE_TLS=true\nMAIL_PORT=587\n")
    cfg = mailer.load_mail_config()
    assert cfg.use_ssl is False and cfg.use_tls is True and cfg.port == 587
    fake = MagicMock()
    with patch.object(mailer.smtplib, "SMTP", return_value=fake) as smtp_cls:
        client = mailer._open_smtp(cfg)
    assert client is fake
    assert smtp_cls.call_args[0][:2] == ("smtp.example.com", 587)
    fake.starttls.assert_called_once()


def test_direct_resolve_uses_system_dns(cfg_file):
    _write_cfg(cfg_file)
    cfg = mailer.load_mail_config()
    with patch.object(mailer.socket, "gethostbyname", return_value="1.2.3.4") as gh, \
         patch.object(mailer.urllib.request, "build_opener", side_effect=AssertionError("DoH used")):
        assert mailer.resolve_host_ip("smtp.example.com", cfg) == "1.2.3.4"
    gh.assert_called_once_with("smtp.example.com")


def test_proxy_path_unchanged(cfg_file):
    _write_cfg(cfg_file, "MAIL_PROXY_HOST=127.0.0.1\nMAIL_PROXY_PORT=8080\n")
    cfg = mailer.load_mail_config()
    with patch.object(mailer, "ProxiedSMTP_SSL", return_value="proxied") as proxied, \
         patch.object(mailer.smtplib, "SMTP_SSL", side_effect=AssertionError("direct used")):
        assert mailer._open_smtp(cfg, real_ip="9.9.9.9") == "proxied"
    proxied.assert_called_once()
