"""Superadmin people/roster management."""

from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from openpyxl import Workbook
from openpyxl.styles import Font

from app import db
from app.import_preview import save_preview, consume_preview
from app.activity import record_audit
from app.decorators import superadmin_required
from app.models import User
from app.utils import (
    apply_team_roster,
    collect_team_form,
    is_email_address,
    is_mobile_phone,
    parse_date,
    parse_team_import,
    today,
)

people_bp = Blueprint("people", __name__, url_prefix="/people")


def _captains():
    from sqlalchemy.orm import selectinload

    return User.query.options(selectinload(User.teammates)).filter_by(role="member").order_by(User.team_name.asc(), User.student_id.asc()).all()


def _specials():
    return (
        User.query.filter(User.role.in_(("approved_member", "special_guest")))
        .order_by(User.role.asc(), User.student_id.asc())
        .all()
    )


def _validate_captain_fields(name, student_id, phone, email, exclude_id=None):
    errors = []
    if not name:
        errors.append("请填写姓名。")
    if not student_id:
        errors.append("请填写学号。")
    else:
        q = User.query.filter_by(student_id=student_id)
        if exclude_id:
            q = q.filter(User.id != exclude_id)
        if q.first():
            errors.append(f"学号 {student_id} 已被其他账号使用。")
    if phone and not is_mobile_phone(phone):
        errors.append("队长手机号格式不正确。")
    if email and not is_email_address(email):
        errors.append("队长邮箱格式不正确。")
    return errors


def _prepare_import_rows(records):
    prepared = []
    seen_captains: dict[str, int] = {}
    for rec in records:
        row = rec["row"]
        errors = []
        name = (rec.get("name") or "").strip()
        sid = (rec.get("student_id") or "").strip()
        phone = (rec.get("phone") or "").strip()
        email = (rec.get("email") or "").strip()
        team_name = (rec.get("team_name") or "").strip()
        members = rec.get("members") or []
        if not team_name:
            errors.append("队名不能为空。")
        if not name:
            errors.append("队长姓名不能为空。")
        if not sid:
            errors.append("队长学号不能为空。")
        if phone and not is_mobile_phone(phone):
            errors.append("队长手机号格式不正确。")
        if email and not is_email_address(email):
            errors.append("队长邮箱格式不正确。")
        for m in members:
            if not m.get("name") or not m.get("student_id"):
                errors.append("队员姓名和学号须同时填写。")
            elif m["student_id"] == sid:
                errors.append(f"队员学号不能与队长相同（{sid}）。")
            if m.get("phone") and not is_mobile_phone(m["phone"]):
                errors.append(f"队员 {m.get('name') or m.get('student_id')} 的手机号格式不正确。")
        member_ids = [m.get("student_id") for m in members if m.get("student_id")]
        if len(member_ids) != len(set(member_ids)):
            errors.append("队员学号不能重复。")
        if sid:
            existing = User.query.filter_by(student_id=sid).first()
            if existing and existing.is_staff:
                errors.append(f"学号 {sid} 已是管理员账号，不能作为队长导入。")
            elif existing and existing.role not in ("member",):
                errors.append(f"学号 {sid} 已是「{existing.role_label}」账号，不能作为队长导入。")
            if sid in seen_captains:
                # later row updates earlier row — allowed
                pass
            seen_captains[sid] = row
        action = "error"
        if not errors:
            existing = User.query.filter_by(student_id=sid).first()
            action = "update" if existing else "create"
        prepared.append(
            {
                "row": row,
                "action": action,
                "errors": errors,
                "team_name": team_name,
                "name": name,
                "student_id": sid,
                "phone": phone,
                "email": email,
                "members": members,
            }
        )
    return prepared


def _apply_import_row(item) -> User:
    sid = item["student_id"]
    user = User.query.filter_by(student_id=sid).first()
    created = user is None
    if user is None:
        user = User(student_id=sid, name=item["name"], role="member")
        user.set_password(sid, require_change=True)
        db.session.add(user)
        db.session.flush()
    else:
        user.name = item["name"]
        user.role = "member"
    user.team_name = item["team_name"] or None
    if item["phone"]:
        user.last_phone = item["phone"]
        user.contact_phone = item["phone"]
    if item["email"]:
        user.last_email = item["email"]
    apply_team_roster(user, item["team_name"], item["members"])
    return user, created


@people_bp.route("/")
@superadmin_required
def index():
    return render_template("people/index.html", captains=_captains(), specials=_specials(), today=today())


