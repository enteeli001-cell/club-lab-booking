"""材料领取与购买（队长端 + 管理端）。

权限：
* 队长（参赛队长）：领取本队免费材料、购买材料（邮箱验证码确认）。
* 管理员：只读查看记录、统计和导出。
* 总管理员：设置材料种类/份数/价格/库存，修改、作废、删除记录，标记结算；均记入审计日志。
"""

import re
from fractions import Fraction
from io import BytesIO
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from app import db
from app import materials as mat
from app import verification as vc
from app.activity import client_ip, current_user_agent, record_audit
from app.decorators import reviewer_required, superadmin_required
from app.models import (
    MaterialClaim,
    MaterialClaimItem,
    MaterialItem,
    MaterialPurchase,
    MaterialPurchaseItem,
    User,
    format_cents,
)
from app.transactions import begin_serialized_write
from app.utils import local_now, protect_excel_text, save_uploaded_photo
from app.quantities import parse_quantity, QUANTITY_ERROR, QUANTITY_HELP

materials_bp = Blueprint("materials", __name__)

CODE_RE = re.compile(r"^[0-9]{6}$")
CODE_EMAILS_PER_HOUR = 10


@materials_bp.app_template_filter("cents")
def _cents_filter(value):
    return format_cents(value)


@materials_bp.app_template_global("quantity_help")
def _quantity_help():
    return QUANTITY_HELP


def _upload_dir() -> Path:
    return Path(current_app.config["UPLOAD_DIR"])


def _remove_photo(name: str | None) -> None:
    if name:
        (_upload_dir() / name).unlink(missing_ok=True)


def _captain_only(feature_label: str):
    """None when the current user may use the captain pages, else a redirect."""
    if current_user.is_staff:
        return redirect(url_for("materials.admin_records"))
    if not current_user.is_captain or not current_user.is_account_active:
        flash(f"仅参赛队长可使用{feature_label}。", "error")
        return redirect(url_for("main.index"))
    return None


def _code_limiter_allows(user_id: int) -> bool:
    if not vc.delivery_allowed(user_id):
        return False
    limiter = current_app.extensions.get("material_code_limiter")
    if limiter is None:
        return True
    return limiter.allow(f"material-code:{user_id}", CODE_EMAILS_PER_HOUR, 3600)


# ================================================================== captain: free claim

@materials_bp.route("/materials/claim", methods=["GET", "POST"])
@login_required
def claim():
    blocked = _captain_only("材料领取")
    if blocked:
        return blocked
    if request.method == "POST":
        begin_serialized_write()
    existing = mat.active_claim(current_user.id)
    items = mat.allotment_items()
    shortages = mat.allotment_shortages(items)
    if request.method == "POST":
        if existing is not None:
            db.session.rollback()
            flash("你的队伍已经领取过本次分发的材料，每队只能领取一次。", "error")
            return redirect(url_for("materials.claim"))
        errors = []
        if not items:
            errors.append("目前没有可领取的材料，请等待总管理员设置。")
        errors.extend(shortages)
        if (request.form.get("confirm") or "") != "1":
            errors.append("请勾选“已核对材料种类和数量”。")
        filename = None
        if not errors:
            filename, photo_errors = save_uploaded_photo(request.files.get("photo"), _upload_dir())
            errors.extend(photo_errors)
        if errors:
            db.session.rollback()
            _remove_photo(filename)
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("materials.claim"))
        record = MaterialClaim(
            user_id=current_user.id,
            team_name=current_user.team_name,
            photo=filename,
            status="active",
            ip=client_ip() or None,
            user_agent=(current_user_agent() or "")[:300] or None,
            created_at=local_now(),
        )
        db.session.add(record)
        failed = None
        for item in items:
            if not mat.take_stock(item.id, item.per_team_qty):
                failed = item
                break
            record.items.append(MaterialClaimItem(
                item_id=item.id, name=item.name, spec=item.spec, unit=item.unit, quantity=item.per_team_qty,
            ))
        if failed is not None:
            db.session.rollback()
            _remove_photo(filename)
            flash(f"「{failed.label}」库存不足，暂时不能领取，请联系管理员。", "error")
            return redirect(url_for("materials.claim"))
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            _remove_photo(filename)
            flash("你的队伍已经领取过本次分发的材料，每队只能领取一次。", "error")
            return redirect(url_for("materials.claim"))
        mat.expire_session_items()
        record_audit(
            "material.claim",
            summary=f"领取比赛材料：{record.items_label}。",
            target_type="material_claim",
            target_id=record.id,
            payload={"items": [[line.name, line.quantity] for line in record.items]},
            reversible=False,
            undo_blocked_reason="请由总管理员在材料记录中作废。",
        )
        flash("领取成功，已记录本队领取的材料和照片。", "success")
        return redirect(url_for("materials.claim"))
    return render_template("materials/claim.html", existing=existing, items=items, shortages=shortages)


