"""队长/特批账号的个人资料：首次登录必须填写姓名、手机号并通过 6 位验证码验证邮箱。

* ``profile_gate()``（before_request）：资料不完整或邮箱未验证时，把所有页面重定向到
  资料完善页；只放行资料页本身、退出登录、修改初始密码、静态文件和 API。
* 以后修改邮箱同样要重新验证；新邮箱验证完成前暂停使用系统。
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app import verification as vc
from app.activity import client_ip, record_audit
from app.mailer import send_mail
from app.models import EmailVerification, User
from app.decorators import reviewer_required
from app.transactions import begin_serialized_write
from app.utils import is_email_address, is_mobile_phone, local_now

profile_bp = Blueprint("profile", __name__)

PURPOSE = "profile-email"
CODE_MAIL_SUBJECT = "邮箱验证码"
CODE_EMAILS_PER_HOUR = 10
GATE_EXEMPT_ENDPOINTS = {
    "static",
    "profile.setup",
    "profile.verify",
    "profile.resend",
    "auth.logout",
    "auth.change_password",
    "main.health",
    "api.health",
}


def _required() -> bool:
    return bool(current_app.config.get("PROFILE_COMPLETION_REQUIRED", True))


def needs_profile(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_regular_user
        and _required()
        and not user.profile_complete
    )


def profile_gate():
    """before_request: force regular users to complete their profile first."""
    if not needs_profile(current_user):
        return None
    endpoint = request.endpoint or ""
    if endpoint in GATE_EXEMPT_ENDPOINTS or request.path.startswith("/static/") or request.path.startswith("/api/"):
        return None
    if current_user.must_change_password and current_user.role != "admin":
        return None  # the forced password change runs first (its own hook redirects)
    message = "请先完善个人信息并验证邮箱后再使用系统。"
    if request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json":
        from app.i18n import t

        return jsonify(ok=False, code="profile_required", error=t(message)), 403
    from app.routes.auth import safe_next_target

    target = safe_next_target(request.full_path.rstrip("?")) if request.method == "GET" else None
    if target in ("/", None) or target.startswith("/profile"):
        target = None
    flash(message, "warning")
    return redirect(url_for("profile.setup", next=target) if target else url_for("profile.setup"))


def _next_url() -> str | None:
    from app.routes.auth import safe_next_target

    value = request.values.get("next")
    target = safe_next_target(value) if value else None
    if target and target.startswith("/profile"):
        return None
    return target


def _pending(user_id: int) -> EmailVerification | None:
    return (
        EmailVerification.query.filter_by(user_id=user_id, status="pending")
        .order_by(EmailVerification.id.desc())
        .first()
    )


def _limiter_allows(user_id: int) -> bool:
    if not vc.delivery_allowed(user_id):
        return False
    limiter = current_app.extensions.get("material_code_limiter")
    return True if limiter is None else limiter.allow(f"profile-code:{user_id}", CODE_EMAILS_PER_HOUR, 3600)


def _issue_code(row: EmailVerification):
    from app.i18n import t

    code = vc.generate_code()
    now = local_now()
    row.code_hash = vc.code_digest(PURPOSE, row.id, code, row.email.lower())
    row.expires_at = vc.expires_from_now()
    row.sent_at = now
    row.send_count = int(row.send_count or 0) + 1
    row.attempts = 0
    db.session.commit()
    source = (
        "{name} 同学：\n\n"
        "你正在社团实验室预约管理系统绑定邮箱，验证码为：{code}\n"
        "验证码 {minutes} 分钟内有效，请勿告诉他人。\n\n"
        "如果不是你本人操作，请忽略本邮件。\n"
    )
    minutes = vc.CODE_TTL_SECONDS // 60
    return send_mail(
        row.email,
        t(CODE_MAIL_SUBJECT),
        t(source, name=current_user.name, code=code, minutes=minutes),
        log_body=t(source, name=current_user.name, code="******", minutes=minutes),
    )


def _finish(next_url):
    flash("个人信息已完善，邮箱验证成功。", "success")
    return redirect(next_url or url_for("main.index"))


@profile_bp.route("/profile", methods=["GET", "POST"])
@login_required
def setup():
    if not current_user.is_regular_user:
        return redirect(url_for("main.index"))
    next_url = _next_url()
    pending = _pending(current_user.id)
    form = {
        "name": current_user.name or "",
        "phone": current_user.last_phone or "",
        "email": (pending.email if pending else "") or current_user.verified_email or current_user.last_email or "",
    }
    if request.method == "POST":
        form = {key: (request.form.get(key) or "").strip() for key in ("name", "phone", "email")}
        errors = []
        if not form["name"]:
            errors.append("请填写姓名。")
        elif len(form["name"]) > 64:
            errors.append("姓名不能超过 64 字。")
        if not is_mobile_phone(form["phone"]):
            errors.append("请填写正确的 11 位手机号（1 开头的大陆手机号）。")
        if not is_email_address(form["email"]) or len(form["email"]) > 120:
            errors.append("请填写正确的邮箱地址。")
        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("profile/setup.html", form=form, pending=pending, next_url=next_url)
        begin_serialized_write()
        db.session.refresh(current_user._get_current_object())
        pending = _pending(current_user.id)
        before = {"name": current_user.name, "phone": current_user.last_phone}
        current_user.name = form["name"]
        current_user.last_phone = form["phone"]
        same_email = current_user.email_verified and current_user.verified_email.lower() == form["email"].lower()
        if same_email:
            if pending:
                pending.status = "cancelled"
                pending.code_hash = None
            if current_user.profile_completed_at is None:
                current_user.profile_completed_at = local_now()
            db.session.commit()
            if before != {"name": current_user.name, "phone": current_user.last_phone}:
                record_audit("profile.update", summary="更新了个人信息（姓名/手机号）。", target_type="user",
                             target_id=current_user.id, payload={"before": before, "after": {"name": current_user.name, "phone": current_user.last_phone}})
            flash("个人信息已保存。", "success")
            return redirect(next_url or url_for("main.index"))
        if not _limiter_allows(current_user.id):
            db.session.commit()
            flash("验证码发送过于频繁，请稍后再试。", "error")
            return render_template("profile/setup.html", form=form, pending=pending, next_url=next_url)
        if pending and pending.email.lower() == form["email"].lower() and vc.resend_wait(pending.sent_at) > 0:
            db.session.commit()  # keep the code that was just sent
            flash("验证码已发送，请查收邮件后输入。", "info")
            return redirect(url_for("profile.verify", next=next_url) if next_url else url_for("profile.verify"))
        if pending:
            pending.status = "cancelled"
            pending.code_hash = None
        # Changing the address invalidates its previous verification and purchase codes.
        current_user.email_verified_at = None
        current_user.profile_completed_at = None
        from app.materials import cancel_other_pending
        cancel_other_pending(current_user.id)
        row = EmailVerification(user_id=current_user.id, email=form["email"], ip=client_ip() or None, created_at=local_now())
        db.session.add(row)
        db.session.flush()
        log = _issue_code(row)
        if getattr(log, "status", "") == "failed":
            flash("验证码邮件发送失败，请检查邮箱地址后重试，或联系管理员。", "error")
        else:
            flash(f"验证码已发送到 {vc.mask_email(row.email)}，5 分钟内有效。", "success")
        return redirect(url_for("profile.verify", next=next_url) if next_url else url_for("profile.verify"))
    return render_template("profile/setup.html", form=form, pending=pending, next_url=next_url)


@profile_bp.route("/profile/verify", methods=["GET", "POST"])
@login_required
def verify():
    if not current_user.is_regular_user:
        return redirect(url_for("main.index"))
    next_url = _next_url()
    if request.method == "POST":
        begin_serialized_write()
        db.session.refresh(current_user._get_current_object())
    row = _pending(current_user.id)
    if row is None:
        db.session.rollback()
        flash("请先填写邮箱并获取验证码。", "info")
        return redirect(url_for("profile.setup", next=next_url) if next_url else url_for("profile.setup"))
    back = url_for("profile.verify", next=next_url) if next_url else url_for("profile.verify")
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not vc.CODE_RE.fullmatch(code):
            db.session.rollback()
            flash("请输入邮件中的 6 位数字验证码。", "error")
            return redirect(back)
        if not row.code_hash or int(row.attempts or 0) >= vc.CODE_MAX_ATTEMPTS:
            db.session.rollback()
            flash("验证码错误次数过多，请点“重新发送验证码”。", "error")
            return redirect(back)
        if vc.is_expired(row.expires_at):
            db.session.rollback()
            flash("验证码已过期（有效期 5 分钟），请点“重新发送验证码”。", "error")
            return redirect(back)
        if not vc.digest_matches(row.code_hash, PURPOSE, row.id, code, row.email.lower()):
            row.attempts = int(row.attempts or 0) + 1
            left = vc.CODE_MAX_ATTEMPTS - row.attempts
            if left <= 0:
                row.code_hash = None
            db.session.commit()
            flash("验证码错误次数过多，该验证码已失效，请点“重新发送验证码”。" if left <= 0 else f"验证码错误，还可以再试 {left} 次。", "error")
            return redirect(back)
        now = local_now()
        old_email = current_user.verified_email
        row.status = "verified"
        row.verified_at = now
        row.code_hash = None
        current_user.verified_email = row.email
        current_user.email_verified_at = now
        current_user.last_email = row.email
        if current_user.profile_completed_at is None:
            current_user.profile_completed_at = now
        db.session.commit()
        record_audit("profile.email_verified", summary=f"验证并绑定邮箱 {vc.mask_email(row.email)}。", target_type="user",
                     target_id=current_user.id, payload={"old": vc.mask_email(old_email), "new": vc.mask_email(row.email)})
        return _finish(next_url)
    return render_template(
        "profile/verify.html",
        row=row,
        masked_email=vc.mask_email(row.email),
        seconds_left=vc.seconds_left(row.expires_at),
        resend_wait=vc.resend_wait(row.sent_at),
        attempts_left=max(0, vc.CODE_MAX_ATTEMPTS - int(row.attempts or 0)) if row.code_hash else 0,
        sends_left=max(0, vc.CODE_MAX_SENDS - int(row.send_count or 0)),
        next_url=next_url,
    )


@profile_bp.route("/profile/resend", methods=["POST"])
@login_required
def resend():
    if not current_user.is_regular_user:
        return redirect(url_for("main.index"))
    next_url = _next_url()
    back = url_for("profile.verify", next=next_url) if next_url else url_for("profile.verify")
    begin_serialized_write()
    row = _pending(current_user.id)
    if row is None:
        db.session.rollback()
        return redirect(url_for("profile.setup"))
    wait = vc.resend_wait(row.sent_at)
    if wait > 0:
        db.session.rollback()
        flash(f"请 {wait} 秒后再重新发送。", "error")
    elif int(row.send_count or 0) >= vc.CODE_MAX_SENDS:
        db.session.rollback()
        flash("验证码发送次数已用完，请返回修改邮箱后重新获取。", "error")
    elif not _limiter_allows(current_user.id):
        db.session.rollback()
        flash("验证码发送过于频繁，请稍后再试。", "error")
    else:
        log = _issue_code(row)
        if getattr(log, "status", "") == "failed":
            flash("验证码邮件发送失败，请稍后重试，或联系管理员。", "error")
        else:
            flash(f"新的验证码已发送到 {vc.mask_email(row.email)}，旧验证码作废。", "success")
    return redirect(back)


@profile_bp.route("/profile/people")
@reviewer_required
def people():
    users = User.query.filter(User.role.in_(("member", "approved_member", "special_guest"))).order_by(User.team_name, User.student_id).all()
    return render_template("profile/people.html", users=users)
