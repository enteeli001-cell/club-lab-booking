import logging
import os
import secrets as std_secrets
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

from app.paths import (
    ADMIN_PASSWORD_FILE,
    API_TOKEN_FILE,
    DATA_DIR,
    DB_PATH,
    LOG_FILE,
    LOG_DIR,
    SECRET_KEY_FILE,
    ensure_runtime_dirs,
)
from app.secretstore import read_or_create_secret, write_secret_file

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

ROOT_DIR = Path(__file__).resolve().parent.parent

login_manager.login_view = "auth.login"
login_manager.login_message = "请先登录"
login_manager.login_message_category = "warning"


def _load_or_create_secret_key(secret_key_file: Path) -> str:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    return read_or_create_secret(secret_key_file, nbytes=32)


def _setup_logging(app: Flask, log_file: Path) -> None:
    if app.config.get("TESTING"):
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    app.logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == str(log_file) for h in app.logger.handlers):
        app.logger.addHandler(handler)
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == str(log_file) for h in werkzeug_logger.handlers):
        werkzeug_logger.addHandler(handler)
        werkzeug_logger.setLevel(logging.INFO)


def create_app(test_config=None):
    test_config = dict(test_config or {})
    data_dir = Path(test_config["DATA_DIR"]) if test_config.get("DATA_DIR") else DATA_DIR
    log_dir = Path(test_config["LOG_DIR"]) if test_config.get("LOG_DIR") else LOG_DIR
    db_path = Path(test_config["DB_PATH"]) if test_config.get("DB_PATH") else (data_dir / DB_PATH.name)
    secret_key_file = data_dir / SECRET_KEY_FILE.name
    admin_password_file = data_dir / ADMIN_PASSWORD_FILE.name
    api_token_file = data_dir / API_TOKEN_FILE.name
    log_file = log_dir / LOG_FILE.name

    ensure_runtime_dirs(data_dir, log_dir)

    app = Flask(
        __name__,
        instance_path=str(data_dir),
        instance_relative_config=True,
    )
    testing = bool(test_config.get("TESTING"))
    app.config.from_mapping(
        SECRET_KEY=test_config.get("SECRET_KEY") if testing else _load_or_create_secret_key(secret_key_file),
        SQLALCHEMY_DATABASE_URI=test_config.get("SQLALCHEMY_DATABASE_URI")
        or ("sqlite:///" + str(db_path)),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        WTF_CSRF_TIME_LIMIT=None,
        APP_TIMEZONE=test_config.get("APP_TIMEZONE") or os.environ.get("APP_TIMEZONE") or "Asia/Shanghai",
        CAPTAIN_NAME_LOOKUP_LIMIT=20,
        CAPTAIN_NAME_LOOKUP_WINDOW=60,
        ADMIN_LOGIN_MAX_FAILURES=5,
        ADMIN_LOGIN_LOCK_SECONDS=60,
        ADMIN_LOGIN_RATE_LIMIT=20,
        ADMIN_LOGIN_RATE_WINDOW=60,
        CAPTAIN_LOGIN_FORGOT_AFTER=3,
        CAPTAIN_RESET_MAX_ATTEMPTS=5,
        CAPTAIN_RESET_WINDOW=3600,
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        CHECKIN_EARLY_MINUTES=int(os.environ.get("CHECKIN_EARLY_MINUTES", "15") or 15),
        NO_SHOW_ALERT_THRESHOLD=int(os.environ.get("NO_SHOW_ALERT_THRESHOLD", "3") or 3),
        REMINDER_MINUTES_BEFORE=int(os.environ.get("REMINDER_MINUTES_BEFORE", "30") or 30),
        REMINDER_MAX_RETRIES=2,
        SCAN_INTERVAL_SECONDS=60,
        CHECKIN_RATE_LIMIT=30,
        CHECKIN_RATE_WINDOW=60,
        # Seconds between automatic page refreshes (app.js); 0 switches the feature off.
        # 队长/特批账号首次登录须完善姓名、手机号并验证邮箱（pytest 的通用夹具会关闭它）。
        PROFILE_COMPLETION_REQUIRED=True,
        AUTO_REFRESH_SECONDS=int(os.environ.get("AUTO_REFRESH_SECONDS", "30") or 0),
        DATA_DIR=str(data_dir),
        LOG_DIR=str(log_dir),
        UPLOAD_DIR=str(data_dir / "uploads"),
        OUTBOX_DIR=str(data_dir / "outbox"),
        MAIL_CONFIG_FILE=str(data_dir / "mail_config"),
        ADMIN_PASSWORD_FILE=str(admin_password_file),
        API_TOKEN_FILE=str(api_token_file),
        DEBUG=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=False,
        SESSION_COOKIE_NAME="lab_session",
    )

    if test_config:
        app.config.update(test_config)

    # Production pages must never render a traceback. Pytest keeps PROPAGATE_EXCEPTIONS.
    if not testing:
        app.config["DEBUG"] = False
        app.config["PROPAGATE_EXCEPTIONS"] = False

    from flask.sessions import SecureCookieSessionInterface

    class _ProxyAwareSessionInterface(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):
            from flask import has_request_context
            if has_request_context():
                from app.activity import request_is_https
                return request_is_https()
            return bool(app.config.get("SESSION_COOKIE_SECURE"))

        def get_cookie_httponly(self, app):
            return True

        def get_cookie_samesite(self, app):
            return app.config.get("SESSION_COOKIE_SAMESITE") or "Lax"

    app.session_interface = _ProxyAwareSessionInterface()

    if not app.config.get("API_TOKEN"):
        if testing:
            app.config["API_TOKEN"] = "test-api-token"
        else:
            token = read_or_create_secret(api_token_file, nbytes=32)
            app.config["API_TOKEN"] = token
            app.logger.info("API 令牌文件：%s", api_token_file)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    _setup_logging(app, log_file)
    from app.ratelimit import ConsecutiveCounter, LoginGuard, SlidingWindowLimiter
    app.extensions["name_lookup_limiter"] = SlidingWindowLimiter()
    app.extensions["admin_login_guard"] = LoginGuard()
    app.extensions["admin_login_limiter"] = SlidingWindowLimiter()
    app.extensions["captain_login_failures"] = ConsecutiveCounter()
    app.extensions["captain_reset_limiter"] = SlidingWindowLimiter()
    app.extensions["checkin_limiter"] = SlidingWindowLimiter()
    app.extensions["material_code_limiter"] = SlidingWindowLimiter()

    from zoneinfo import ZoneInfo

    ZoneInfo(app.config["APP_TIMEZONE"])  # fail at start-up, not at the first request, on a bad zone name

    from app import models  # noqa: F401
    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    from app.routes.api import api_bp
    from app.routes.audit import audit_bp
    from app.routes.tools import tools_bp
    from app.routes.duties import duties_bp
    from app.routes.records import records_bp
    from app.routes.mail import mail_bp
    from app.routes.people import people_bp
    from app.routes.managers import managers_bp
    from app.routes.activity import activity_bp
    from app.routes.security import security_bp
    from app.routes.auth import auth_bp
    from app.routes.bookings import bookings_bp
    from app.routes.checkin import checkin_bp
    from app.routes.noshow import noshow_bp
    from app.routes.announcements import announcements_bp
    from app.routes.labs import labs_bp
    from app.routes.main import main_bp
    from app.routes.review import review_bp
    from app.routes.stats import stats_bp
    from app.routes.users import users_bp
    from app.routes.toggles import toggles_bp
    from app.routes.materials import materials_bp
    from app.routes.profile import profile_bp
    from app.routes.simulation import simulation_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(labs_bp)
    app.register_blueprint(bookings_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(duties_bp)
    app.register_blueprint(records_bp)
    app.register_blueprint(mail_bp)
    app.register_blueprint(people_bp)
    app.register_blueprint(managers_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(security_bp)
    app.register_blueprint(checkin_bp)
    app.register_blueprint(noshow_bp)
    app.register_blueprint(announcements_bp)
    app.register_blueprint(toggles_bp)
    app.register_blueprint(materials_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(simulation_bp)
    from app import i18n
    app.jinja_env.globals["t"] = i18n.t
    app.jinja_env.filters["t"] = i18n.t
    app.jinja_env.filters["t"] = i18n.t
    app.jinja_env.globals["language_url"] = i18n.language_url
    app.jinja_env.globals["weekday_label"] = i18n.weekday_label
    app.jinja_env.globals["calendar_day"] = i18n.calendar_day
    app.jinja_env.globals["duration_label"] = i18n.duration_label
    app.after_request(i18n.remember_choice)
    from app.toggles import captain_feature_on
    app.jinja_env.globals["feature_on"] = captain_feature_on
    from app.routes.profile import needs_profile
    app.jinja_env.globals["profile_required"] = needs_profile
    csrf.exempt(api_bp)
    from app.checkin import captain_checkin_cards
    app.jinja_env.globals["checkin_cards"] = captain_checkin_cards
    from app.assets import asset_url
    app.jinja_env.globals["asset_url"] = asset_url

    @app.context_processor
    def inject_globals():
        from app.utils import timezone_label, timezone_name, today
        from app.notices import (
            BOOKING_CHANGE_LOCKED_NOTICE,
            BOOKING_SUBMITTED_NOTICE,
            BOOKING_SUBMITTED_TITLE,
            LOGIN_NOTICE_TITLE,
            LOGIN_NOTICES,
            REJECT_EMAIL_WARNING,
            CHECKIN_PLEDGE,
            CHECKIN_PLEDGE_CHECKBOX,
            CHECKIN_PLEDGE_TITLE,
            PRIVACY_NOTICE,
        )

        unread = 0
        try:
            from flask_login import current_user as _cu
            if getattr(_cu, "is_authenticated", False) and getattr(_cu, "is_superadmin", False):
                from app.security import unread_high_count
                unread = unread_high_count()
        except Exception:
            unread = 0
        return {
            "system_name": "社团实验室预约管理系统",
            "login_notice_title": LOGIN_NOTICE_TITLE,
            "login_notices": LOGIN_NOTICES,
            "booking_submitted_title": BOOKING_SUBMITTED_TITLE,
            "booking_submitted_notice": BOOKING_SUBMITTED_NOTICE,
            "booking_change_locked_notice": BOOKING_CHANGE_LOCKED_NOTICE,
            "reject_email_warning": REJECT_EMAIL_WARNING,
            "privacy_notice": PRIVACY_NOTICE,
            "checkin_pledge_title": CHECKIN_PLEDGE_TITLE,
            "checkin_pledge": CHECKIN_PLEDGE,
            "checkin_pledge_checkbox": CHECKIN_PLEDGE_CHECKBOX,
            "auto_refresh_seconds": max(0, int(current_app.config.get("AUTO_REFRESH_SECONDS") or 0)),
            "tz_label": timezone_label(),
            "tz_name": timezone_name(),
            "app_today": today().isoformat(),
            "unread_high_alerts": unread,
            "noshow_alert": _pending_noshow_alert(),
            "maintenance_on": _maintenance_on(),
            "languages": i18n.LANGUAGES,
            "current_lang": i18n.current_lang(),
            "html_lang": i18n.html_lang(),
            "i18n_js": i18n.js_catalog(),
            **_announcement_context(),
        }




    @app.after_request
    def set_device_cookie_and_security(response):
        from app.activity import ensure_device_cookie
        from app.security import inspect_response
        from app.assets import compress_response, static_cache_headers
        response = ensure_device_cookie(response)
        if request.endpoint == "static":
            response = static_cache_headers(response)
        elif request.endpoint in ("tools.media", "materials.media") and response.status_code == 200:
            # Uploaded photos have random, never-reused names and the route still checks
            # permission on every miss; let only this user's browser keep them for a day.
            response.headers["Cache-Control"] = "private, max-age=86400"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        try:
            response = inspect_response(response)
        except Exception:
            current_app.logger.exception("安全检测（响应）失败")
        try:
            response = compress_response(response)
        except Exception:
            current_app.logger.exception("响应压缩失败")
        return response

    @app.before_request
    def enforce_simulation_boundary():
        # Apply before password/profile redirects: the internal module must return
        # 403 to every non-superadmin, including incomplete regular accounts.
        if request.path == "/simulation" or request.path.startswith("/simulation/"):
            from flask_login import current_user
            from flask import abort
            if not current_user.is_authenticated or not current_user.is_superadmin:
                abort(403)

    @app.before_request
    def inspect_security_request():
        from app.security import inspect_request
        try:
            return inspect_request()
        except Exception:
            current_app.logger.exception("安全检测（请求）失败")
            return None

    @app.before_request
    def enforce_blacklist_and_account():
        from flask_login import current_user, logout_user
        from app.activity import active_blacklist_hits, blacklist_exempt_path

        if blacklist_exempt_path(request.path):
            return None
        hits = active_blacklist_hits()
        if hits:
            hit = hits[0]
            expires = hit.expires_at.strftime("%Y-%m-%d %H:%M") if hit.expires_at else None
            return render_template("banned.html", reason=hit.reason, expires_at=expires), 403
        if current_user.is_authenticated and not current_user.is_account_active:
            reason = current_user.inactive_reason() or "该账号当前不可用。"
            logout_user()
            flash(reason, "error")
            return redirect(url_for("auth.login"))
        return None

    @app.before_request
    def enforce_toggles():
        from app.toggles import enforce_request

        return enforce_request()

    @app.before_request
    def enforce_password_change():
        from flask_login import current_user

        if not current_user.is_authenticated:
            return None
        if not current_user.must_change_password:
            return None
        if getattr(current_user, "role", None) == "admin":
            return None
        endpoint = request.endpoint or ""
        if endpoint in {"auth.change_password", "auth.logout", "static"}:
            return None
        if request.path.startswith("/static/"):
            return None
        if request.path.startswith("/api/"):
            return None
        flash("首次登录请先修改初始密码后再使用系统。", "warning")
        # Keep where the user was heading (e.g. the scanned check-in QR code) so the
        # password page can send them back afterwards.
        from app.routes.auth import safe_next_target
        target = safe_next_target(request.full_path.rstrip("?")) if request.method == "GET" else None
        return redirect(url_for("auth.change_password", next=target) if target else url_for("auth.change_password"))

    @app.before_request
    def enforce_profile_completion():
        from app.routes.profile import profile_gate

        return profile_gate()

    def _wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(CSRFError)
    def handle_csrf(_error):
        try:
            from app.security import record_csrf_failure
            record_csrf_failure()
        except Exception:
            pass
        if _wants_json():
            return jsonify(ok=False, error="CSRF 校验失败"), 400
        return render_template("error.html", message="表单已过期，请刷新后重试。"), 400

    @app.errorhandler(400)
    def handle_400(error):
        message = getattr(error, "description", None) or "请求无效。"
        if _wants_json():
            return jsonify(ok=False, error=i18n.t(message)), 400
        return render_template("error.html", message=message), 400

    @app.errorhandler(401)
    def handle_401(error):
        message = getattr(error, "description", None) or "未授权。"
        if _wants_json():
            return jsonify(ok=False, error=i18n.t(message)), 401
        return render_template("error.html", message=message), 401

    @app.errorhandler(403)
    def handle_403(error):
        message = getattr(error, "description", None) or "没有权限执行该操作。"
        if _wants_json():
            return jsonify(ok=False, error=i18n.t(message)), 403
        return render_template("error.html", message=message), 403

    @app.errorhandler(404)
    def handle_404(_error):
        if _wants_json():
            return jsonify(ok=False, error=i18n.t("接口不存在。")), 404
        return render_template("error.html", message="页面不存在。"), 404

    @app.errorhandler(413)
    def handle_413(_error):
        message = "上传文件过大。"
        if _wants_json():
            return jsonify(ok=False, error=i18n.t(message)), 413
        return render_template("error.html", message=message), 413

    @app.errorhandler(500)
    def handle_500(_error):
        try:
            app.logger.exception("未捕获的服务器错误")
        except Exception:
            pass
        message = "服务器繁忙，请稍后重试。"
        if _wants_json():
            return jsonify(ok=False, error=i18n.t(message)), 500
        return render_template("error.html", message=message), 500

    from app.utils import format_datetime as _format_datetime
    app.jinja_env.filters["fmt_dt"] = _format_datetime
    from app.models import ROLE_LABELS
    app.jinja_env.filters["role_label"] = lambda role: i18n.t(ROLE_LABELS.get(role, role or "—"))

    with app.app_context():
        from app.migrations import migration_guard
        with migration_guard():
            _enable_sqlite_wal()
            db.create_all()
            _ensure_user_schema()
            _ensure_checkin_schema()
            _ensure_announcement_schema()
            _ensure_materials_schema()
            from app.migrations import ensure_simulation_schema
            ensure_simulation_schema()
            from app.toggles import ensure_toggle_settings
            ensure_toggle_settings()
            _ensure_performance_indexes()
            _migrate_roles()
            if not app.config.get("TESTING"):
                _ensure_initial_admin(Path(app.config["ADMIN_PASSWORD_FILE"]))

    return app


def _ensure_initial_admin(password_file: Path):
    from app.models import User

    if User.query.count() > 0:
        return

    from app.utils import generate_staff_password, staff_password_complexity_errors

    if password_file.exists():
        password = password_file.read_text(encoding="utf-8").strip()
        if not password:
            password = generate_staff_password()
            write_secret_file(password_file, password)
    else:
        password = generate_staff_password()
        write_secret_file(password_file, password)

    admin = User(student_id="admin", name="总管理员", role="superadmin")
    admin.set_password(password, require_change=False)
    db.session.add(admin)
    db.session.commit()
    print(f"已创建总管理员（账号：admin），密码已写入：{password_file}")



def _enable_sqlite_wal() -> None:
    """Turn on SQLite WAL and a busy timeout for the current engine."""
    from sqlalchemy import event, text

    engine = db.engine
    if engine.dialect.name != "sqlite":
        return

    def _on_connect(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    event.listen(engine, "connect", _on_connect)
    # Apply immediately to the already-opened connection as well.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


def _ensure_user_schema():
    """Add newly introduced columns on existing SQLite databases."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    if "must_change_password" not in columns:
        db.session.execute(
            text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()
    if "team_name" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN team_name VARCHAR(80)"))
        db.session.commit()
    if "last_phone" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN last_phone VARCHAR(32)"))
        db.session.commit()
    if "contact_phone" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN contact_phone VARCHAR(32)"))
        db.session.commit()
    if "bookings" in inspector.get_table_names():
        booking_cols = {col["name"] for col in inspector.get_columns("bookings")}
        if "captain_phone" not in booking_cols:
            db.session.execute(text("ALTER TABLE bookings ADD COLUMN captain_phone VARCHAR(32)"))
            db.session.commit()
    if "labs" in inspector.get_table_names():
        lab_cols = {col["name"] for col in inspector.get_columns("labs")}
        if "contact_name" not in lab_cols:
            db.session.execute(text("ALTER TABLE labs ADD COLUMN contact_name VARCHAR(64)"))
            db.session.commit()
        if "contact_phone" not in lab_cols:
            db.session.execute(text("ALTER TABLE labs ADD COLUMN contact_phone VARCHAR(32)"))
            db.session.commit()
    if "last_email" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN last_email VARCHAR(120)"))
        db.session.commit()
    for col, ddl in (
        ("verified_email", "VARCHAR(120)"),
        ("email_verified_at", "DATETIME"),
        ("profile_completed_at", "DATETIME"),
    ):
        if col not in columns:
            db.session.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
            db.session.commit()
    if "bookings" in inspector.get_table_names():
        booking_cols = {col["name"] for col in inspector.get_columns("bookings")}
        if "captain_email" not in booking_cols:
            db.session.execute(text("ALTER TABLE bookings ADD COLUMN captain_email VARCHAR(120)"))
            db.session.commit()
    if "tool_loan_items" in inspector.get_table_names():
        item_cols = {col["name"] for col in inspector.get_columns("tool_loan_items")}
        if "catalog_id" not in item_cols:
            db.session.execute(text("ALTER TABLE tool_loan_items ADD COLUMN catalog_id INTEGER"))
            db.session.commit()
        if "is_other" not in item_cols:
            db.session.execute(text("ALTER TABLE tool_loan_items ADD COLUMN is_other BOOLEAN NOT NULL DEFAULT 0"))
            db.session.commit()



    if "enabled" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1"))
        db.session.commit()
    if "organization" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN organization VARCHAR(120)"))
        db.session.commit()
    if "special_reason" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN special_reason VARCHAR(200)"))
        db.session.commit()
    if "valid_until" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN valid_until DATE"))
        db.session.commit()
    if "password_cipher" not in columns:
        db.session.execute(text("ALTER TABLE users ADD COLUMN password_cipher TEXT"))
        db.session.commit()
    if "team_members" in inspector.get_table_names():
        tm_cols = {col["name"] for col in inspector.get_columns("team_members")}
        if "phone" not in tm_cols:
            db.session.execute(text("ALTER TABLE team_members ADD COLUMN phone VARCHAR(32)"))
            db.session.commit()


def _pending_noshow_alert():
    """Forced reminder shown on every page to a captain until acknowledged."""
    try:
        from flask_login import current_user as _cu

        if not getattr(_cu, "is_authenticated", False) or not getattr(_cu, "is_regular_user", False):
            return None
        from app.routes.profile import needs_profile
        if needs_profile(_cu) or _cu.must_change_password:
            return None
        if request.endpoint in {"noshow.acknowledge", "static"}:
            return None
        from app.noshow import pending_alert_context

        return pending_alert_context(_cu)
    except Exception:
        current_app.logger.exception("读取未签到提醒失败")
        return None


def _maintenance_on() -> bool:
    try:
        from app.toggles import maintenance_enabled

        return maintenance_enabled()
    except Exception:
        current_app.logger.exception("读取维护模式状态失败")
        return False


def _announcement_context() -> dict:
    """Forced announcement popup list and the unread badge for the nav (empty for anonymous users)."""
    empty = {"announcement_popups": [], "announcement_unread": 0}
    try:
        from flask_login import current_user as _cu

        if not getattr(_cu, "is_authenticated", False):
            return empty
        from app.routes.profile import needs_profile
        if needs_profile(_cu) or _cu.must_change_password:
            return empty
        if request.endpoint in {"announcements.acknowledge", "static"}:
            return empty
        from app.announcements import page_context
        from app.toggles import feature_enabled

        if getattr(_cu, "is_regular_user", False) and not feature_enabled("announcements"):
            return empty  # switched off for captains: no popup they could not dismiss, no badge
        ctx = page_context(_cu)
        return {"announcement_popups": ctx["popups"], "announcement_unread": ctx["unread"]}
    except Exception:
        current_app.logger.exception("读取公告提醒失败")
        return empty


def _ensure_announcement_schema():
    """Additive-only: announcements.publisher_name (editable 发布人) for databases created earlier."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "announcements" in inspector.get_table_names():
        cols = {col["name"] for col in inspector.get_columns("announcements")}
        if "publisher_name" not in cols:
            db.session.execute(text("ALTER TABLE announcements ADD COLUMN publisher_name VARCHAR(30)"))
            db.session.commit()


def _ensure_checkin_schema():
    """Additive-only migration for the booking check-in feature (never drops data)."""
    from sqlalchemy import inspect, text

    from app.checkin import ensure_lab_tokens

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if "labs" in tables:
        lab_cols = {col["name"] for col in inspector.get_columns("labs")}
        if "checkin_token" not in lab_cols:
            db.session.execute(text("ALTER TABLE labs ADD COLUMN checkin_token VARCHAR(64)"))
            db.session.commit()
        if "checkin_token_updated_at" not in lab_cols:
            db.session.execute(text("ALTER TABLE labs ADD COLUMN checkin_token_updated_at DATETIME"))
            db.session.commit()
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_labs_checkin_token ON labs (checkin_token)"))
        db.session.commit()
    if "labs" in tables:
        for col, ddl in (
            ("geo_lat", "FLOAT"),
            ("geo_lng", "FLOAT"),
            ("geo_radius_m", "INTEGER NOT NULL DEFAULT 30"),
            ("geo_required", "BOOLEAN NOT NULL DEFAULT 0"),
        ):
            if col not in {c["name"] for c in inspect(db.engine).get_columns("labs")}:
                db.session.execute(text(f"ALTER TABLE labs ADD COLUMN {col} {ddl}"))
                db.session.commit()
    if "bookings" in tables:
        for col, ddl in (
            ("pledge_accepted_at", "DATETIME"),
            ("checkin_lat", "FLOAT"),
            ("checkin_lng", "FLOAT"),
            ("checkin_accuracy", "FLOAT"),
            ("checkin_distance_m", "FLOAT"),
        ):
            if col not in {c["name"] for c in inspect(db.engine).get_columns("bookings")}:
                db.session.execute(text(f"ALTER TABLE bookings ADD COLUMN {col} {ddl}"))
                db.session.commit()
    if "labs" in tables:
        ensure_lab_tokens()  # ORM query: needs every Lab column to exist first
    # Bookings that ended before the check-in feature existed are never counted as no-shows.
    from app.models import AppSetting
    from app.utils import local_now

    if db.session.get(AppSetting, "checkin_enabled_at") is None:
        db.session.add(AppSetting(key="checkin_enabled_at", value=local_now().isoformat(timespec="seconds")))
        db.session.commit()
    if "bookings" in tables:
        booking_cols = {col["name"] for col in inspector.get_columns("bookings")}
        for name, ddl in (
            ("checked_in_at", "DATETIME"),
            ("checkin_ip", "VARCHAR(64)"),
            ("checkin_ua", "VARCHAR(300)"),
            ("reminder_sent_at", "DATETIME"),
            ("reminder_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in booking_cols:
                db.session.execute(text(f"ALTER TABLE bookings ADD COLUMN {name} {ddl}"))
                db.session.commit()


def _migrate_roles():
    """Merge legacy officer into admin; upgrade the bootstrap account to superadmin."""
    from app.models import User

    changed = False
    for user in User.query.filter_by(role="officer").all():
        user.role = "admin"
        changed = True
    bootstrap = User.query.filter_by(student_id="admin").first()
    if bootstrap is not None and bootstrap.role != "superadmin":
        bootstrap.role = "superadmin"
        if bootstrap.name in {"系统管理员", "管理员"}:
            bootstrap.name = "总管理员"
        changed = True
    if changed:
        db.session.commit()


# Composite indexes for the hot list/lookup queries (each checked with EXPLAIN QUERY PLAN;
# role+created_at on the log tables was tried and dropped: it defeats the LIMIT 300 scan). Additive only: CREATE INDEX IF NOT EXISTS
# never rewrites or deletes rows, and re-running it on every start is a no-op.
PERFORMANCE_INDEXES = (
    ("ix_bookings_status_created", "bookings", "status, created_at"),
    ("ix_bookings_lab_date_status", "bookings", "lab_id, date, status"),
    ("ix_bookings_user_date", "bookings", "user_id, date, start_time"),
    ("ix_tool_loans_user_borrow", "tool_loans", "user_id, borrow_at"),
    ("ix_tool_loans_borrow_at", "tool_loans", "borrow_at"),
    ("ix_security_events_sev_read", "security_events", "severity, read_at"),
    ("ix_security_events_ip_type_created", "security_events", "ip, event_type, created_at"),
    ("ix_blacklist_kind_value_active", "blacklist_entries", "kind, value, active"),
    ("ix_noshow_alerts_user_ack", "noshow_alerts", "user_id, acknowledged_at"),
    ("ix_announcements_pinned_created", "announcements", "pinned, created_at"),
    ("ix_announcements_level_created", "announcements", "level, created_at"),
)


def _ensure_performance_indexes():
    from sqlalchemy import inspect, text

    tables = set(inspect(db.engine).get_table_names())
    for name, table, columns in PERFORMANCE_INDEXES:
        if table in tables:
            db.session.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))
    db.session.commit()


def _ensure_materials_schema():
    """Add exact quantity columns and the one-active-claim guard.

    Backfill new columns only; preserve legacy columns and historical amounts.
    """
    from sqlalchemy import inspect, text
    from app.quantities import EXACT_COLUMNS

    # Additive upgrade from the deployed integer material schema. Keep old values,
    # including historical prices/totals, and backfill exact values only if absent.
    tables = set(inspect(db.engine).get_table_names())
    for table, fields in EXACT_COLUMNS.items():
        if table not in tables:
            continue
        columns = {col["name"] for col in inspect(db.engine).get_columns(table)}
        for exact, legacy in fields:
            if exact not in columns:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{exact}" TEXT'))
            db.session.execute(text(
                f'UPDATE "{table}" SET "{exact}" = CAST("{legacy}" AS TEXT) '
                f'WHERE "{exact}" IS NULL AND "{legacy}" IS NOT NULL'
            ))
    db.session.commit()

    if "material_claims" in inspect(db.engine).get_table_names():
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_material_claims_user_active "
            "ON material_claims (user_id) WHERE status = 'active'"
        ))
        db.session.commit()
