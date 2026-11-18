"""Passive security monitoring: record suspicious requests, never probe back.

Detection is request-local. Auto-ban only uses the existing blacklist and
never targets the current superadmin session or the default localhost whitelist.
Geographic labels come solely from Cloudflare request headers.
"""

from __future__ import annotations

import re
import json
from datetime import timedelta
from threading import Lock
from urllib.parse import unquote

from flask import Request, current_app, has_app_context, has_request_context, request
from flask_login import current_user

from app import db
from app.activity import client_ip, current_device_id, current_user_agent, redact, redact_url
from app.ratelimit import SlidingWindowLimiter
from app.utils import local_now

EVENT_LABELS = {
    "brute_force": "暴力破解",
    "crawl": "高频爬取",
    "path_scan": "路径扫描",
    "injection": "注入特征",
    "xss": "XSS 特征",
    "traversal": "路径穿越",
    "forbidden": "越权访问",
    "csrf": "CSRF 校验失败",
    "rate_limit": "频率超限",
    "honeypot": "蜜罐访问",
}

SEVERITY_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高危",
    "critical": "严重",
}

HONEYPOT_PREFIXES = ("/_security_honeypot",)

SCAN_PATHS = {
    "/.env",
    "/.env.local",
    "/.env.production",
    "/.git",
    "/.git/config",
    "/.git/HEAD",
    "/.gitignore",
    "/.aws",
    "/.aws/credentials",
    "/wp-admin",
    "/wp-login.php",
    "/xmlrpc.php",
    "/phpmyadmin",
    "/phpmyadmin/",
    "/phpmyadmin/index.php",
    "/adminer.php",
    "/vendor/phpunit",
    "/.htaccess",
    "/config.php",
    "/backup.sql",
    "/admin-backup",
    "/admin-backup/",
    "/server-status",
    "/actuator/env",
    "/wp-config.php",
}

INJECTION_RE = re.compile(
    r"(union\s+select|\bor\s+1=1\b|'[\s]*or[\s]*'|xp_cmdshell|/etc/passwd|information_schema)",
    re.I,
)
XSS_RE = re.compile(
    r"(<\s*script|javascript:|onerror\s*=|onload\s*=|document\.cookie|<\s*img[^>]+on\w+)",
    re.I,
)
TRAVERSAL_RE = re.compile(r"(\.\./|\.\.\\|%2e%2e%2f|%2e%2e%5c)", re.I)
CRAWLER_UA_RE = re.compile(
    r"(python-requests|curl/|scrapy|wget/|httpclient|go-http-client|libwww-perl|httpie|aiohttp|headlesschrome|headless)",
    re.I,
)
SENSITIVE_KEYS = re.compile(
    r"(password|passwd|pwd|token|secret|auth_code|authorization|api_key|csrf_token|cipher|^code$|verification_code|验证码)",
    re.I,
)

_DEFAULT_LOCAL_WHITELIST = frozenset({"127.0.0.1", "::1", "localhost"})

# Recent superadmin session IPs/devices, so unauthenticated noise on the same
# address cannot auto-ban the administrator out.
_superadmin_seen: dict[str, float] = {}
_seen_lock = Lock()


def default_thresholds() -> dict:
    return {
        "brute_force_fails": 8,
        "brute_force_window": 600,
        "crawl_requests": 80,
        "crawl_window": 60,
        "scan_404s": 12,
        "scan_404_window": 300,
        "injection_strikes": 3,
        "auto_ban_seconds": 30 * 60,
        "api_rate": 60,
        "api_window": 60,
    }


def thresholds() -> dict:
    values = default_thresholds()
    if not has_app_context():
        return values
    for key, fallback in values.items():
        cfg_key = "SECURITY_" + key.upper()
        raw = current_app.config.get(cfg_key)
        if raw is None:
            continue
        try:
            values[key] = int(raw)
        except (TypeError, ValueError):
            values[key] = fallback
    return values


def _limiter(name: str) -> SlidingWindowLimiter:
    ext = current_app.extensions.setdefault("security_limiters", {})
    if name not in ext:
        ext[name] = SlidingWindowLimiter()
    return ext[name]


