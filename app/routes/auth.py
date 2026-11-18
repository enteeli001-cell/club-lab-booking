from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app import db
from app.activity import client_ip, record_audit, record_login
from app.security import record_brute_force
from app.models import User
from app.utils import (
    PASSWORD_RULES,
    STAFF_PASSWORD_RULES,
    mask_person_name,
    safe_local_url,
    validate_new_password,
    validate_new_staff_password,
    validate_new_superadmin_password,
    find_staff_by_password,
)

from app.i18n import t

auth_bp = Blueprint("auth", __name__)

CAPTAIN_HINT = "仅允许本人登录和预约，请勿代他人操作。"


# Pages that must never be a post-login destination (loops or one-shot actions).
_NEXT_BLOCKED_PREFIXES = ("/login", "/admin/login", "/logout", "/password", "/forgot-password", "/static/", "/api/")


def safe_next_target(value):
    """Return a local in-app path that is safe to resume after login / password change."""
    value = (value or "").strip()
    if not safe_local_url(value):
        return None
    if value.startswith(_NEXT_BLOCKED_PREFIXES):
        return None
    return value


def _safe_next():
    return safe_next_target(request.form.get("next")) or safe_next_target(request.args.get("next"))


def _password_change_url(next_url=None):
    return url_for("auth.change_password", next=next_url) if next_url else url_for("auth.change_password")


def _next_context(next_url):
    """Short label for "where you will land after this step" shown on login / password pages."""
    if not next_url:
        return None
    if next_url.startswith("/checkin/"):
        return {"kind": "checkin", "label": "扫码签到页面"}
    return {"kind": "page", "label": "刚才访问的页面"}


def _client_ip() -> str:
    return client_ip() or "unknown"


