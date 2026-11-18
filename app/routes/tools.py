from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from werkzeug.utils import secure_filename

from app import db
from app.activity import record_audit
from app.decorators import reviewer_required, superadmin_required
from app.transactions import begin_serialized_write
from app.inventory import borrowed_quantity, catalog_choices, check_loan_quantities, inventory_rows
from app.models import Lab, ToolCatalog, ToolLoan, ToolLoanItem
from app.queries import loan_list_options
from app.utils import (
    collect_loan_items,
    is_mobile_phone,
    local_now,
    parse_datetime,
    save_uploaded_photo,
)

tools_bp = Blueprint("tools", __name__)


def _upload_dir() -> Path:
    return Path(current_app.config["UPLOAD_DIR"])


def _loan_or_403(loan_id: int) -> ToolLoan | None:
    loan = db.session.get(ToolLoan, loan_id)
    return loan


def _can_view_loan(loan: ToolLoan) -> bool:
    if current_user.is_staff:
        return True
    return loan.user_id == current_user.id


@tools_bp.route("/tools")
@login_required
def my_loans():
    if current_user.is_staff:
        return redirect(url_for("tools.admin_loans"))
    loans = (
        ToolLoan.query.options(*loan_list_options())
        .filter_by(user_id=current_user.id)
        .order_by(ToolLoan.borrow_at.desc())
        .all()
    )
    return render_template("tools/list.html", loans=loans, admin_view=False)


@tools_bp.route("/tools/new", methods=["GET", "POST"])
@login_required
def create_loan():
    if not current_user.is_captain:
        flash("仅参赛队长可登记工具借用。", "error")
        return redirect(url_for("tools.admin_loans") if current_user.is_staff else url_for("main.index"))
    if request.method == "POST":
        begin_serialized_write()
    labs = Lab.query.filter_by(enabled=True).order_by(Lab.name.asc()).all()
    now = local_now().replace(second=0, microsecond=0)
    form = {
        "lab_id": request.values.get("lab_id", str(labs[0].id) if len(labs) == 1 else ""),
        "borrow_at": request.values.get("borrow_at", now.strftime("%Y-%m-%dT%H:%M")),
        "expected_return_at": request.values.get("expected_return_at", ""),
        "contact": request.values.get("contact", current_user.last_phone or ""),
        "tool_items": [{"name": "", "quantity": "1", "catalog_id": "", "is_other": False}],
    }
    if request.method == "POST":
        form["lab_id"] = (request.form.get("lab_id") or "").strip()
        form["borrow_at"] = (request.form.get("borrow_at") or "").strip()
        form["expected_return_at"] = (request.form.get("expected_return_at") or "").strip()
        form["contact"] = (request.form.get("contact") or "").strip()
        items, item_errors = collect_loan_items(request.form)
        form["tool_items"] = items or [{"name": "", "quantity": "1", "catalog_id": "", "is_other": False}]
        lab = db.session.get(Lab, int(form["lab_id"])) if form["lab_id"].isdigit() else None
        borrow_at = parse_datetime(form["borrow_at"])
        expected = parse_datetime(form["expected_return_at"])
        errors = list(item_errors)
        if not item_errors:
            errors.extend(check_loan_quantities(items))
        if lab is None or not lab.enabled:
            errors.append("请选择实验室。")
        if borrow_at is None or expected is None:
            errors.append("请填写借用时间和预计归还时间。")
        elif expected <= borrow_at:
            errors.append("预计归还时间必须晚于借用时间。")
        if not is_mobile_phone(form["contact"]):
            errors.append("请填写有效的手机号作为联系方式。")
        filename, photo_errors = save_uploaded_photo(request.files.get("borrow_photo"), _upload_dir())
        errors.extend(photo_errors)
        if errors:
            if filename:
                (_upload_dir() / filename).unlink(missing_ok=True)
            for msg in errors:
                flash(msg, "error")
        else:
            loan = ToolLoan(
                user=current_user,
                lab=lab,
                team_name=current_user.team_name,
                contact=form["contact"],
                borrow_at=borrow_at,
                expected_return_at=expected,
                borrow_photo=filename,
                status="borrowing",
            )
            db.session.add(loan)
            db.session.flush()
            for item in items:
                db.session.add(ToolLoanItem(
                    loan=loan,
                    name=item["name"],
                    quantity=item["quantity"],
                    catalog_id=item.get("catalog_id"),
                    is_other=bool(item.get("is_other")),
                ))
            current_user.last_phone = form["contact"]
            db.session.commit()
            record_audit("tool.borrow", summary=f"登记工具借用：{loan.items_label}。", target_type="tool_loan", target_id=loan.id, reversible=False, undo_blocked_reason="请通过归还流程处理。")
            flash("工具借用已登记。", "success")
            return redirect(url_for("tools.my_loans"))
    return render_template("tools/form.html", form=form, labs=labs, catalog=catalog_choices())