# ================================================================== captain: purchase

def _my_purchases():
    return (
        MaterialPurchase.query.options(selectinload(MaterialPurchase.items))
        .filter(MaterialPurchase.user_id == current_user.id, MaterialPurchase.status.in_(("confirmed", "voided")))
        .order_by(MaterialPurchase.created_at.desc())
        .all()
    )


@materials_bp.route("/materials/buy", methods=["GET", "POST"])
@login_required
def buy():
    blocked = _captain_only("材料购买")
    if blocked:
        return blocked
    if request.method == "POST":
        begin_serialized_write()
    items = mat.items_of("sale")
    email = mat.bound_email(current_user)
    pending = (
        MaterialPurchase.query.filter_by(user_id=current_user.id, status="pending")
        .order_by(MaterialPurchase.id.desc())
        .first()
    )
    if request.method == "POST":
        db.session.refresh(current_user._get_current_object())
        email = mat.bound_email(current_user)
        if not email:
            flash("请先完善个人信息并验证邮箱后再使用系统。", "error")
            return redirect(url_for("profile.setup"))
        picked, errors = mat.collect_purchase_selection(request.form, items)
        if (request.form.get("confirm") or "") != "1":
            errors.append("请勾选确认购买明细和金额。")
        if not errors and not _code_limiter_allows(current_user.id):
            errors.append("验证码发送过于频繁，请稍后再试。")
        filename = None
        if not errors:
            filename, photo_errors = save_uploaded_photo(request.files.get("photo"), _upload_dir())
            errors.extend(photo_errors)
        if errors:
            _remove_photo(filename)
            for msg in errors:
                flash(msg, "error")
            return render_template(
                "materials/buy.html", items=items, email=email, masked_email=mat.mask_email(email),
                pending=pending, purchases=_my_purchases(), qty={i.id: request.form.get(f"qty_{i.id}", "") for i in items},
                totals=_my_totals(),
            )
        mat.cancel_other_pending(current_user.id)
        purchase = MaterialPurchase(
            user_id=current_user.id,
            team_name=current_user.team_name,
            photo=filename,
            status="pending",
            email_to=email,
            ip=client_ip() or None,
            user_agent=(current_user_agent() or "")[:300] or None,
            created_at=local_now(),
        )
        for item, qty in picked:
            purchase.items.append(MaterialPurchaseItem(
                item_id=item.id, name=item.name, spec=item.spec, unit=item.unit,
                unit_price_cents=item.unit_price_cents, quantity=qty,
            ))
        purchase.recompute_total()
        db.session.add(purchase)
        db.session.flush()
        log = mat.issue_code(purchase)
        if getattr(log, "status", "") == "failed":
            flash("验证码邮件发送失败，请稍后点“重新发送验证码”，或联系管理员。", "error")
        else:
            flash(f"验证码已发送到 {mat.mask_email(email)}，5 分钟内有效。", "success")
        return redirect(url_for("materials.verify", purchase_id=purchase.id))
    return render_template(
        "materials/buy.html", items=items, email=email, masked_email=mat.mask_email(email),
        pending=pending, purchases=_my_purchases(), qty={}, totals=_my_totals(),
    )


def _my_totals() -> dict:
    rows = MaterialPurchase.query.filter_by(user_id=current_user.id, status="confirmed").all()
    return {
        "total": format_cents(sum(r.total_cents for r in rows)),
        "unsettled": format_cents(sum(r.total_cents for r in rows if not r.settled)),
    }


def _own_pending_or_redirect(purchase_id: int):
    purchase = db.session.get(MaterialPurchase, purchase_id)
    if purchase is None or purchase.user_id != current_user.id:
        flash("购买记录不存在。", "error")
        return None, redirect(url_for("materials.buy"))
    if purchase.status != "pending":
        if purchase.status == "confirmed":
            flash("该购买已确认，无需重复验证。", "info")
        else:
            flash("该购买申请已失效，请重新提交。", "error")
        return None, redirect(url_for("materials.buy"))
    # A code sent before an address change cannot authorize a purchase afterwards.
    db.session.refresh(current_user._get_current_object())
    if not mat.bound_email(current_user) or (purchase.email_to or "").lower() != mat.bound_email(current_user).lower():
        flash("邮箱已变更，请重新提交购买。", "error")
        return None, redirect(url_for("materials.buy"))
    return purchase, None