def _record_login_result(*, user=None, student_id=None, success=False, reason=None):
    record_login(user=user, student_id=student_id, success=success, reason=reason)
    try:
        record_brute_force(student_id=student_id, success=success)
    except Exception:
        pass


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next()
    if current_user.is_authenticated:
        if current_user.must_change_password and current_user.role != "admin":
            return redirect(_password_change_url(next_url))
        return redirect(next_url or url_for("main.index"))

    def page(**extra):
        extra.setdefault("show_forgot_prompt", False)
        extra.setdefault("forgot_student_id", "")
        return render_template("login.html", next_url=next_url, next_context=_next_context(next_url), **extra)

    from app.toggles import FEATURE_DISABLED_MESSAGE, feature_enabled

    if not feature_enabled("captain_login"):
        # This entrance is for captains only (staff use /admin/login), so the whole form is closed.
        flash(f"队长登录：{FEATURE_DISABLED_MESSAGE}。", "error")
        return page(captain_login_off=True), (403 if request.method == "POST" else 200)

    if request.method == "POST":
        student_id = (request.form.get("student_id") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(student_id=student_id).first()
        failures = current_app.extensions["captain_login_failures"]
        fail_key = f"{_client_ip()}:{student_id}"
        if user and user.check_password(password):
            if user.is_staff:
                _record_login_result(user=user, student_id=student_id, success=False, reason="请走管理员入口")
                flash("该账号是管理员或总管理员，请使用下方的「管理员登录」入口。", "error")
                return page()
            if not user.is_regular_user:
                _record_login_result(user=user, student_id=student_id, success=False, reason="账号类型不可从此入口登录")
                flash("学号或密码不正确。", "error")
                return page()
            if not user.is_account_active:
                reason = user.inactive_reason() or "该账号当前不可用。"
                _record_login_result(user=user, student_id=student_id, success=False, reason=reason)
                flash(reason, "error")
                return page()
            failures.reset(fail_key)
            if user.is_captain and password == user.student_id:
                user.must_change_password = True
                db.session.commit()
            login_user(user)
            _record_login_result(user=user, student_id=student_id, success=True)
            if user.must_change_password:
                flash("首次使用初始密码登录，请先修改密码后再使用系统。请按页面规则设置新密码。", "warning")
                return redirect(_password_change_url(next_url))
            return redirect(next_url or url_for("main.index"))
        _record_login_result(user=user, student_id=student_id, success=False, reason="学号或密码不正确")
        flash("学号或密码不正确。", "error")
        show_forgot = False
        if user is not None and user.is_captain:
            count = failures.fail(fail_key)
            threshold = current_app.config.get("CAPTAIN_LOGIN_FORGOT_AFTER", 3)
            show_forgot = count >= threshold
        return page(
            show_forgot_prompt=show_forgot,
            forgot_student_id=student_id if show_forgot else "",
        )

    return page()


@auth_bp.route("/login/captain-name")
def captain_name():
    limiter = current_app.extensions["name_lookup_limiter"]
    limit = current_app.config.get("CAPTAIN_NAME_LOOKUP_LIMIT", 20)
    window = current_app.config.get("CAPTAIN_NAME_LOOKUP_WINDOW", 60)
    if not limiter.allow(f"captain-name:{_client_ip()}", limit, window):
        return jsonify(ok=False, found=False, error=t("查询过于频繁，请稍后再试。")), 429

    student_id = (request.args.get("student_id") or "").strip()
    if not student_id:
        return jsonify(ok=True, found=False, error=t("未找到该队长账号"))

    user = User.query.filter_by(student_id=student_id).first()
    if user is None or not user.is_regular_user:
        return jsonify(ok=True, found=False, error=t("未找到该队长账号"))

    greeting = f"{mask_person_name(user.name)} 同学，您好"
    return jsonify(ok=True, found=True, greeting=t(greeting), hint=t(CAPTAIN_HINT))


@auth_bp.route("/login/admin")
def admin_login_legacy():
    return redirect(url_for("auth.admin_login", **request.args))


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    next_url = _safe_next()
    if current_user.is_authenticated:
        if current_user.must_change_password and current_user.role != "admin":
            return redirect(_password_change_url(next_url))
        return redirect(next_url or url_for("main.index"))

    guard = current_app.extensions["admin_login_guard"]
    limiter = current_app.extensions["admin_login_limiter"]
    ip = _client_ip()
    max_failures = current_app.config.get("ADMIN_LOGIN_MAX_FAILURES", 5)
    lock_seconds = current_app.config.get("ADMIN_LOGIN_LOCK_SECONDS", 60)
    rate_limit = current_app.config.get("ADMIN_LOGIN_RATE_LIMIT", 20)
    rate_window = current_app.config.get("ADMIN_LOGIN_RATE_WINDOW", 60)

    remaining = guard.locked_remaining(ip)
    if remaining:
        flash(f"登录失败次数过多，请 {remaining} 秒后再试。", "error")
        return render_template("login_admin.html", lock_remaining=remaining), 429

    if request.method == "POST":
        if not limiter.allow(f"admin-login:{ip}", rate_limit, rate_window):
            flash("尝试过于频繁，请稍后再试。", "error")
            return render_template("login_admin.html", lock_remaining=0), 429
        password = request.form.get("password") or ""
        user = find_staff_by_password(password)
        if user is None:
            _record_login_result(success=False, reason="密码不正确")
            locked = guard.register_failure(ip, max_failures, lock_seconds)
            if locked:
                flash(f"登录失败次数过多，请 {locked} 秒后再试。", "error")
                return render_template("login_admin.html", lock_remaining=locked), 429
            flash("密码不正确。", "error")
            return render_template("login_admin.html", lock_remaining=0)
        if not user.is_account_active:
            _record_login_result(user=user, student_id=user.student_id, success=False, reason=user.inactive_reason())
            flash(user.inactive_reason() or "该账号当前不可用。", "error")
            return render_template("login_admin.html", lock_remaining=0)
        from app.toggles import maintenance_enabled

        if maintenance_enabled() and not user.is_superadmin:
            _record_login_result(user=user, student_id=user.student_id, success=False, reason="维护模式仅总管理员可登录")
            flash("网站正在升级维护，暂时只有总管理员可以登录。", "error")
            return render_template("login_admin.html", lock_remaining=0), 503
        guard.register_success(ip)
        login_user(user)
        _record_login_result(user=user, student_id=user.student_id, success=True)
        if user.must_change_password and user.role != "admin":
            flash("首次使用下发密码登录，请先按规则修改密码后再使用系统。", "warning")
            return redirect(_password_change_url(next_url))
        return redirect(next_url or url_for("main.index"))

    return render_template("login_admin.html", lock_remaining=0)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("已退出登录。", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if current_user.role == "admin":
        flash("管理员不能自行修改密码，如需更换请联系总管理员。", "warning")
        return redirect(url_for("main.index"))
    forced = bool(current_user.must_change_password)
    is_staff = current_user.is_staff
    next_url = _safe_next()
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if current_user.is_superadmin:
            errors = validate_new_superadmin_password(current_user, current, new, confirm)
        elif is_staff:
            errors = validate_new_staff_password(current_user, current, new, confirm)
        else:
            errors = validate_new_password(current_user, current, new, confirm)
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            current_user.set_password(new, require_change=False)
            db.session.commit()
            record_audit("password.change", summary="修改了登录密码。", reversible=False, undo_blocked_reason="密码无法撤回。")
            if next_url and next_url.startswith("/checkin/"):
                flash("密码已修改，已返回签到页面，请确认签到。", "success")
            else:
                flash("密码已修改。", "success")
            return redirect(next_url or url_for("main.index"))
    return render_template(
        "password.html",
        forced=forced,
        is_staff=is_staff,
        soft_rules=current_user.is_superadmin,
        password_rules=STAFF_PASSWORD_RULES if is_staff else PASSWORD_RULES,
        student_id=current_user.student_id,
        min_length=(12 if is_staff and not current_user.is_superadmin else (1 if current_user.is_superadmin else 6)),
        next_url=next_url,
        next_context=_next_context(next_url),
    )



def _write_reset_log(student_id: str, success: bool) -> None:
    from app.models import PasswordResetLog

    log = PasswordResetLog(ip=_client_ip(), student_id=student_id or "", success=success)
    db.session.add(log)
    db.session.commit()


def _reset_limited(student_id: str) -> bool:
    limiter = current_app.extensions["captain_reset_limiter"]
    limit = current_app.config.get("CAPTAIN_RESET_MAX_ATTEMPTS", 5)
    window = current_app.config.get("CAPTAIN_RESET_WINDOW", 3600)
    ip = _client_ip()
    # Both the student ID and the IP are capped independently.
    ok_sid = limiter.allow(f"reset-sid:{student_id}", limit, window)
    ok_ip = limiter.allow(f"reset-ip:{ip}", limit, window)
    return not (ok_sid and ok_ip)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    from app.models import TeamMember
    from app.utils import validate_new_password

    form = {
        "name": (request.values.get("name") or "").strip(),
        "student_id": (request.values.get("student_id") or "").strip(),
        "teammate_name": (request.values.get("teammate_name") or "").strip(),
        "teammate_student_id": (request.values.get("teammate_student_id") or "").strip(),
        "new_password": "",
        "confirm_password": "",
    }
    next_url = _safe_next()
    if request.method != "POST":
        return render_template("forgot_password.html", form=form, next_url=next_url)

    if _reset_limited(form["student_id"]):
        _write_reset_log(form["student_id"], False)
        flash("尝试过于频繁，请稍后再试。", "error")
        return render_template("forgot_password.html", form=form, next_url=next_url), 429

    captain = User.query.filter_by(student_id=form["student_id"]).first()
    teammate = None
    identity_ok = False
    if captain is not None and captain.is_captain and captain.name == form["name"] and form["teammate_name"] and form["teammate_student_id"]:
        teammate = TeamMember.query.filter_by(
            captain_id=captain.id,
            name=form["teammate_name"],
            student_id=form["teammate_student_id"],
        ).first()
        identity_ok = teammate is not None

    if not identity_ok:
        _write_reset_log(form["student_id"], False)
        flash("信息不匹配", "error")
        return render_template("forgot_password.html", form=form, next_url=next_url)

    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    # Reuse captain password rules; current password is not known, so check composition,
    # not-equal-student-id, and confirm only.
    errors = []
    if len(new) < 6:
        errors.append("新密码长度不能少于 6 位。")
    if not any("A" <= ch <= "Z" for ch in new):
        errors.append("新密码必须包含大写字母。")
    if not any("a" <= ch <= "z" for ch in new):
        errors.append("新密码必须包含小写字母。")
    if not any(not ("A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9") for ch in new):
        errors.append("新密码必须包含至少一个特殊符号。")
    if new == captain.student_id:
        errors.append("新密码不能与学号相同。")
    if captain.check_password(new):
        errors.append("新密码不能与旧密码相同。")
    if new != confirm:
        errors.append("两次输入的新密码不一致。")
    if errors:
        # Identity already verified; show password-rule errors only.
        for msg in errors:
            flash(msg, "error")
        form["new_password"] = ""
        form["confirm_password"] = ""
        return render_template("forgot_password.html", form=form, next_url=next_url)

    captain.set_password(new, require_change=False)
    db.session.commit()
    _write_reset_log(form["student_id"], True)
    flash("密码已重置，请使用新密码登录。", "success")
    return redirect(url_for("auth.login", next=next_url) if next_url else url_for("auth.login"))