@tools_bp.route("/tools/<int:loan_id>/return", methods=["GET", "POST"])
@login_required
def return_loan(loan_id):
    if request.method == "POST":
        begin_serialized_write()
    loan = _loan_or_403(loan_id)
    if loan is None or not _can_view_loan(loan) or (not current_user.is_staff and loan.user_id != current_user.id):
        flash("只能归还自己的借用记录。", "error")
        return redirect(url_for("tools.my_loans"))
    if loan.status != "borrowing":
        flash("该记录已归还。", "error")
        return redirect(url_for("tools.my_loans"))
    if request.method == "POST":
        photo = request.files.get("return_photo")
        if current_user.is_superadmin and (photo is None or not photo.filename):
            filename, errors = None, []
        else:
            filename, errors = save_uploaded_photo(photo, _upload_dir())
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            loan.return_photo = filename
            loan.actual_return_at = local_now()
            loan.status = "returned"
            db.session.commit()
            record_audit("tool.return", payload={"photo_exempt": current_user.is_superadmin and filename is None}, summary=f"登记工具归还：{loan.items_label}。", target_type="tool_loan", target_id=loan.id, reversible=False, undo_blocked_reason="归还记录不能自动撤回。")
            flash("已登记归还。", "success")
            return redirect(url_for("tools.my_loans") if current_user.is_captain else url_for("tools.admin_loans"))
    return render_template("tools/return.html", loan=loan)


@tools_bp.route("/tools/admin")
@reviewer_required
def admin_loans():
    status = request.args.get("status", "all")
    if status not in ("all", "borrowing", "overdue", "returned"):
        status = "all"
    query = ToolLoan.query.options(*loan_list_options())
    if status == "returned":
        query = query.filter_by(status="returned")
    elif status == "borrowing":
        query = query.filter(ToolLoan.status == "borrowing", ToolLoan.expected_return_at >= local_now())
    elif status == "overdue":
        query = query.filter(ToolLoan.status == "borrowing", ToolLoan.expected_return_at < local_now())
    loans = query.order_by(ToolLoan.borrow_at.desc()).all()
    return render_template(
        "tools/admin_list.html",
        loans=loans,
        status=status,
        admin_view=True,
        inventory=inventory_rows(major_only=True),
    )