def remember_superadmin_session(ip: str | None, device_id: str | None) -> None:
    now = local_now().timestamp()
    with _seen_lock:
        # drop stale (> 12h)
        stale = [k for k, ts in _superadmin_seen.items() if now - ts > 12 * 3600]
        for k in stale:
            _superadmin_seen.pop(k, None)
        if ip:
            _superadmin_seen[f"ip:{ip}"] = now
        if device_id:
            _superadmin_seen[f"device:{device_id}"] = now


def _is_recent_superadmin(kind: str, value: str) -> bool:
    with _seen_lock:
        return f"{kind}:{value}" in _superadmin_seen


def is_default_whitelisted_ip(ip: str | None) -> bool:
    return (ip or "") in _DEFAULT_LOCAL_WHITELIST


def is_whitelisted(ip: str | None = None, device_id: str | None = None) -> bool:
    from app.models import SecurityWhitelist

    ip = ip or ""
    device_id = device_id or ""
    if is_default_whitelisted_ip(ip):
        return True
    q = SecurityWhitelist.query
    if ip and q.filter_by(kind="ip", value=ip).first():
        return True
    if device_id and q.filter_by(kind="device", value=device_id).first():
        return True
    return False


def _cf_meta(req: Request) -> tuple[str | None, str | None]:
    from app.activity import is_trusted_proxy
    if not is_trusted_proxy(req.remote_addr):
        return None, None
    country = (req.headers.get("CF-IPCountry") or "").strip().upper() or None
    if country in {"XX", "T1"}:  # CF unknown / tor
        pass
    parts = []
    for header in ("CF-IPCountry", "CF-IPCity", "CF-Region", "CF-RAY", "CF-Visitor"):
        val = (req.headers.get(header) or "").strip()
        if val:
            parts.append(f"{header}={val[:80]}")
    return country, ("; ".join(parts)[:400] if parts else None)


def _summarize_params(req: Request) -> str | None:
    items = []
    try:
        source = {}
        source.update(req.args.to_dict(flat=True))
        if req.method in {"POST", "PUT", "PATCH"} and req.mimetype in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
            "application/json",
        }:
            if req.is_json:
                data = req.get_json(silent=True) or {}
                if isinstance(data, dict):
                    source.update({str(k): v for k, v in list(data.items())[:30]})
            else:
                source.update(req.form.to_dict(flat=True))
        redacted = redact(source)
        for key, val in list(redacted.items())[:20]:
            if SENSITIVE_KEYS.search(str(key)):
                items.append(f"{key}=[redacted]")
            else:
                items.append(f"{key}={str(val)[:80]}")
    except Exception:
        return None
    text = "&".join(items)
    return text[:400] if text else None


def _inspect_text(*parts: str) -> str | None:
    blob = " ".join(unquote(p or "") for p in parts)
    if not blob.strip():
        return None
    if INJECTION_RE.search(blob):
        return "injection"
    if XSS_RE.search(blob):
        return "xss"
    if TRAVERSAL_RE.search(blob):
        return "traversal"
    return None


def _path_kind(path: str) -> str | None:
    lowered = (path or "").rstrip("/") or "/"
    if any(path.startswith(prefix) for prefix in HONEYPOT_PREFIXES):
        return "honeypot"
    if path in SCAN_PATHS or lowered in SCAN_PATHS:
        return "path_scan"
    # prefix matches like /.git/objects/...
    if path.startswith("/.git/") or path.startswith("/phpmyadmin/") or path.startswith("/wp-admin/"):
        return "path_scan"
    if path.startswith("/.") and path not in {"/favicon.ico"}:
        # hidden/dot paths often used in scans
        return "path_scan"
    return None