@materials_bp.route("/materials/buy/<int:purchase_id>/verify", methods=["GET", "POST"])
@login_required
def verify(purchase_id):
    blocked = _captain_only("材料购买")
    if blocked:
        return blocked
    if request.method == "POST":
        begin_serialized_write()
    purchase, redirect_response = _own_pending_or_redirect(purchase_id)
    if redirect_response:
        db.session.rollback()
        return redirect_response
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not CODE_RE.fullmatch(code):
            db.session.rollback()
            flash("请输入邮件中的 6 位数字验证码。", "error")
            return redirect(url_for("materials.verify", purchase_id=purchase.id))
        if int(purchase.code_attempts or 0) >= mat.CODE_MAX_ATTEMPTS:
            db.session.rollback()
            flash("验证码错误次数过多，请点“重新发送验证码”。", "error")
            return redirect(url_for("materials.verify", purchase_id=purchase.id))
        if mat.code_expired(purchase):
            db.session.rollback()
            flash("验证码已过期（有效期 5 分钟），请点“重新发送验证码”。", "error")
            return redirect(url_for("materials.verify", purchase_id=purchase.id))
        if not mat.code_matches(purchase, code):
            purchase.code_attempts = int(purchase.code_attempts or 0) + 1
            left = mat.CODE_MAX_ATTEMPTS - purchase.code_attempts
            if left <= 0:
                purchase.code_hash = None
            db.session.commit()
            if left <= 0:
                flash("验证码错误次数过多，该验证码已失效，请点“重新发送验证码”。", "error")
            else:
                flash(f"验证码错误，还可以再试 {left} 次。", "error")
            return redirect(url_for("materials.verify", purchase_id=purchase.id))
        errors = mat.confirm_purchase(purchase)
        if errors:
            purchase = db.session.get(MaterialPurchase, purchase_id)
            purchase.status = "cancelled"
            purchase.code_hash = None
            purchase.note = errors[0][:200]
            purchase.updated_at = local_now()
            db.session.commit()
            for msg in errors:
                flash(msg, "error")
            flash("本次购买未成功，没有扣减库存，请重新选择后提交。", "error")
            return redirect(url_for("materials.buy"))
        db.session.flush()
        record_audit(
            "material.purchase",
            summary=f"确认购买材料：{purchase.items_label}，合计 {purchase.total_label} 元。",
            target_type="material_purchase",
            target_id=purchase.id,
            payload={"items": [[l.name, l.quantity, l.unit_price_cents] for l in purchase.items], "total_cents": purchase.total_cents},
            reversible=False,
            undo_blocked_reason="请由总管理员在材料记录中作废。",
        )
        flash(f"购买成功！合计 {purchase.total_label} 元，比赛后统一结算。", "success")
        return redirect(url_for("materials.buy"))
    return render_template(
        "materials/verify.html",
        purchase=purchase,
        masked_email=mat.mask_email(purchase.email_to),
        seconds_left=mat.code_seconds_left(purchase),
        resend_wait=mat.resend_wait_seconds(purchase),
        attempts_left=max(0, mat.CODE_MAX_ATTEMPTS - int(purchase.code_attempts or 0)) if purchase.code_hash else 0,
        sends_left=max(0, mat.CODE_MAX_SENDS - int(purchase.code_send_count or 0)),
    )


@materials_bp.route("/materials/buy/<int:purchase_id>/resend", methods=["POST"])
@login_required
def resend(purchase_id):
    blocked = _captain_only("材料购买")
    if blocked:
        return blocked
    begin_serialized_write()
    purchase, redirect_response = _own_pending_or_redirect(purchase_id)
    if redirect_response:
        db.session.rollback()
        return redirect_response
    wait = mat.resend_wait_seconds(purchase)
    if wait > 0:
        db.session.rollback()
        flash(f"请 {wait} 秒后再重新发送。", "error")
    elif int(purchase.code_send_count or 0) >= mat.CODE_MAX_SENDS:
        db.session.rollback()
        flash("本次购买的验证码发送次数已用完，请取消后重新提交。", "error")
    elif not _code_limiter_allows(current_user.id):
        db.session.rollback()
        flash("验证码发送过于频繁，请稍后再试。", "error")
    else:
        log = mat.issue_code(purchase)
        if getattr(log, "status", "") == "failed":
            flash("验证码邮件发送失败，请稍后重试，或联系管理员。", "error")
        else:
            flash(f"新的验证码已发送到 {mat.mask_email(purchase.email_to)}，旧验证码作废。", "success")
    return redirect(url_for("materials.verify", purchase_id=purchase_id))


@materials_bp.route("/materials/buy/<int:purchase_id>/cancel", methods=["POST"])
@login_required
def cancel_pending(purchase_id):
    blocked = _captain_only("材料购买")
    if blocked:
        return blocked
    begin_serialized_write()
    purchase, redirect_response = _own_pending_or_redirect(purchase_id)
    if redirect_response:
        db.session.rollback()
        return redirect_response
    purchase.status = "cancelled"
    purchase.code_hash = None
    purchase.updated_at = local_now()
    purchase.note = "队长取消"
    db.session.commit()
    flash("已取消本次购买，没有扣减库存。", "info")
    return redirect(url_for("materials.buy"))


# ================================================================== photos