@tools_bp.route("/tools/admin/export")
@reviewer_required
def export_loans():
    loans = ToolLoan.query.order_by(ToolLoan.borrow_at.desc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "工具借用"
    ws.append(
        [
            "队长",
            "学号",
            "队伍",
            "组员",
            "实验室",
            "工具及数量",
            "借用时间",
            "预计归还",
            "实际归还",
            "联系方式",
            "状态",
            "借用照片",
            "归还照片",
        ]
    )
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5D62")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for loan in loans:
        ws.append(
            [
                loan.user.name,
                loan.user.student_id,
                loan.team_name or "",
                loan.user.teammates_label if loan.user else "",
                loan.lab.name,
                loan.items_label,
                loan.borrow_at.strftime("%Y-%m-%d %H:%M"),
                loan.expected_return_at.strftime("%Y-%m-%d %H:%M"),
                loan.actual_return_at.strftime("%Y-%m-%d %H:%M") if loan.actual_return_at else "",
                loan.contact,
                loan.display_status_label,
                loan.borrow_photo,
                loan.return_photo or "",
            ]
        )
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="tool-loans.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@tools_bp.route("/media/<filename>")
@login_required
def media(filename):
    name = secure_filename(filename)
    if not name or name != filename or "/" in filename or "\\" in filename:
        return "文件不存在。", 404
    loan = ToolLoan.query.filter(
        (ToolLoan.borrow_photo == name) | (ToolLoan.return_photo == name)
    ).first()
    if loan is None or not _can_view_loan(loan):
        return "无权查看该照片。", 403
    path = _upload_dir() / name
    if not path.is_file():
        return "文件不存在。", 404
    return send_file(path)


@tools_bp.route("/tools/catalog")
@superadmin_required
def catalog():
    return render_template("tools/catalog.html", items=inventory_rows(major_only=False))


def _parse_catalog_form():
    name = (request.form.get("name") or "").strip()
    raw_qty = (request.form.get("total_quantity") or "").strip()
    is_major = (request.form.get("is_major") or "").strip() in {"1", "on", "true", "yes"}
    errors = []
    if not name:
        errors.append("请填写工具名称。")
    elif len(name) > 80:
        errors.append("工具名称不能超过 80 字。")
    try:
        qty = int(raw_qty)
    except (TypeError, ValueError):
        qty = None
        errors.append("总数量须为非负整数。")
    else:
        if qty < 0:
            errors.append("总数量须为非负整数。")
    return name, qty, is_major, errors


@tools_bp.route("/tools/catalog/new", methods=["POST"])
@superadmin_required
def catalog_create():
    name, qty, is_major, errors = _parse_catalog_form()
    if not errors and ToolCatalog.query.filter_by(name=name).first():
        errors.append("已存在同名工具。")
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("tools.catalog"))
    item = ToolCatalog(name=name, total_quantity=qty, is_major=is_major)
    db.session.add(item)
    db.session.commit()
    record_audit("catalog.create", summary=f"新增工具「{item.name}」（总数 {item.total_quantity}）。", target_type="catalog", target_id=item.id, payload={"item": {"name": item.name, "total_quantity": item.total_quantity, "is_major": item.is_major}}, reversible=True)
    flash("已添加工具。", "success")
    return redirect(url_for("tools.catalog"))


@tools_bp.route("/tools/catalog/<int:item_id>/update", methods=["POST"])
@superadmin_required
def catalog_update(item_id):
    begin_serialized_write()
    item = db.session.get(ToolCatalog, item_id)
    if item is None:
        flash("工具不存在。", "error")
        return redirect(url_for("tools.catalog"))
    name, qty, is_major, errors = _parse_catalog_form()
    other = ToolCatalog.query.filter(ToolCatalog.name == name, ToolCatalog.id != item.id).first()
    if other:
        errors.append("已存在同名工具。")
    if qty is not None and qty < borrowed_quantity(item.id):
        errors.append("总数量不能少于当前已借出数量。")
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("tools.catalog"))
    before = {"name": item.name, "total_quantity": item.total_quantity, "is_major": item.is_major}
    item.name = name
    item.total_quantity = qty
    item.is_major = is_major
    db.session.commit()
    record_audit("catalog.update", summary=f"修改工具清单「{item.name}」。", target_type="catalog", target_id=item.id, payload={"before": before, "after": {"name": item.name, "total_quantity": item.total_quantity, "is_major": item.is_major}}, reversible=True)
    flash("已保存工具信息。", "success")
    return redirect(url_for("tools.catalog"))


@tools_bp.route("/tools/catalog/<int:item_id>/delete", methods=["POST"])
@superadmin_required
def catalog_delete(item_id):
    begin_serialized_write()
    item = db.session.get(ToolCatalog, item_id)
    if item is None:
        flash("工具不存在。", "error")
        return redirect(url_for("tools.catalog"))
    if borrowed_quantity(item.id):
        flash("该工具仍有未归还的借用记录，不能删除。", "error")
        return redirect(url_for("tools.catalog"))
    snapshot = {"name": item.name, "total_quantity": item.total_quantity, "is_major": item.is_major}
    name = item.name
    ToolLoanItem.query.filter_by(catalog_id=item.id).update({"catalog_id": None})
    db.session.delete(item)
    db.session.commit()
    record_audit("catalog.delete", summary=f"删除工具「{name}」。", target_type="catalog", payload={"item": snapshot}, reversible=True)
    flash("已删除工具。", "success")
    return redirect(url_for("tools.catalog"))