def record_security_event(
    event_type: str,
    *,
    severity: str,
    detail: str | None = None,
    req: Request | None = None,
    auto_ban: bool = False,
) -> object | None:
    from app.models import SecurityEvent

    if not has_request_context() and req is None:
        return None
    req = req or request
    ip = client_ip(req) or None
    device = current_device_id() or None
    country, cf_headers = _cf_meta(req)
    user_id = None
    student_id = None
    try:
        if current_user.is_authenticated:
            user_id = current_user.id
            student_id = current_user.student_id
    except Exception:
        pass

    now = local_now()
    window_start = now - timedelta(hours=24)
    prior = (
        SecurityEvent.query.filter(
            SecurityEvent.ip == ip,
            SecurityEvent.event_type == event_type,
            SecurityEvent.created_at >= window_start,
        ).count()
        if ip
        else 0
    )
    event = SecurityEvent(
        created_at=now,
        event_type=event_type,
        severity=severity,
        ip=ip,
        country=country,
        cf_headers=cf_headers,
        user_agent=(current_user_agent() or None),
        device_id=device,
        user_id=user_id,
        student_id=student_id,
        method=req.method,
        path=(req.path or "")[:300],
        query_summary=_summarize_params(req),
        referer=redact_url(req.headers.get("Referer")),
        detail=(detail or "")[:400] or None,
        count=prior + 1,
        auto_blocked=False,
    )
    db.session.add(event)
    db.session.commit()

    if auto_ban:
        event.auto_blocked = _maybe_auto_ban(event)
        if event.auto_blocked:
            db.session.commit()
    return event


def _maybe_auto_ban(event) -> bool:
    from app.models import Blacklist

    seconds = thresholds()["auto_ban_seconds"]
    ip = event.ip or ""
    device = event.device_id or ""
    blocked = False

    def _protect(kind: str, value: str) -> bool:
        if not value:
            return True
        if kind == "ip" and is_default_whitelisted_ip(value):
            return True
        if is_whitelisted(ip=value if kind == "ip" else None, device_id=value if kind == "device" else None):
            return True
        if _is_recent_superadmin(kind, value):
            return True
        try:
            if current_user.is_authenticated and current_user.is_superadmin:
                return True
        except Exception:
            pass
        return False

    expires = local_now() + timedelta(seconds=seconds)
    reason = f"安全防护自动临时拉黑（{EVENT_LABELS.get(event.event_type, event.event_type)}）"

    for kind, value in (("ip", ip), ("device", device)):
        if _protect(kind, value):
            continue
        existing = (
            Blacklist.query.filter_by(kind=kind, value=value, active=True)
            .order_by(Blacklist.id.desc())
            .first()
        )
        if existing and existing.is_in_effect():
            blocked = True
            continue
        row = Blacklist(
            created_by_id=None,
            kind=kind,
            value=value,
            reason=reason[:200],
            expires_at=expires,
            active=True,
        )
        db.session.add(row)
        blocked = True
    return blocked


