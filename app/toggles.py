"""Captain feature switches and the site-wide maintenance mode (总管理员 only).

Everything lives in the existing ``app_settings`` key/value table:

* ``feature.<key>``        "1" (on, default) / "0" (off) – one row per captain feature
* ``maintenance.enabled``  "1" / "0" (default)
* ``maintenance.message``  text shown on the 503 page

The switches only ever apply to regular users (队长 / 特批账号); staff are never affected.
Enforcement is server-side in ``enforce_request()`` (a ``before_request`` hook) plus
``api_feature_error()`` for the bearer-token API.
"""

from flask import flash, g, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, logout_user

from app import db
from app.i18n import t

FEATURE_DISABLED_MESSAGE = "该功能已被管理员暂时关闭"
DEFAULT_MAINTENANCE_MESSAGE = "网站正在升级，请稍后再访问"
MAINTENANCE_MESSAGE_MAX = 200
MAINTENANCE_RETRY_AFTER = 600  # seconds

FEATURE_PREFIX = "feature."
MAINT_ENABLED_KEY = "maintenance.enabled"
MAINT_MESSAGE_KEY = "maintenance.message"

# (key, label, description) – the order is the order on the 功能开关 page.
FEATURES = (
    ("captain_login", "队长登录", "队长/特批账号从登录页登录；关闭后已登录的队长也会被退出。"),
    ("forgot_password", "忘记密码自助重置", "登录页的“忘记密码”，通过队员信息核验后重置密码。"),
    ("change_password", "修改密码", "登录后主动修改密码（首次登录的强制改密不受影响）。"),
    ("booking_create", "新建测试预约", "提交新的实验室测试预约（含 API 创建预约、冲突检测）。"),
    ("booking_edit", "修改预约", "修改尚未开始的预约申请。"),
    ("booking_cancel", "取消预约", "取消自己的预约申请（含 API 取消）。"),
    ("records", "我的预约 / 申请记录", "查看个人的预约与申请记录（含 API 查询自己的预约）。"),
    ("schedule", "占用视图", "查看实验室各时段占用情况。"),
    ("checkin", "二维码签到", "扫描实验室二维码签到，以及工作台上的签到卡片。"),
    ("announcements", "查看公告", "公告列表、公告详情和强制弹窗公告。"),
    ("tool_borrow", "工具借用登记", "查看自己的工具借用并登记新的借用。"),
    ("tool_return", "工具归还", "为借用中的工具办理归还。"),
    ("material_claim", "队长材料领取", "比赛前队长拍照领取本队的免费材料。"),
    ("material_purchase", "材料购买", "队长选购材料，拍照并通过邮箱验证码确认（含重新发送验证码）。"),
)
FEATURE_KEYS = tuple(key for key, _label, _desc in FEATURES)
FEATURE_LABELS = {key: label for key, label, _desc in FEATURES}

# Web endpoints of each feature (checked for logged-in regular users).
ENDPOINT_FEATURES = {
    "bookings.create_booking": "booking_create",
    "bookings.edit_booking": "booking_edit",
    "bookings.cancel_booking": "booking_cancel",
    "bookings.my_bookings": "records",
    "records.index": "records",
    "bookings.schedule": "schedule",
    "checkin.scan": "checkin",
    "checkin.confirm": "checkin",
    "announcements.list_announcements": "announcements",
    "announcements.detail": "announcements",
    "announcements.acknowledge": "announcements",
    "tools.my_loans": "tool_borrow",
    "tools.create_loan": "tool_borrow",
    "tools.return_loan": "tool_return",
    "materials.claim": "material_claim",
    "materials.buy": "material_purchase",
    "materials.verify": "material_purchase",
    "materials.resend": "material_purchase",
    "materials.cancel_pending": "material_purchase",
    "auth.change_password": "change_password",
}

# Bearer-token API endpoints, checked when the acting user (X-Student-Id) is a regular user.
API_ENDPOINT_FEATURES = {
    "api.create_booking": "booking_create",
    "api.check_conflicts": "booking_create",
    "api.cancel_booking": "booking_cancel",
    "api.list_bookings": "records",
    "api.get_booking": "records",
}

# Reachable by everyone while maintenance mode is on, so the 总管理员 can log in and switch it off.
MAINTENANCE_EXEMPT_ENDPOINTS = {"static", "main.health", "api.health", "auth.admin_login", "auth.admin_login_legacy"}


# ------------------------------------------------------------------ storage

def ensure_toggle_settings() -> None:
    """Additive migration: insert the default rows that are missing (never changes existing ones)."""
    from app.models import AppSetting

    defaults = {FEATURE_PREFIX + key: "1" for key in FEATURE_KEYS}
    defaults[MAINT_ENABLED_KEY] = "0"
    defaults[MAINT_MESSAGE_KEY] = DEFAULT_MAINTENANCE_MESSAGE
    existing = {row.key for row in AppSetting.query.filter(AppSetting.key.in_(list(defaults))).all()}
    missing = [AppSetting(key=key, value=value) for key, value in defaults.items() if key not in existing]
    if missing:
        db.session.add_all(missing)
        db.session.commit()