@materials_bp.route("/materials/media/<filename>")
@login_required
def media(filename):
    name = secure_filename(filename)
    if not name or name != filename:
        return "文件不存在。", 404
    owner_id = None
    claim_row = MaterialClaim.query.filter_by(photo=name).first()
    if claim_row is not None:
        owner_id = claim_row.user_id
    else:
        purchase_row = MaterialPurchase.query.filter_by(photo=name).first()
        if purchase_row is not None:
            owner_id = purchase_row.user_id
    if owner_id is None or (not current_user.is_staff and owner_id != current_user.id):
        return "无权查看该照片。", 403
    path = _upload_dir() / name
    if not path.is_file():
        return "文件不存在。", 404
    return send_file(path)


# ================================================================== staff: records (admins read-only)

PURCHASE_FILTERS = ("confirmed", "unsettled", "settled", "voided", "pending", "cancelled", "all")


@materials_bp.route("/materials/admin")
@reviewer_required
def admin_records():
    tab = request.args.get("tab", "claims")
    if tab not in ("claims", "purchases", "settle"):
        tab = "claims"
    context = {"tab": tab, "free_items": mat.items_of("free", enabled_only=False), "sale_items": mat.items_of("sale", enabled_only=False)}
    if tab == "claims":
        context["stats"] = mat.claim_statistics()
        context["voided_claims"] = (
            MaterialClaim.query.options(selectinload(MaterialClaim.items))
            .filter_by(status="voided").order_by(MaterialClaim.created_at.desc()).all()
        )
    elif tab == "purchases":
        status = request.args.get("status", "confirmed")
        if status not in PURCHASE_FILTERS:
            status = "confirmed"
        query = MaterialPurchase.query.options(selectinload(MaterialPurchase.items), selectinload(MaterialPurchase.user))
        if status == "unsettled":
            query = query.filter(MaterialPurchase.status == "confirmed", MaterialPurchase.settled.is_(False))
        elif status == "settled":
            query = query.filter(MaterialPurchase.status == "confirmed", MaterialPurchase.settled.is_(True))
        elif status != "all":
            query = query.filter(MaterialPurchase.status == status)
        context["status"] = status
        context["purchases"] = query.order_by(MaterialPurchase.created_at.desc()).limit(500).all()
    else:
        context["teams"] = mat.purchase_team_totals()
        context["sold"] = mat.sold_totals()
        context["grand_total"] = format_cents(sum(r["total_cents"] for r in context["teams"]))
        context["grand_unsettled"] = format_cents(sum(r["unsettled_cents"] for r in context["teams"]))
    return render_template("materials/admin.html", **context)


@materials_bp.route("/materials/admin/export")
@reviewer_required
def export():
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5D62")

    def sheet(ws, headers, rows):
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        for row in rows:
            # Keep non-integer rational quantities as text (never float/date cells).
            ws.append([v.numerator if isinstance(v, Fraction) and v.denominator == 1
                       else str(v) if isinstance(v, Fraction) else v for v in row])
        for col in range(1, ws.max_column + 1):
            ws.column_dimensions[get_column_letter(col)].width = 18

    fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""  # noqa: E731
    claims = MaterialClaim.query.options(selectinload(MaterialClaim.items), selectinload(MaterialClaim.user)).order_by(MaterialClaim.created_at.asc()).all()
    ws = wb.active
    ws.title = "材料领取"
    sheet(ws, ["记录号", "领取时间", "队伍", "队长", "学号", "材料", "规格", "数量", "单位", "单价(元)", "合计(元)", "状态", "照片", "IP", "备注"], [
        [c.id, fmt(c.created_at), c.team_name or "", c.user.name, c.user.student_id, line.name, line.spec or "",
         line.quantity, line.unit, 0, 0, c.status_label, c.photo or "", c.ip or "", c.note or ""]
        for c in claims for line in c.items
    ])
    purchases = (
        MaterialPurchase.query.options(selectinload(MaterialPurchase.items), selectinload(MaterialPurchase.user))
        .order_by(MaterialPurchase.created_at.asc()).all()
    )
    sheet(wb.create_sheet("材料购买"), ["记录号", "确认时间", "队伍", "队长", "学号", "材料", "规格", "单价(元)", "数量", "单位", "小计(元)", "订单合计(元)", "状态", "结算", "照片", "IP", "备注"], [
        [p.id, fmt(p.confirmed_at or p.created_at), p.team_name or "", p.user.name, p.user.student_id, line.name, line.spec or "",
         float(line.unit_price_label), line.quantity, line.unit, float(line.subtotal_label), float(p.total_label), p.status_label,
         "已结算" if p.settled else "未结算", p.photo or "", p.ip or "", p.note or ""]
        for p in purchases for line in p.items
    ])
    sheet(wb.create_sheet("结算汇总"), ["队伍", "队长", "学号", "订单数", "购买合计(元)", "应付合计/未结算(元)"], [
        [r["team_name"], r["user"].name if r["user"] else "", r["user"].student_id if r["user"] else "", r["count"],
         float(format_cents(r["total_cents"])), float(format_cents(r["unsettled_cents"]))]
        for r in mat.purchase_team_totals()
    ])
    sheet(wb.create_sheet("库存"), ["类型", "材料", "规格", "单位", "每队份数", "单价(元)", "当前库存", "启用"], [
        ["免费领取" if i.kind == "free" else "购买", i.name, i.spec or "", i.unit, i.per_team_qty if i.kind == "free" else "",
         float(i.unit_price_label) if i.kind == "sale" else "", i.stock, "是" if i.enabled else "否"]
        for i in mat.items_of("free", enabled_only=False) + mat.items_of("sale", enabled_only=False)
    ])
    stats = mat.claim_statistics()
    sheet(wb.create_sheet("领取统计"), ["队伍", "队长", "学号", "领取状态", "领取时间"], [
        [r["user"].team_name or "", r["user"].name, r["user"].student_id,
         "已领取" if r["claim"] else "未领取", fmt(r["claim"].created_at) if r["claim"] else ""]
        for r in stats["rows"]
    ] + [[c.team_name or "", c.user.name, c.user.student_id, "已领取（账号已停用）", fmt(c.created_at)] for c in stats["extra"]])
    sheet(wb.create_sheet("发放汇总"), ["材料", "单位", "已发放合计"], [
        [r["label"], r["unit"], r["quantity"]] for r in stats["totals"]
    ])
    protect_excel_text(wb)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="materials.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ================================================================== super admin: settings