def inspect_request():
    """before_request hook. Returns a response to short-circuit, or None."""
    if not has_request_context():
        return None
    path = request.path or ""
    if path.startswith("/static/") or path in {"/favicon.ico"}:
        return None

    ip = client_ip()
    device = current_device_id()
    try:
        if current_user.is_authenticated and current_user.is_superadmin:
            remember_superadmin_session(ip, device)
    except Exception:
        pass

    kind = _path_kind(path)
    if kind == "honeypot":
        record_security_event("honeypot", severity="critical", detail="访问了 robots.txt 中的蜜罐路径。", auto_ban=True)
        from flask import render_template
        from app.models import Blacklist
        # If just banned, subsequent middleware would catch it; still show 404
        return render_template("error.html", message="页面不存在。"), 404
    if kind == "path_scan":
        record_security_event("path_scan", severity="high", detail=f"请求了常见扫描路径 {path}。", auto_ban=True)
        from flask import render_template
        return render_template("error.html", message="页面不存在。"), 404

    signature = _inspect_text(path, request.query_string.decode("utf-8", "replace"))
    # Avoid reading form here for every GET; for POST inspect non-sensitive values later in after? do GET/query now.
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            if request.form:
                bits = []
                for key, val in request.form.items():
                    if SENSITIVE_KEYS.search(str(key)):
                        continue
                    bits.append(str(val)[:200])
                signature = signature or _inspect_text(*bits)
        except Exception:
            pass
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            signature = signature or _inspect_text(json.dumps(redact(data), ensure_ascii=False))
    if signature:
        event = record_security_event(
            signature,
            severity="high",
            detail=f"请求中出现{EVENT_LABELS.get(signature, signature)}。",
            auto_ban=False,
        )
        if event and event.count >= thresholds()["injection_strikes"]:
            event.auto_blocked = _maybe_auto_ban(event)
            if event.auto_blocked:
                db.session.commit()
        # do not block the response for first hits; auto-ban applies next request

    # Frequency / crawler — record when over threshold, never probe
    if path not in {"/health"}:
        crawl_ok = _limiter("crawl").allow(f"crawl:{ip}", thresholds()["crawl_requests"], thresholds()["crawl_window"])
        ua = current_user_agent()
        crawler = bool(CRAWLER_UA_RE.search(ua or ""))
        if (not crawl_ok) or crawler:
            # For crawler UA, only record once per window unless also over rate
            if not crawl_ok or crawler:
                if not is_default_whitelisted_ip(ip):
                    record_security_event(
                        "crawl",
                        severity="medium" if crawl_ok else "high",
                        detail="检测到爬虫特征 User-Agent。" if crawler and crawl_ok else "同一 IP/设备请求频率偏高。",
                        auto_ban=not crawl_ok and not is_whitelisted(ip, device),
                    )

    if path.startswith("/api/"):
        api_ok = _limiter("api").allow(f"api:{ip}", thresholds()["api_rate"], thresholds()["api_window"])
        if not api_ok and not is_default_whitelisted_ip(ip):
            record_security_event("rate_limit", severity="medium", detail="API 请求超过频率限制。", auto_ban=False)
            from flask import jsonify
            return jsonify(ok=False, error="请求过于频繁，请稍后再试。"), 429
    return None


def inspect_response(response):
    """after_request: count 404s, record 403 / already handled CSRF via errorhandler."""
    if not has_request_context():
        return response
    path = request.path or ""
    if path.startswith("/static/") or path in {"/favicon.ico", "/health", "/robots.txt"}:
        return response
    ip = client_ip()
    status = getattr(response, "status_code", 200) or 200
    if status == 404 and _path_kind(path) is None:
        ok = _limiter("404").allow(f"404:{ip}", thresholds()["scan_404s"], thresholds()["scan_404_window"])
        if not ok and not is_default_whitelisted_ip(ip):
            record_security_event(
                "path_scan",
                severity="high",
                detail="短时间内出现大量 404 响应。",
                auto_ban=True,
            )
    if status == 403 and path not in {"/banned"} and not path.startswith("/static/"):
        # Skip the banned page itself
        if request.endpoint != "security.dashboard":
            record_security_event("forbidden", severity="medium", detail="返回 403。", auto_ban=False)
    return response


def record_csrf_failure() -> None:
    record_security_event("csrf", severity="medium", detail="CSRF 校验失败。", auto_ban=False)


def record_brute_force(*, student_id: str | None = None, success: bool) -> None:
    if success:
        return
    ip = client_ip() or "unknown"
    lim = _limiter("brute")
    ok = lim.allow(f"brute:{ip}", thresholds()["brute_force_fails"], thresholds()["brute_force_window"])
    detail = "登录失败。"
    if student_id:
        detail = "登录失败。"
    record_security_event(
        "brute_force",
        severity="medium" if ok else "high",
        detail=detail,
        auto_ban=not ok and not is_default_whitelisted_ip(ip),
    )


def unread_high_count() -> int:
    from app.models import SecurityEvent

    return (
        SecurityEvent.query.filter(
            SecurityEvent.severity.in_(("high", "critical")),
            SecurityEvent.read_at.is_(None),
        ).count()
    )


def mark_high_events_read() -> None:
    from app.models import SecurityEvent

    now = local_now()
    SecurityEvent.query.filter(
        SecurityEvent.severity.in_(("high", "critical")),
        SecurityEvent.read_at.is_(None),
    ).update({"read_at": now}, synchronize_session=False)
    db.session.commit()


def clear_security_events() -> int:
    from app.models import SecurityEvent

    count = SecurityEvent.query.delete()
    db.session.commit()
    return count
