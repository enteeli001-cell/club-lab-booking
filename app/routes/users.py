from io import BytesIO

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from openpyxl import Workbook

from app import db
from app.decorators import superadmin_required
from app.models import ROLE_LABELS, Booking, DutyShift, User
from app.account_history import detach_account_history
from app.activity import record_audit
from app.utils import (
    apply_team_roster,
    collect_team_form,
    is_mobile_phone,
    generate_unique_staff_password,
    normalize_role,
    parse_user_import,
    resolve_account_password,
    staff_password_complexity_errors,
    staff_password_is_unique,
)

users_bp = Blueprint("users", __name__, url_prefix="/users")


def _issue_password(role: str, student_id: str, provided: str | None, exclude_id: int | None = None):
    """Return (password, auto, errors). Do not log the password."""
    provided = (provided or "").strip()
    if role in ("admin", "superadmin"):
        if provided:
            errors = staff_password_complexity_errors(provided)
            if not staff_password_is_unique(provided, exclude_id=exclude_id):
                errors.append("该密码与其他管理员的密码相同，请更换。")
            if errors:
                return "", False, errors
            return provided, False, []
        try:
            return generate_unique_staff_password(exclude_id=exclude_id), True, []
        except RuntimeError as exc:
            return "", True, [str(exc)]
    return resolve_account_password(role, student_id, provided)


@users_bp.route("/")
@superadmin_required
def list_users():
    from sqlalchemy.orm import selectinload

    users = User.query.options(selectinload(User.teammates)).order_by(User.role.asc(), User.student_id.asc()).all()
    return render_template("users/list.html", users=users)


@users_bp.route("/new", methods=["GET", "POST"])
@superadmin_required
def create_user():
    generated = None
    form = {
        "name": "",
        "student_id": "",
        "role": "member",
        "password": "",
        "contact_phone": "",
    }
    if request.method == "POST":
        form["name"] = (request.form.get("name") or "").strip()
        form["student_id"] = (request.form.get("student_id") or "").strip()
        form["role"] = (request.form.get("role") or "member").strip()
        form["password"] = (request.form.get("password") or "").strip()
        form["contact_phone"] = (request.form.get("contact_phone") or "").strip()

        errors = []
        if not form["name"]:
            errors.append("请填写姓名。")
        if not form["student_id"]:
            errors.append("请填写学号或账号。")
        elif User.query.filter_by(student_id=form["student_id"]).first():
            errors.append("该学号/账号已存在。")
        if form["role"] not in ROLE_LABELS:
            errors.append("角色无效。")
        if form["role"] in ("admin", "superadmin") and form.get("contact_phone"):
            if not is_mobile_phone(form["contact_phone"]):
                errors.append("管理员联系方式须为 11 位手机号。")

        password, auto, pw_errors = _issue_password(form["role"], form["student_id"], form["password"])
        errors.extend(pw_errors)

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            user = User(name=form["name"], student_id=form["student_id"], role=form["role"], contact_phone=(form.get("contact_phone") or None) if form["role"] in ("admin", "superadmin") else None)
            user.set_password(password, require_change=True)
            db.session.add(user)
            db.session.flush()
            if user.is_captain:
                team_name, members, team_errors = collect_team_form(request.form)
                errors.extend(team_errors)
                if not errors:
                    errors.extend(apply_team_roster(user, team_name, members))
                if errors:
                    db.session.rollback()
                    for msg in errors:
                        flash(msg, "error")
                    return render_template("users/new.html", form=form, generated=None)
            db.session.commit()
            generated = {
                "user": user,
                "password": password,
                "is_captain_default": bool(user.is_captain and auto),
                "auto": auto,
            }
            if user.is_captain and auto:
                flash(f"已添加参赛队长：{user.name}（{user.student_id}）。初始密码等于学号，首次登录须修改。请立即抄录。", "success")
            else:
                flash(f"已添加账号：{user.name}（{user.student_id}），请立即抄录下方密码。" + ("" if user.role == "admin" else "首次登录须修改。"), "success")
            return render_template(
                "users/new.html",
                form={"name": "", "student_id": "", "role": "member", "password": "", "contact_phone": ""},
                generated=generated,
            )

    return render_template("users/new.html", form=form, generated=generated)


@users_bp.route("/import", methods=["GET", "POST"])
@superadmin_required
def import_users():
    if request.method == "POST":
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            flash("请选择要导入的 CSV 或 Excel 文件。", "error")
            return render_template("users/import.html")

        records, errors = parse_user_import(uploaded)
        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("users/import.html")

        created = []
        skipped = []
        for record in records:
            row = record["row"]
            name = record["name"]
            student_id = record["student_id"]
            role = normalize_role(record["role_raw"])
            password = record["password"]

            if not name or not student_id:
                skipped.append({"row": row, "reason": "姓名或学号/账号为空"})
                continue
            if role is None:
                skipped.append({"row": row, "reason": f"角色无效：{record['role_raw'] or '（空）'}"})
                continue
            if User.query.filter_by(student_id=student_id).first():
                skipped.append({"row": row, "reason": f"学号/账号 {student_id} 已存在"})
                continue
            password, auto, pw_errors = _issue_password(role, student_id, password)
            if pw_errors:
                skipped.append({"row": row, "reason": pw_errors[0]})
                continue

            user = User(name=name, student_id=student_id, role=role)
            user.set_password(password, require_change=True)
            db.session.add(user)
            db.session.flush()
            if user.is_captain:
                team_errors = apply_team_roster(user, record.get("team_name") or "", record.get("members") or [])
                if team_errors:
                    db.session.delete(user)
                    skipped.append({"row": row, "reason": team_errors[0]})
                    continue
            created.append({"user": user, "password": password, "auto": auto, "row": row})

        db.session.commit()
        if created:
            flash(f"成功导入 {len(created)} 个账号。请立即抄录或下载一次性密码，不会写入系统日志。", "success")
        if skipped:
            flash(f"跳过 {len(skipped)} 行，请查看下方说明。", "warning")
        return render_template("users/import_result.html", created=created, skipped=skipped)

    return render_template("users/import.html")