def _parse_item_form(kind: str, item=None):
    form = request.form
    errors = []
    name = (form.get("name") or "").strip()
    spec = (form.get("spec") or "").strip() or None
    unit = (form.get("unit") or "").strip() or "个"
    if not name:
        errors.append("请填写材料名称。")
    elif len(name) > 80:
        errors.append("材料名称不能超过 80 字。")
    if spec and len(spec) > 80:
        errors.append("型号/规格不能超过 80 字。")
    if len(unit) > 16:
        errors.append("单位不能超过 16 字。")
    # An unchanged stored quantity may have a larger denominator after exact
    # arithmetic; accept that canonical value without reapplying input bounds.
    def quantity(field, maximum=mat.MAX_STOCK, allow_zero=False):
        raw = (form.get(field) or "").strip()
        old = getattr(item, field, None) if item is not None else None
        if old is not None and raw == str(old):
            return old
        return parse_quantity(raw, maximum=maximum, allow_zero=allow_zero)
    stock = quantity("stock", allow_zero=True)
    if stock is None:
        errors.append("库存须为 0 到 1000000 之间的数量，支持小数或分数。")
    values = {"name": name, "spec": spec, "unit": unit, "stock": stock,
              "enabled": (form.get("enabled") or "") in {"1", "on", "true"},
              "sort_order": mat.parse_non_negative_int(form.get("sort_order") or "0", maximum=9999) or 0}
    if kind == "free":
        per_team = quantity("per_team_qty", maximum=100000)
        if per_team is None or per_team <= 0:
            errors.append("每队份数须大于 0 且不超过 100000，支持小数或分数。")
        values["per_team_qty"] = per_team or 0
    else:
        price = mat.parse_price_cents(form.get("unit_price"))
        if price is None:
            errors.append("单价须为不小于 0 的金额，最多两位小数（不超过 10000 元）。")
        values["unit_price_cents"] = price or 0
        raw_max = (form.get("max_per_purchase") or "").strip()
        if raw_max:
            max_per = quantity("max_per_purchase", maximum=100000)
            if not max_per:
                errors.append("每次限购须大于 0 且不超过 100000，支持小数或分数；不限购请留空。")
            values["max_per_purchase"] = max_per or None
        else:
            values["max_per_purchase"] = None
    return values, errors


def _item_snapshot(item: MaterialItem) -> dict:
    return {
        "kind": item.kind, "name": item.name, "spec": item.spec, "unit": item.unit, "per_team_qty": item.per_team_qty,
        "unit_price_cents": item.unit_price_cents, "stock": item.stock, "max_per_purchase": item.max_per_purchase,
        "enabled": item.enabled, "sort_order": item.sort_order,
    }


@materials_bp.route("/materials/settings")
@superadmin_required
def settings():
    return render_template(
        "materials/settings.html",
        free_items=mat.items_of("free", enabled_only=False),
        sale_items=mat.items_of("sale", enabled_only=False),
        free_locked=MaterialClaim.query.filter_by(status="active").first() is not None,
    )