@people_bp.route("/import", methods=["GET", "POST"])
@superadmin_required
def import_teams():
    if request.method == "POST":
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            flash("请选择要上传的 Excel 或 CSV 文件。", "error")
            return render_template("people/import.html")
        records, errors = parse_team_import(uploaded)
        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("people/import.html")
        prepared = _prepare_import_rows(records)
        save_preview(prepared)
        return render_template("people/import_preview.html", rows=prepared)
    return render_template("people/import.html")


@people_bp.route("/import/confirm", methods=["POST"])
@superadmin_required
def import_confirm():
    prepared = consume_preview()
    if prepared:
        # Revalidate against current accounts before applying a previously previewed upload.
        prepared = _prepare_import_rows(prepared)
    if not prepared:
        flash("没有可确认的导入预览，请重新上传。", "error")
        return redirect(url_for("people.import_teams"))
    created = updated = skipped = 0
    for item in prepared:
        if item["action"] == "error":
            skipped += 1
            continue
        _user, is_new = _apply_import_row(item)
        if is_new:
            created += 1
        else:
            updated += 1
    db.session.commit()
    record_audit(
        "people.import",
        summary=f"导入参赛队伍：新增 {created} 支，更新 {updated} 支，跳过 {skipped} 行。",
        payload={"created": created, "updated": updated, "skipped": skipped},
        reversible=False,
        undo_blocked_reason="批量导入无法自动整批撤回。",
    )
    flash(f"导入完成：新增 {created} 支队伍，更新 {updated} 支，跳过 {skipped} 行。", "success")
    return redirect(url_for("people.index"))