@users_bp.route("/import/template.xlsx")
@superadmin_required
def download_import_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "账号导入模板"
    ws.append([
        "姓名",
        "学号",
        "角色",
        "初始密码",
        "队名",
        "队员1姓名",
        "队员1学号",
        "队员2姓名",
        "队员2学号",
    ])
    ws.append([
        "张三",
        "2021001",
        "参赛队长",
        "",
        "结构先锋队",
        "李四",
        "2021011",
        "周七",
        "2021012",
    ])
    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="account_import_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@users_bp.route("/import/passwords.xlsx", methods=["POST"])

@superadmin_required
def download_import_passwords():
    names = request.form.getlist("name")
    student_ids = request.form.getlist("student_id")
    passwords = request.form.getlist("password")
    if not student_ids:
        flash("没有可导出的账号。", "error")
        return redirect(url_for("users.import_users"))

    wb = Workbook()
    ws = wb.active
    ws.title = "导入账号"
    ws.append(["姓名", "学号/账号", "角色", "初始密码"])
    roles = request.form.getlist("role_label")
    for i, student_id in enumerate(student_ids):
        ws.append([
            names[i] if i < len(names) else "",
            student_id,
            roles[i] if i < len(roles) else "",
            passwords[i] if i < len(passwords) else "",
        ])
    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="imported_accounts.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@users_bp.route("/<int:user_id>/reset-password", methods=["POST"])
@superadmin_required
def reset_password(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("账号不存在。", "error")
        return redirect(url_for("users.list_users"))
    provided = (request.form.get("password") or "").strip()
    if user.is_captain and not provided:
        user.set_password(user.student_id, require_change=True)
        db.session.commit()
        flash(f"已将 {user.name}（{user.student_id}）的密码重置为学号，该队长下次登录须修改密码。", "success")
        return redirect(url_for("users.list_users"))

    password, auto, errors = _issue_password(user.role, user.student_id, provided, exclude_id=user.id)
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("users.list_users"))
    user.set_password(password, require_change=True)
    db.session.commit()
    flash(f"已重置 {user.name}（{user.student_id}）的密码，请立即抄录。" + ("" if user.role == "admin" else "对方下次登录须修改。"), "success")
    from sqlalchemy.orm import selectinload
    return render_template("users/list.html", users=User.query.options(selectinload(User.teammates)).order_by(User.role.asc(), User.student_id.asc()).all(), issued={"user": user, "password": password, "auto": auto})



@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@superadmin_required
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("账号不存在。", "error")
        return redirect(url_for("users.list_users"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        contact_phone = (request.form.get("contact_phone") or "").strip()
        if not name:
            flash("请填写姓名。", "error")
        elif user.is_staff and contact_phone and not is_mobile_phone(contact_phone):
            flash("管理员联系方式须为 11 位手机号。", "error")
        else:
            user.name = name
            if user.is_staff:
                user.contact_phone = contact_phone or None
            if user.is_captain:
                team_name, members, errors = collect_team_form(request.form)
                if errors:
                    for msg in errors:
                        flash(msg, "error")
                    return render_template("users/edit.html", user=user, members=members, team_name=team_name)
                errors = apply_team_roster(user, team_name, members)
                if errors:
                    for msg in errors:
                        flash(msg, "error")
                    return render_template("users/edit.html", user=user, members=members, team_name=team_name)
            db.session.commit()
            flash("账号信息已更新。", "success")
            return redirect(url_for("users.list_users"))
    members = [{"name": m.name, "student_id": m.student_id} for m in user.teammates]
    if not members:
        members = [{"name": "", "student_id": ""}]
    return render_template("users/edit.html", user=user, members=members, team_name=user.team_name or "")


@users_bp.route("/<int:user_id>/delete", methods=["POST"])
@superadmin_required
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        flash("账号不存在。", "error")
        return redirect(url_for("users.list_users"))
    if user.id == current_user.id:
        flash("不能删除当前登录的总管理员账号。", "error")
        return redirect(url_for("users.list_users"))
    if user.is_superadmin and User.query.filter_by(role="superadmin").count() <= 1:
        flash("不能删除唯一的总管理员。", "error")
        return redirect(url_for("users.list_users"))
    if user.bookings or Booking.query.filter_by(reviewed_by_id=user.id).first():
        flash("该账号仍有关联预约记录，不能删除。", "error")
        return redirect(url_for("users.list_users"))
    if user.tool_loans or DutyShift.query.filter_by(admin_id=user.id).first():
        flash("该账号仍有工具借用记录或值班安排，不能删除。可先停用。", "error")
        return redirect(url_for("users.list_users"))
    from app.models import MaterialClaim, MaterialPurchase
    if MaterialClaim.query.filter_by(user_id=user.id).first() or MaterialPurchase.query.filter_by(user_id=user.id).first():
        flash("该账号仍有材料记录，不能删除。可先停用。", "error")
        return redirect(url_for("users.list_users"))
    detach_account_history(user.id)
    name, sid = user.name, user.student_id
    db.session.delete(user)
    db.session.commit()
    record_audit("user.delete", summary=f"删除账号 {name}（{sid}）。", target_type="user", reversible=False)
    flash(f"已删除账号：{user.name}（{user.student_id}）。", "success")
    return redirect(url_for("users.list_users"))