@materials_bp.route("/materials/settings/<kind>/new", methods=["POST"])
@superadmin_required
def item_create(kind):
    if kind not in ("free", "sale"):
        abort(404)
    begin_serialized_write()
    values, errors = _parse_item_form(kind)
    if kind == "free" and MaterialClaim.query.filter_by(status="active").first():
        errors.append("已有队伍领取材料，免费清单已锁定，只能调整库存。")
    if not errors and MaterialItem.query.filter_by(kind=kind, name=values["name"], spec=values["spec"]).first():
        errors.append("已存在同名同规格的材料。")
    if errors:
        db.session.rollback()
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("materials.settings", _anchor=kind))
    item = MaterialItem(kind=kind, **values)
    db.session.add(item)
    db.session.flush()
    record_audit("material.item_create", summary=f"新增{'免费领取' if kind == 'free' else '购买'}材料「{item.label}」。",
                 target_type="material_item", target_id=item.id, payload={"after": _item_snapshot(item)})
    flash("已添加材料。", "success")
    return redirect(url_for("materials.settings", _anchor=kind))


@materials_bp.route("/materials/settings/items/<int:item_id>/update", methods=["POST"])
@superadmin_required
def item_update(item_id):
    begin_serialized_write()
    item = db.session.get(MaterialItem, item_id)
    if item is None:
        db.session.rollback()
        flash("材料不存在。", "error")
        return redirect(url_for("materials.settings"))
    values, errors = _parse_item_form(item.kind, item)
    if item.kind == "free" and MaterialClaim.query.filter_by(status="active").first():
        if any(values[key] != getattr(item, key) for key in ("name", "spec", "unit", "per_team_qty", "enabled")):
            errors.append("已有队伍领取材料，免费清单已锁定，只能调整库存。")
    other = MaterialItem.query.filter(
        MaterialItem.kind == item.kind, MaterialItem.name == values["name"], MaterialItem.spec.is_(values["spec"]) if values["spec"] is None else MaterialItem.spec == values["spec"],
        MaterialItem.id != item.id,
    ).first()
    if other:
        errors.append("已存在同名同规格的材料。")
    if errors:
        db.session.rollback()
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("materials.settings", _anchor=item.kind))
    before = _item_snapshot(item)
    for key, value in values.items():
        setattr(item, key, value)
    item.updated_at = local_now()
    db.session.flush()
    record_audit("material.item_update", summary=f"修改材料「{item.label}」（库存 {before['stock']} → {item.stock}）。",
                 target_type="material_item", target_id=item.id, payload={"before": before, "after": _item_snapshot(item)})
    flash("已保存材料信息。", "success")
    return redirect(url_for("materials.settings", _anchor=item.kind))


@materials_bp.route("/materials/settings/items/<int:item_id>/delete", methods=["POST"])
@superadmin_required
def item_delete(item_id):
    begin_serialized_write()
    item = db.session.get(MaterialItem, item_id)
    if item is None:
        db.session.rollback()
        flash("材料不存在。", "error")
        return redirect(url_for("materials.settings"))
    snapshot = _item_snapshot(item)
    if item.kind == "free" and MaterialClaim.query.filter_by(status="active").first():
        db.session.rollback()
        flash("已有队伍领取材料，免费清单已锁定，只能调整库存。", "error")
        return redirect(url_for("materials.settings"))
    kind, label = item.kind, item.label
    # Records keep their own name/spec/price snapshot; only the link is cleared.
    MaterialClaimItem.query.filter_by(item_id=item.id).update({"item_id": None})
    MaterialPurchaseItem.query.filter_by(item_id=item.id).update({"item_id": None})
    db.session.delete(item)
    db.session.flush()
    record_audit("material.item_delete", summary=f"删除材料「{label}」。", target_type="material_item",
                 target_id=item_id, payload={"before": snapshot})
    flash("已删除材料，历史记录中的名称和价格保持不变。", "success")
    return redirect(url_for("materials.settings", _anchor=kind))


# ================================================================== super admin: correct records

def _apply_line_changes(lines, *, active: bool) -> tuple[list[dict], list[str]]:
    """Read qty_<line_id> fields; adjust stock by the difference for active records."""
    changes, errors = [], []
    for line in list(lines):
        price_raw = request.form.get(f"price_{line.id}")
        if isinstance(line, MaterialPurchaseItem) and price_raw is not None:
            price = mat.parse_price_cents(price_raw)
            if price is None:
                errors.append("单价须为不小于 0 的金额，最多两位小数（不超过 10000 元）。")
            elif price != line.unit_price_cents:
                if not active:
                    errors.append("只有有效记录可以修改数量；已作废或已取消的记录只能修改备注。")
                else:
                    changes.append({"name": line.label, "price_before": line.unit_price_cents, "price_after": price})
                    line.unit_price_cents = price
        raw = request.form.get(f"qty_{line.id}")
        if raw is None:
            continue
        qty = parse_quantity(raw, maximum=100000, allow_zero=True)
        if qty is None:
            errors.append(QUANTITY_ERROR)
            continue
        if qty == line.quantity:
            continue
        if not active:
            errors.append("只有有效记录可以修改数量；已作废或已取消的记录只能修改备注。")
            break
        delta = qty - line.quantity
        if delta > 0:
            if line.item_id is None or not mat.take_stock(line.item_id, delta):
                errors.append(f"「{line.label}」库存不足，不能增加 {delta}{line.unit}。")
                continue
        else:
            mat.give_back_stock(line.item_id, -delta)
        changes.append({"name": line.label, "before": line.quantity, "after": qty})
        line.quantity = qty
    return changes, errors