def _settings() -> dict:
    """All toggle rows, read once per request."""
    cached = g.get("_toggle_settings")
    if cached is None:
        from app.models import AppSetting

        rows = AppSetting.query.filter(
            AppSetting.key.like(FEATURE_PREFIX + "%") | AppSetting.key.like("maintenance.%")
        ).all()
        cached = {row.key: row.value for row in rows}
        g._toggle_settings = cached
    return cached


def _set(key: str, value: str) -> None:
    from app.models import AppSetting

    row = db.session.get(AppSetting, key)
    if row is None:
        db.session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()
    g.pop("_toggle_settings", None)


def feature_enabled(key: str) -> bool:
    return _settings().get(FEATURE_PREFIX + key, "1") != "0"


def feature_states() -> dict:
    return {key: feature_enabled(key) for key in FEATURE_KEYS}


def set_feature(key: str, enabled: bool) -> None:
    if key not in FEATURE_LABELS:
        raise KeyError(key)
    _set(FEATURE_PREFIX + key, "1" if enabled else "0")


def maintenance_enabled() -> bool:
    return _settings().get(MAINT_ENABLED_KEY, "0") == "1"


def maintenance_message() -> str:
    return (_settings().get(MAINT_MESSAGE_KEY) or "").strip() or DEFAULT_MAINTENANCE_MESSAGE


def set_maintenance(enabled: bool) -> None:
    _set(MAINT_ENABLED_KEY, "1" if enabled else "0")


def set_maintenance_message(message: str) -> None:
    _set(MAINT_MESSAGE_KEY, message)


def captain_feature_on(key: str) -> bool:
    """Template helper: False only when the current user is a regular user and the feature is off."""
    if not getattr(current_user, "is_authenticated", False) or not current_user.is_regular_user:
        return True
    return feature_enabled(key)


# ------------------------------------------------------------------ enforcement

def _is_superadmin() -> bool:
    return bool(getattr(current_user, "is_authenticated", False) and current_user.is_superadmin)


def _wants_json() -> bool:
    return (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
    )


def _maintenance_response():
    message = maintenance_message()
    if message == DEFAULT_MAINTENANCE_MESSAGE:
        message = t(message)
    if _wants_json():
        response = jsonify(ok=False, error=message, code="maintenance")
    else:
        response = make_response(render_template("maintenance.html", message=message))
    response.status_code = 503
    response.headers["Retry-After"] = str(MAINTENANCE_RETRY_AFTER)
    return response


def _feature_disabled_response():
    if _wants_json():
        return jsonify(ok=False, kind="error", code="feature_disabled", title=t(FEATURE_DISABLED_MESSAGE),
                       error=t(FEATURE_DISABLED_MESSAGE), message=t(FEATURE_DISABLED_MESSAGE)), 403
    return render_template("error.html", message=FEATURE_DISABLED_MESSAGE + "。"), 403


def enforce_request():
    """before_request hook: maintenance mode first, then the captain feature switches."""
    endpoint = request.endpoint or ""
    if endpoint == "static" or request.path.startswith("/static/"):
        return None

    if maintenance_enabled() and not _is_superadmin():
        if endpoint not in MAINTENANCE_EXEMPT_ENDPOINTS:
            return _maintenance_response()
        if endpoint == "auth.admin_login" and current_user.is_authenticated:
            # A leftover captain/admin session must not hide the login form from the 总管理员.
            logout_user()
        return None

    if endpoint == "auth.forgot_password":
        if not feature_enabled("forgot_password") and not getattr(current_user, "is_staff", False):
            return _feature_disabled_response()
        return None

    if not current_user.is_authenticated or not current_user.is_regular_user:
        return None

    if not feature_enabled("captain_login") and endpoint != "auth.logout":
        logout_user()
        if _wants_json():
            return _feature_disabled_response()
        flash("队长登录功能已被管理员暂时关闭，你已退出登录。", "error")
        return redirect(url_for("auth.login"))

    key = ENDPOINT_FEATURES.get(endpoint)
    if key is None or feature_enabled(key):
        return None
    if key == "change_password" and current_user.must_change_password:
        return None  # the forced first-login change must stay possible
    return _feature_disabled_response()


def api_feature_error(user):
    """For app.routes.api: the disabled-feature message if this regular user's API call is switched off."""
    if user is None or not user.is_regular_user:
        return None
    if not feature_enabled("captain_login"):
        return FEATURE_DISABLED_MESSAGE
    key = API_ENDPOINT_FEATURES.get(request.endpoint or "")
    if key and not feature_enabled(key):
        return FEATURE_DISABLED_MESSAGE
    return None
