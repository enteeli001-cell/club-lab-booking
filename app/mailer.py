"""Mail delivery: optional SMTP, otherwise a local outbox.

The SMTP password is read only from the environment variable named by
MAIL_PASSWORD_ENV (default SMTP_AUTH_CODE). It is never copied
into files, logs, the database, or process output.
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
import uuid
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime as format_mail_date, formataddr
from pathlib import Path
from typing import Any

DEFAULT_PASSWORD_ENV = "SMTP_AUTH_CODE"
DEFAULT_DOH_URL = "https://cloudflare-dns.com/dns-query"
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8080

# RFC 2606 / 6761 reserved names — never delivered over the network.
_RESERVED_EXACT = {
    "example.com",
    "example.net",
    "example.org",
    "test",
    "invalid",
    "localhost",
}
_RESERVED_SUFFIXES = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".test",
    ".invalid",
    ".localhost",
    ".example",
)


def is_reserved_recipient(address: str) -> bool:
    text = (address or "").strip().lower()
    if "@" not in text:
        return False
    domain = text.rsplit("@", 1)[1].rstrip(".")
    if domain in _RESERVED_EXACT:
        return True
    return any(domain.endswith(suffix) for suffix in _RESERVED_SUFFIXES)


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _config_paths() -> tuple[Path, Path]:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            data = Path(current_app.config["DATA_DIR"])
            cfg = Path(current_app.config.get("MAIL_CONFIG_FILE") or (data / "mail_config"))
            outbox = Path(current_app.config.get("OUTBOX_DIR") or (data / "outbox"))
            return cfg, outbox
    except Exception:
        pass
    from app.paths import MAIL_CONFIG_FILE, OUTBOX_DIR

    return MAIL_CONFIG_FILE, OUTBOX_DIR


def _parse_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # Never load a password value from the file, even if someone added one.
        if key.upper() in {"MAIL_PASSWORD", "SMTP_PASSWORD", "PASSWORD", "SMTP_AUTH_CODE"}:
            continue
        values[key.strip()] = val.strip()
    return values


@dataclass
class MailConfig:
    host: str = ""
    port: int = 465
    username: str = ""
    sender: str = ""
    sender_name: str = ""
    use_ssl: bool = True
    use_tls: bool = False
    password_env: str = DEFAULT_PASSWORD_ENV
    send_enabled: bool = True
    proxy_host: str = DEFAULT_PROXY_HOST
    proxy_port: int = DEFAULT_PROXY_PORT
    use_proxy: bool = True
    doh_url: str = DEFAULT_DOH_URL

    @property
    def password_configured(self) -> bool:
        return bool(os.environ.get(self.password_env))

    def password(self) -> str | None:
        value = os.environ.get(self.password_env)
        return value if value else None

    @property
    def smtp_ready(self) -> bool:
        return bool(self.host and self.sender and self.username and self.password_configured)


def load_mail_config() -> MailConfig:
    cfg_path, _ = _config_paths()
    file_vals = _parse_file(cfg_path)

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            env = os.environ.get(name)
            if env is not None and env != "":
                return env
            if name in file_vals and file_vals[name] != "":
                return file_vals[name]
        return default

    port_raw = pick("MAIL_PORT", default="465")
    try:
        port = int(port_raw)
    except ValueError:
        port = 465
    proxy_port_raw = pick("MAIL_PROXY_PORT", default=str(DEFAULT_PROXY_PORT))
    try:
        proxy_port = int(proxy_port_raw)
    except ValueError:
        proxy_port = DEFAULT_PROXY_PORT

    proxy_host = pick("MAIL_PROXY_HOST")
    username = pick("MAIL_USERNAME", "MAIL_FROM")
    sender = pick("MAIL_FROM", "MAIL_USERNAME")
    return MailConfig(
        host=pick("MAIL_HOST"),
        port=port,
        username=username,
        sender=sender,
        sender_name=pick("MAIL_FROM_NAME"),
        use_ssl=_truthy(pick("MAIL_USE_SSL"), default=True),
        use_tls=_truthy(pick("MAIL_USE_TLS"), default=False),
        password_env=pick("MAIL_PASSWORD_ENV", default=DEFAULT_PASSWORD_ENV) or DEFAULT_PASSWORD_ENV,
        send_enabled=_truthy(pick("MAIL_SEND_ENABLED"), default=True),
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        doh_url=pick("MAIL_DOH_URL", default=DEFAULT_DOH_URL) or DEFAULT_DOH_URL,
        # Proxy is used only when MAIL_PROXY_HOST is set and MAIL_USE_PROXY is not false.
        use_proxy=bool(proxy_host) and _truthy(pick("MAIL_USE_PROXY"), default=True),
    )


def _safe_error(exc: BaseException, password_env: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    secret = os.environ.get(password_env) or ""
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[:300]


def resolve_host_ip(hostname: str, config: MailConfig | None = None, timeout: float = 20) -> str:
    cfg = config or load_mail_config()
    if not cfg.use_proxy:
        return socket.gethostbyname(hostname)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {
                "https": f"http://{cfg.proxy_host}:{cfg.proxy_port}",
                "http": f"http://{cfg.proxy_host}:{cfg.proxy_port}",
            }
        )
    )
    url = f"{cfg.doh_url}?name={hostname}&type=A"
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with opener.open(req, timeout=timeout) as resp:
        payload = json.load(resp)
    answers = payload.get("Answer") or []
    ips = [a["data"] for a in answers if a.get("type") == 1]
    if not ips:
        raise OSError(f"DoH 未返回 {hostname} 的 A 记录")
    return ips[0]


def open_proxied_ssl_socket(
    hostname: str,
    port: int,
    real_ip: str,
    config: MailConfig | None = None,
    timeout: float = 30,
) -> ssl.SSLSocket:
    cfg = config or load_mail_config()
    sock = socket.create_connection((cfg.proxy_host, cfg.proxy_port), timeout=timeout)
    try:
        sock.sendall(f"CONNECT {real_ip}:{port} HTTP/1.1\r\nHost: {real_ip}:{port}\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("代理在 CONNECT 过程中关闭连接")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b" 200 " not in status and not status.endswith(b" 200"):
            raise OSError("代理 CONNECT 失败")
        context = ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=hostname)
    except Exception:
        sock.close()
        raise


class ProxiedSMTP_SSL(smtplib.SMTP_SSL):
    """SMTP over HTTP CONNECT, TLS SNI uses the configured hostname."""

    def __init__(self, host: str, port: int, real_ip: str, config: MailConfig, timeout: float = 30):
        self._real_ip = real_ip
        self._mail_config = config
        context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        return open_proxied_ssl_socket(
            hostname=host,
            port=port,
            real_ip=self._real_ip,
            config=self._mail_config,
            timeout=timeout or 30,
        )


def _open_direct_smtp(config: MailConfig):
    """Plain SMTP connection without any proxy (e.g. on the cloud server)."""
    context = ssl.create_default_context()
    if config.use_ssl:
        return smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context)
    client = smtplib.SMTP(config.host, config.port, timeout=30)
    try:
        client.ehlo_or_helo_if_needed()
        if config.use_tls:
            client.starttls(context=context)
            client.ehlo()
    except Exception:
        client.close()
        raise
    return client


def _open_smtp(config: MailConfig, real_ip: str | None = None):
    if not config.use_proxy:
        return _open_direct_smtp(config)
    ip = real_ip or resolve_host_ip(config.host, config)
    if config.use_ssl:
        client = ProxiedSMTP_SSL(config.host, config.port, ip, config)
        return client
    # STARTTLS path (still via CONNECT to the real IP, then upgrade).
    sock = socket.create_connection((config.proxy_host, config.proxy_port), timeout=30)
    sock.sendall(f"CONNECT {ip}:{config.port} HTTP/1.1\r\nHost: {ip}:{config.port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError("代理在 CONNECT 过程中关闭连接")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 200 " not in status and not status.endswith(b" 200"):
        sock.close()
        raise OSError("代理 CONNECT 失败")
    client = smtplib.SMTP()
    client.sock = sock
    client.file = None
    client._host = config.host
    code, _msg = client.getreply()
    if code != 220:
        client.close()
        raise smtplib.SMTPConnectError(code, "unexpected SMTP banner")
    client.ehlo_or_helo_if_needed()
    if config.use_tls:
        context = ssl.create_default_context()
        client.starttls(context=context)
        # After STARTTLS, SNI/hostname should already be set if wrap uses server_hostname.
        # smtplib.starttls uses context.wrap_socket(self.sock, server_hostname=self._host)
        client.ehlo_or_helo_if_needed()
    return client


def smtp_login_only(config: MailConfig | None = None, real_ip: str | None = None) -> None:
    """Connect, log in, and QUIT. Does not send any message."""
    cfg = config or load_mail_config()
    if not cfg.host or not cfg.username:
        raise RuntimeError("SMTP 未配置主机或用户名")
    if not cfg.password_configured:
        raise RuntimeError(f"未设置环境变量 {cfg.password_env}")
    client = _open_smtp(cfg, real_ip=real_ip)
    try:
        client.login(cfg.username, cfg.password())
        try:
            client.quit()
        except Exception:
            client.close()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise


def send_via_smtp(message: EmailMessage, config: MailConfig | None = None) -> None:
    """Deliver one message over SMTP. Tests must mock this; never opens a socket under TESTING."""
    try:
        from flask import current_app, has_app_context

        if has_app_context() and current_app.config.get("TESTING"):
            raise RuntimeError("pytest 中禁止真实 SMTP 发送，请 mock send_via_smtp")
    except RuntimeError:
        raise
    except Exception:
        pass
    cfg = config or load_mail_config()
    if not cfg.smtp_ready:
        raise RuntimeError("SMTP 未完整配置")
    client = _open_smtp(cfg)
    try:
        client.login(cfg.username, cfg.password())
        client.send_message(message)
        try:
            client.quit()
        except Exception:
            client.close()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise


def _build_message(cfg: MailConfig, recipient: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    sender = cfg.sender or cfg.username or "noreply@localhost"
    if cfg.sender_name:
        msg["From"] = formataddr((cfg.sender_name, sender))
    else:
        msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    from app.utils import aware_now

    msg["Date"] = format_mail_date(aware_now())
    msg.set_content(body)
    return msg


def _write_eml(outbox: Path, message: EmailMessage) -> str:
    outbox.mkdir(parents=True, exist_ok=True)
    from app.utils import local_now

    stamp = local_now().strftime("%Y%m%d%H%M%S")
    name = f"{stamp}_{uuid.uuid4().hex[:10]}.eml"
    path = outbox / name
    path.write_bytes(bytes(message))
    return name


def _record_log(*, recipient: str, subject: str, body: str, status: str, error: str | None, filename: str | None, booking_id: int | None):
    from app import db
    from app.models import MailLog

    log = MailLog(
        recipient=recipient,
        subject=subject,
        body=body,
        status=status,
        error=error,
        filename=filename,
        booking_id=booking_id,
    )
    db.session.add(log)
    db.session.commit()
    return log


def send_mail(recipient: str, subject: str, body: str, *, booking_id: int | None = None, log_body: str | None = None) -> Any:
    """Deliver or archive a message. Never raises to the caller after logging.

    ``log_body`` (optional) replaces the body stored in the mail log, e.g. to keep a
    one-time verification code out of the admin-visible mail records.
    """
    from app.models import MailLog

    cfg = load_mail_config()
    _, outbox = _config_paths()
    recipient = (recipient or "").strip()
    subject = (subject or "").strip() or "（无主题）"
    body = body or ""

    if not recipient:
        return _record_log(
            recipient="",
            subject=subject,
            body=log_body if log_body is not None else body,
            status="failed",
            error="未填写队长邮箱",
            filename=None,
            booking_id=booking_id,
        )

    message = _build_message(cfg, recipient, subject, body)
    archived_message = _build_message(cfg, recipient, subject, log_body) if log_body is not None else message
    reserved = is_reserved_recipient(recipient)
    use_real = cfg.send_enabled and cfg.smtp_ready and not reserved

    if not use_real:
        filename = _write_eml(outbox, archived_message)
        return _record_log(
            recipient=recipient,
            subject=subject,
            body=log_body if log_body is not None else body,
            status="local",
            error=None,
            filename=filename,
            booking_id=booking_id,
        )

    try:
        send_via_smtp(message, cfg)
        return _record_log(
            recipient=recipient,
            subject=subject,
            body=log_body if log_body is not None else body,
            status="sent",
            error=None,
            filename=None,
            booking_id=booking_id,
        )
    except Exception as exc:
        filename = _write_eml(outbox, archived_message)
        return _record_log(
            recipient=recipient,
            subject=subject,
            body=log_body if log_body is not None else body,
            status="failed",
            error=_safe_error(exc, cfg.password_env),
            filename=filename,
            booking_id=booking_id,
        )


def send_reject_notice(booking, reason: str) -> Any:
    recipient = (booking.captain_email or "").strip()
    subject = "【社团实验室预约管理系统】测试申请未通过审核"
    day = booking.date.isoformat() if booking.date else ""
    slot = booking.time_range_label if hasattr(booking, "time_range_label") else ""
    lab = booking.lab.name if booking.lab else ""
    name = booking.user.name if booking.user else "队长"
    body = (
        f"{name}，你好：\n\n"
        f"你提交的测试申请未通过审核。\n"
        f"实验室：{lab}\n"
        f"时段：{day} {slot}\n"
        f"拒绝理由：{reason}\n\n"
        f"如有疑问请联系管理员。\n"
        f"（此邮件由系统自动发送）\n"
    )
    return send_mail(recipient, subject, body, booking_id=booking.id)


def check_smtp_login(*, max_attempts: int = 2) -> dict:
    """Run connect+login+QUIT with per-step retries. Same step failing twice stops."""
    cfg = load_mail_config()
    report = {"ok": False, "steps": [], "failed_step": None, "error": None}
    real_ip = None

    def attempt(step_name: str, fn):
        last_error = None
        for n in range(1, max_attempts + 1):
            try:
                result = fn()
                report["steps"].append({"step": step_name, "ok": True, "attempt": n})
                return result
            except Exception as exc:
                last_error = _safe_error(exc, cfg.password_env)
                report["steps"].append({"step": step_name, "ok": False, "attempt": n, "error": last_error})
                if n >= max_attempts:
                    report["failed_step"] = step_name
                    report["error"] = last_error
                    raise
        raise RuntimeError(last_error or step_name)

    try:
        if not cfg.host or not cfg.username:
            raise RuntimeError("SMTP 未配置 MAIL_HOST / MAIL_USERNAME")
        if not cfg.password_configured:
            raise RuntimeError(f"未设置环境变量 {cfg.password_env}")

        def do_resolve():
            return resolve_host_ip(cfg.host, cfg)

        real_ip = attempt("doh_resolve", do_resolve)
        report["ip_resolved"] = True

        def do_login():
            smtp_login_only(cfg, real_ip=real_ip)

        attempt("smtp_login", do_login)
        report["ok"] = True
        return report
    except Exception as exc:
        if report["error"] is None:
            report["error"] = _safe_error(exc, cfg.password_env)
        report["ok"] = False
        return report