def _redirect_admin(tab: str):
    target = request.form.get("next") or ""
    if target.startswith("/materials/admin"):
        return redirect(target)
    return redirect(url_for("materials.admin_records", tab=tab))


@materials_bp.route("/materials/admin/claims/<int:claim_id>/edit", methods=["GET", "POST"])
@superadmin_required
def claim_edit(claim_id):
    if request.method == "POST":
        begin_serialized_write()
    record = db.session.get(MaterialClaim, claim_id)
    if record is None:
        db.session.rollback()
        flash("领取记录不存在。", "error")
        return redirect(url_for("materials.admin_records", tab="claims"))
    if request.method == "POST":
        changes, errors = _apply_line_changes(record.items, active=record.status == "active")
        note = (request.form.get("note") or "").strip()[:200] or None
        if errors:
            db.session.rollback()
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("materials.claim_edit", claim_id=claim_id))
        for line in [l for l in record.items if l.quantity == 0]:
            record.items.remove(line)
        if not record.items:
            db.session.rollback()
            flash("至少保留一项材料；如需取消整条领取记录，请使用“作废”。", "error")
            return redirect(url_for("materials.claim_edit", claim_id=claim_id))
        old_note = record.note
        record.note = note
        record.updated_at = local_now()
        record.updated_by_id = current_user.id
        db.session.flush()
        mat.expire_session_items()
        if changes or old_note != note:
            record_audit("material.claim_edit", summary=f"修改 {record.user.name} 的材料领取记录 #{record.id}。",
                         target_type="material_claim", target_id=record.id,
                         payload={"changes": changes, "note_before": old_note, "note_after": note})
        else:
            db.session.commit()
        flash("已保存领取记录。", "success")
        return redirect(url_for("materials.admin_records", tab="claims"))
    return render_template("materials/edit.html", record=record, kind="claim")


@materials_bp.route("/materials/admin/purchases/<int:purchase_id>/edit", methods=["GET", "POST"])
@superadmin_required
def purchase_edit(purchase_id):
    if request.method == "POST":
        begin_serialized_write()
    record = db.session.get(MaterialPurchase, purchase_id)
    if record is None:
        db.session.rollback()
        flash("购买记录不存在。", "error")
        return redirect(url_for("materials.admin_records", tab="purchases"))
    if request.method == "POST":
        before_total = record.total_cents
        changes, errors = _apply_line_changes(record.items, active=record.status == "confirmed")
        note = (request.form.get("note") or "").strip()[:200] or None
        if errors:
            db.session.rollback()
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("materials.purchase_edit", purchase_id=purchase_id))
        for line in [l for l in record.items if l.quantity == 0]:
            record.items.remove(line)
        if not record.items:
            db.session.rollback()
            flash("至少保留一项材料；如需取消整笔购买，请使用“作废”。", "error")
            return redirect(url_for("materials.purchase_edit", purchase_id=purchase_id))
        record.recompute_total()
        # A corrected amount must be reviewed for settlement again.
        was_settled = record.settled
        if changes:
            record.settled = False
            record.settled_at = record.settled_by_id = None
        old_note = record.note
        record.note = note
        record.updated_at = local_now()
        record.updated_by_id = current_user.id
        db.session.flush()
        mat.expire_session_items()
        if changes or old_note != note:
            record_audit("material.purchase_edit",
                         summary=f"修改 {record.user.name} 的材料购买记录 #{record.id}（合计 {format_cents(before_total)} → {record.total_label} 元）。",
                         target_type="material_purchase", target_id=record.id,
                         payload={"changes": changes, "total_before": before_total, "total_after": record.total_cents,
                                  "note_before": old_note, "note_after": note, "was_settled": was_settled})
        else:
            db.session.commit()
        flash("已保存购买记录。", "success")
        return redirect(url_for("materials.admin_records", tab="purchases"))
    return render_template("materials/edit.html", record=record, kind="purchase")