@people_bp.route("/import/template.xlsx")
@superadmin_required
def download_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "参赛队伍"
    headers = [
        "队名",
        "队长姓名",
        "队长学号",
        "队长手机",
        "队长邮箱",
        "队员1姓名",
        "队员1学号",
        "队员1手机",
        "队员2姓名",
        "队员2学号",
        "队员2手机",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.append(
        [
            "测试队",
            "张三",
            "2021001",
            "13800138000",
            "zhangsan@example.com",
            "李四",
            "2021011",
            "13800138011",
            "周七",
            "2021012",
            "13800138012",
        ]
    )
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 16
    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="team_import_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@people_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@superadmin_required
def edit_person(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_staff:
        flash("只能编辑参赛或特批人员。", "error")
        return redirect(url_for("people.index"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        student_id = (request.form.get("student_id") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        team_name = (request.form.get("team_name") or "").strip()
        organization = (request.form.get("organization") or "").strip()
        special_reason = (request.form.get("special_reason") or "").strip()
        valid_until = parse_date(request.form.get("valid_until"))
        errors = _validate_captain_fields(name, student_id, phone, email, exclude_id=user.id)
        members = []
        if user.is_captain:
            team_name, members, team_errors = collect_team_form(request.form)
            errors.extend(team_errors)
        if user.is_special_guest and not organization:
            errors.append("请填写单位。")
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            before = {"name": user.name, "student_id": user.student_id, "team_name": user.team_name}
            user.name = name
            user.student_id = student_id
            if phone:
                user.last_phone = phone
            if email:
                user.last_email = email
            if user.is_captain:
                apply_team_roster(user, team_name, members)
            else:
                user.team_name = team_name or user.team_name
            if user.is_special_guest:
                user.organization = organization or None
                user.special_reason = special_reason or None
                user.valid_until = valid_until
            db.session.commit()
            record_audit(
                "people.update",
                summary=f"更新人员 {user.name}（{user.student_id}）资料。",
                target_type="user",
                target_id=user.id,
                payload={"before": before, "after": {"name": name, "student_id": student_id, "team_name": user.team_name}},
                reversible=False,
                undo_blocked_reason="人员资料修改不支持一键撤销，请再编辑改回。",
            )
            flash("已保存。", "success")
            return redirect(url_for("people.index"))
    return render_template("people/edit.html", user=user)


@people_bp.route("/<int:user_id>/password", methods=["POST"])
@superadmin_required
def set_password(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_staff:
        flash("只能为参赛或特批人员设置密码。", "error")
        return redirect(url_for("people.index"))
    mode = (request.form.get("mode") or "").strip()
    provided = (request.form.get("password") or "").strip()
    if mode == "reset_sid":
        user.set_password(user.student_id, require_change=True)
        db.session.commit()
        record_audit("people.reset_password", summary=f"将 {user.name}（{user.student_id}）密码重置为学号。", target_type="user", target_id=user.id, reversible=False, undo_blocked_reason="密码无法撤回。")
        flash("已将密码重置为学号，该账号下次登录须修改密码。", "success")
    elif provided:
        user.set_password(provided, require_change=False)
        db.session.commit()
        record_audit("people.set_password", summary=f"已为 {user.name}（{user.student_id}）手动设置登录密码。", target_type="user", target_id=user.id, reversible=False, undo_blocked_reason="密码无法撤回。")
        flash("已设置新的登录密码（不会在页面上再次显示）。", "success")
    else:
        flash("请填写新密码，或选择重置为学号。", "error")
    return redirect(url_for("people.edit_person", user_id=user.id))


@people_bp.route("/member-login", methods=["POST"])
@superadmin_required
def grant_member_login():
    captain = db.session.get(User, request.form.get("captain_id", type=int))
    student_id = (request.form.get("student_id") or "").strip()
    if captain is None or not captain.is_captain:
        flash("未找到该队伍。", "error")
        return redirect(url_for("people.index"))
    member = next((m for m in captain.teammates if m.student_id == student_id), None)
    if member is None:
        flash("该学号不在此队名单中。", "error")
        return redirect(url_for("people.edit_person", user_id=captain.id))
    existing = User.query.filter_by(student_id=student_id).first()
    if existing:
        flash("该学号已有登录账号。", "error")
        return redirect(url_for("people.edit_person", user_id=captain.id))
    user = User(
        student_id=member.student_id,
        name=member.name,
        role="approved_member",
        team_name=captain.team_name,
        last_phone=member.phone,
        enabled=True,
    )
    user.set_password(member.student_id, require_change=True)
    db.session.add(user)
    db.session.commit()
    record_audit(
        "people.grant_login",
        summary=f"特批队员 {user.name}（{user.student_id}）开通登录（仅预约）。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="开通登录后请使用停用账号来关闭访问。",
    )
    flash(f"已为 {user.name} 开通登录（权限为仅预约，初始密码为学号，首次登录须改密）。", "success")
    return redirect(url_for("people.edit_person", user_id=captain.id))


@people_bp.route("/special", methods=["POST"])
@superadmin_required
def create_special():
    name = (request.form.get("name") or "").strip()
    student_id = (request.form.get("student_id") or "").strip()
    organization = (request.form.get("organization") or "").strip()
    reason = (request.form.get("special_reason") or "").strip()
    valid_until = parse_date(request.form.get("valid_until"))
    errors = []
    if not name:
        errors.append("请填写姓名。")
    if not student_id:
        errors.append("请填写学号。")
    elif User.query.filter_by(student_id=student_id).first():
        errors.append("该学号已存在。")
    if not organization:
        errors.append("请填写单位。")
    if not reason:
        errors.append("请填写事由。")
    if valid_until is None:
        errors.append("请填写有效期。")
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("people.index"))
    user = User(
        student_id=student_id,
        name=name,
        role="special_guest",
        organization=organization,
        special_reason=reason,
        valid_until=valid_until,
        enabled=True,
    )
    user.set_password(student_id, require_change=True)
    db.session.add(user)
    db.session.commit()
    record_audit(
        "people.create_special",
        summary=f"创建特批非参赛账号 {name}（{student_id}），有效期至 {valid_until}。",
        target_type="user",
        target_id=user.id,
        reversible=False,
        undo_blocked_reason="请使用停用或删除来处理该账号。",
    )
    flash("已创建特批非参赛账号，初始密码为学号，首次登录须改密。权限为仅预约。", "success")
    return redirect(url_for("people.index"))


@people_bp.route("/<int:user_id>/toggle", methods=["POST"])
@superadmin_required
def toggle_enabled(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_superadmin:
        flash("不能停用总管理员。", "error")
        return redirect(url_for("people.index"))
    user.enabled = not bool(user.enabled)
    db.session.commit()
    action = "account.disable" if not user.enabled else "account.enable"
    record_audit(
        action,
        summary=f"{'停用' if not user.enabled else '启用'}账号 {user.name}（{user.student_id}）。",
        target_type="user",
        target_id=user.id,
        payload={"enabled": user.enabled, "student_id": user.student_id},
        reversible=not user.enabled,
        undo_blocked_reason=None if not user.enabled else "启用操作请直接再点停用。",
    )
    flash("已停用该账号。" if not user.enabled else "已启用该账号。", "success")
    return redirect(request.referrer or url_for("people.index"))
