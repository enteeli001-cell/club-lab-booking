"""Superadmin management of regular admin accounts."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.pwstore import decrypt_password
from flask_login import current_user

from app import db
from app.account_history import detach_account_history
from app.activity import record_audit
from app.decorators import superadmin_required
from app.models import Booking, DutyShift, User
from app.utils import generate_unique_staff_password, is_mobile_phone, staff_password_is_unique

managers_bp = Blueprint("managers", __name__, url_prefix="/managers")


def _admins():
    return User.query.filter_by(role="admin").order_by(User.student_id.asc()).all()


@managers_bp.route("/")
@superadmin_required
def index():
    issued = request.args.get("issued")
    # password is flashed via session once
    issued_data = None
    if request.args.get("show") == "1":
        issued_data = request.args  # not used; template gets from flask session via context
    return render_template("managers/index.html", admins=_admins(), issued=None)


@managers_bp.route("/new", methods=["POST"])
@superadmin_required
def create_admin():
    name = (request.form.get("name") or "").strip()
    student_id = (request.form.get("student_id") or "").strip()
    contact = (request.form.get("contact_phone") or "").strip()
    errors = []
    if not name:
        errors.append("请填写使用人姓名。")
    if not student_id:
        errors.append("请填写学号。")
    elif User.query.filter_by(student_id=student_id).first():
        errors.append("该学号已存在。")
    if contact and not is_mobile_phone(contact):
        errors.append("联系方式须为 11 位手机号。")
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("managers.index"))
    password = generate_unique_staff_password()
    user = User(student_id=student_id, name=name, role="admin", contact_phone=contact or None, enabled=True)
    user.set_password(password, require_change=True)
    db.session.add(user)
    db.session.commit()
    record_audit(
        "admin.create",
        summary=f"新增管理员 {name}（{student_id}），已生成一次性密码。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="删除管理员请在管理员管理中操作。",
    )
    return render_template(
        "managers/index.html",
        admins=_admins(),
        issued={"user": user, "password": password, "title": "新管理员密码（仅显示一次）"},
    )


@managers_bp.route("/<int:user_id>/update", methods=["POST"])
@superadmin_required
def update_admin(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        flash("只能修改普通管理员。", "error")
        return redirect(url_for("managers.index"))
    name = (request.form.get("name") or "").strip()
    student_id = (request.form.get("student_id") or "").strip()
    contact = (request.form.get("contact_phone") or "").strip()
    errors = []
    if not name:
        errors.append("请填写使用人姓名。")
    if not student_id:
        errors.append("请填写学号。")
    else:
        other = User.query.filter(User.student_id == student_id, User.id != user.id).first()
        if other:
            errors.append("该学号已被其他账号使用。")
    if contact and not is_mobile_phone(contact):
        errors.append("联系方式须为 11 位手机号。")
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("managers.index"))
    user.name = name
    user.student_id = student_id
    user.contact_phone = contact or None
    db.session.commit()
    record_audit(
        "admin.update",
        summary=f"更新管理员资料 {name}（{student_id}）。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="管理员资料请再编辑改回。",
    )
    flash("已保存管理员资料。", "success")
    return redirect(url_for("managers.index"))


@managers_bp.route("/<int:user_id>/regen", methods=["POST"])
@superadmin_required
def regen_password(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        flash("只能为普通管理员重新生成密码。", "error")
        return redirect(url_for("managers.index"))
    password = generate_unique_staff_password(exclude_id=user.id)
    user.set_password(password, require_change=True)
    db.session.commit()
    record_audit(
        "admin.regen_password",
        summary=f"为管理员 {user.name}（{user.student_id}）重新生成登录密码。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="密码无法撤回。",
    )
    return render_template(
        "managers/index.html",
        admins=_admins(),
        issued={"user": user, "password": password, "title": "重新生成的管理员密码（仅显示一次）"},
    )


@managers_bp.route("/<int:user_id>/toggle", methods=["POST"])
@superadmin_required
def toggle_admin(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        flash("只能停用或启用普通管理员。", "error")
        return redirect(url_for("managers.index"))
    user.enabled = not bool(user.enabled)
    db.session.commit()
    record_audit(
        "account.disable" if not user.enabled else "account.enable",
        summary=f"{'停用' if not user.enabled else '启用'}管理员 {user.name}（{user.student_id}）。",
        target_type="user",
        target_id=user.id,
        payload={"enabled": user.enabled, "student_id": user.student_id},
        reversible=not user.enabled,
        undo_blocked_reason=None if not user.enabled else "启用操作请直接再点停用。",
    )
    flash("已停用该管理员。" if not user.enabled else "已启用该管理员。", "success")
    return redirect(url_for("managers.index"))


@managers_bp.route("/<int:user_id>/delete", methods=["POST"])
@superadmin_required
def delete_admin(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        flash("只能删除普通管理员。", "error")
        return redirect(url_for("managers.index"))
    if user.id == current_user.id:
        flash("不能删除当前登录账号。", "error")
        return redirect(url_for("managers.index"))
    reviewed = Booking.query.filter_by(reviewed_by_id=user.id).count()
    duties = DutyShift.query.filter_by(admin_id=user.id).count()
    if reviewed or duties or user.bookings or user.tool_loans:
        flash("该管理员仍有审核记录或值班安排，请先停用账号，而不是删除。", "error")
        return redirect(url_for("managers.index"))
    name, sid = user.name, user.student_id
    detach_account_history(user.id)
    db.session.delete(user)
    db.session.commit()
    record_audit(
        "admin.delete",
        summary=f"删除管理员 {name}（{sid}）。",
        target_type="user",
        reversible=False,
        undo_blocked_reason="账号删除后无法自动恢复。",
    )
    flash("已删除该管理员账号。", "success")
    return redirect(url_for("managers.index"))


@managers_bp.route("/<int:user_id>/reveal-password", methods=["POST"])
@superadmin_required
def reveal_password(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        flash("只能查看普通管理员的密码。", "error")
        return redirect(url_for("managers.index"))
    if not user.password_cipher:
        flash("该账号还没有可查看的密文。请重新生成密码，或通过设置脚本写入后再查看。", "warning")
        return redirect(url_for("managers.index"))
    password = decrypt_password(user.password_cipher)
    if not password:
        flash("密文无法解密（查看密钥可能已更换）。请重新生成密码。", "error")
        return redirect(url_for("managers.index"))
    record_audit(
        "admin.view_password",
        summary=f"查看管理员 {user.name}（{user.student_id}）的登录密码。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="查看行为不可撤回。",
    )
    return render_template(
        "managers/index.html",
        admins=_admins(),
        issued={
            "user": user,
            "password": password,
            "title": "管理员密码（点击查看，本次已记入审计日志）",
            "revealed": True,
        },
    )