@materials_bp.route("/materials/admin/<kind>/<int:record_id>/void", methods=["POST"])
@superadmin_required
def record_void(kind, record_id):
    if kind not in ("claims", "purchases"):
        abort(404)
    begin_serialized_write()
    model = MaterialClaim if kind == "claims" else MaterialPurchase
    record = db.session.get(model, record_id)
    live_status = "active" if kind == "claims" else "confirmed"
    if record is None or record.status not in (live_status, "pending"):
        db.session.rollback()
        flash("该记录不存在或已失效。", "error")
        return _redirect_admin(kind)
    returned = record.status == live_status
    if returned:
        for line in record.items:
            mat.give_back_stock(line.item_id, line.quantity)
    record.status = "voided" if returned else "cancelled"
    if kind == "purchases":
        record.code_hash = None
    record.updated_at = local_now()
    record.updated_by_id = current_user.id
    reason = (request.form.get("reason") or "").strip()[:200]
    if reason:
        record.note = reason
    db.session.flush()
    mat.expire_session_items()
    record_audit(f"material.{kind[:-1]}_void", summary=f"作废 {record.user.name} 的材料{'领取' if kind == 'claims' else '购买'}记录 #{record.id}，库存已退回。",
                 target_type=f"material_{kind[:-1]}", target_id=record.id,
                 payload={"items": [[l.name, l.quantity] for l in record.items], "stock_returned": returned, "reason": reason})
    flash("已作废该记录，库存已退回。" + ("该队可以重新领取。" if kind == "claims" else ""), "success")
    return _redirect_admin(kind)


@materials_bp.route("/materials/admin/<kind>/<int:record_id>/delete", methods=["POST"])
@superadmin_required
def record_delete(kind, record_id):
    if kind not in ("claims", "purchases"):
        abort(404)
    begin_serialized_write()
    model = MaterialClaim if kind == "claims" else MaterialPurchase
    record = db.session.get(model, record_id)
    if record is None:
        db.session.rollback()
        flash("记录不存在。", "error")
        return _redirect_admin(kind)
    live_status = "active" if kind == "claims" else "confirmed"
    returned = record.status == live_status
    if returned:
        for line in record.items:
            mat.give_back_stock(line.item_id, line.quantity)
    snapshot = {
        "user": record.user.student_id, "team": record.team_name, "status": record.status,
        "items": [{"name": l.name, "spec": l.spec, "unit": l.unit, "quantity": l.quantity,
                   "unit_price_cents": getattr(l, "unit_price_cents", 0)} for l in record.items],
        "created_at": record.created_at.isoformat(timespec="seconds"), "photo": record.photo,
        "ip": record.ip, "note": record.note,
    }
    if kind == "purchases":
        snapshot["total_cents"] = record.total_cents
        snapshot["settled"] = record.settled
    name, photo = record.user.name, record.photo
    db.session.delete(record)
    db.session.flush()
    mat.expire_session_items()
    record_audit(f"material.{kind[:-1]}_delete", summary=f"删除 {name} 的材料{'领取' if kind == 'claims' else '购买'}记录 #{record_id}。",
                 target_type=f"material_{kind[:-1]}", target_id=record_id, payload={"before": snapshot, "stock_returned": returned})
    _remove_photo(photo)
    flash("已删除该记录。" + ("库存已退回。" if returned else ""), "success")
    return _redirect_admin(kind)


@materials_bp.route("/materials/admin/purchases/<int:purchase_id>/settle", methods=["POST"])
@superadmin_required
def purchase_settle(purchase_id):
    begin_serialized_write()
    record = db.session.get(MaterialPurchase, purchase_id)
    if record is None or record.status != "confirmed":
        flash("只有已确认的购买可以标记结算。", "error")
        return _redirect_admin("purchases")
    settled = request.form.get("settled") == "1"
    if record.settled != settled:
        record.settled = settled
        record.settled_at = local_now() if settled else None
        record.settled_by_id = current_user.id if settled else None
        db.session.flush()
        record_audit("material.settle", summary=f"将 {record.user.name} 的购买记录 #{record.id}（{record.total_label} 元）标记为{'已结算' if settled else '未结算'}。",
                     target_type="material_purchase", target_id=record.id, payload={"settled": settled})
    flash("已标记为已结算。" if settled else "已改回未结算。", "success")
    return _redirect_admin("purchases")


@materials_bp.route("/materials/admin/teams/<int:user_id>/settle", methods=["POST"])
@superadmin_required
def team_settle(user_id):
    begin_serialized_write()
    user = db.session.get(User, user_id)
    rows = MaterialPurchase.query.filter_by(user_id=user_id, status="confirmed", settled=False).all()
    if user is None or not rows:
        flash("该队没有未结算的购买记录。", "info")
        return redirect(url_for("materials.admin_records", tab="settle"))
    now = local_now()
    for row in rows:
        row.settled = True
        row.settled_at = now
        row.settled_by_id = current_user.id
    amount = format_cents(sum(r.total_cents for r in rows))
    db.session.flush()
    record_audit("material.settle_team", summary=f"{user.team_name or user.name} 的 {len(rows)} 笔材料购买（{amount} 元）已全部标记为已结算。",
                 target_type="user", target_id=user.id, payload={"purchase_ids": [r.id for r in rows]})
    flash(f"已将 {user.team_name or user.name} 的 {len(rows)} 笔购买（{amount} 元）标记为已结算。", "success")
    return redirect(url_for("materials.admin_records", tab="settle"))
