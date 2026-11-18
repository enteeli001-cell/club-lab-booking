from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import db
from app.activity import record_audit
from app.decorators import lab_manager_required
from app.models import Booking, DutyShift, Lab, ToolLoan
from app.utils import parse_time

labs_bp = Blueprint("labs", __name__, url_prefix="/labs")


def _form_from_lab(lab=None):
    if lab is None:
        return {
            "name": "",
            "capacity": "10",
            "open_start": "08:00",
            "open_end": "22:00",
            "enabled": True,
            "contact_name": "",
            "contact_phone": "",
            "geo_lat": "",
            "geo_lng": "",
            "geo_radius_m": "30",
            "geo_required": False,
        }
    return {
        "name": lab.name,
        "capacity": str(lab.capacity),
        "open_start": lab.open_start.strftime("%H:%M"),
        "open_end": lab.open_end.strftime("%H:%M"),
        "enabled": lab.enabled,
        "contact_name": lab.contact_name or "",
        "contact_phone": lab.contact_phone or "",
        "geo_lat": "" if lab.geo_lat is None else f"{lab.geo_lat:.6f}",
        "geo_lng": "" if lab.geo_lng is None else f"{lab.geo_lng:.6f}",
        "geo_radius_m": str(lab.geo_radius_m or 30),
        "geo_required": bool(lab.geo_required),
    }


def _read_geo(form):
    for key in ("geo_lat", "geo_lng", "geo_radius_m"):
        form[key] = (request.form.get(key) or "").strip()
    form["geo_required"] = request.form.get("geo_required") == "on"


def _validate_geo(form):
    import math

    errors = []
    lat_raw, lng_raw = form.get("geo_lat", ""), form.get("geo_lng", "")
    lat = lng = None
    if lat_raw or lng_raw:
        try:
            lat, lng = float(lat_raw), float(lng_raw)
            if not (math.isfinite(lat) and math.isfinite(lng)):
                raise ValueError
        except ValueError:
            errors.append("纬度和经度必须同时填写，并且是有效的数字。")
            lat = lng = None
        else:
            if not -90 <= lat <= 90:
                errors.append("纬度须在 -90 到 90 之间。")
            if not -180 <= lng <= 180:
                errors.append("经度须在 -180 到 180 之间。")
    form["_geo_lat"], form["_geo_lng"] = (lat, lng) if not errors else (None, None)
    try:
        radius = int(form.get("geo_radius_m") or "30")
        if not 5 <= radius <= 1000:
            raise ValueError
    except ValueError:
        errors.append("签到半径须是 5 到 1000 米之间的整数。")
        radius = 30
    form["_geo_radius_m"] = radius
    if form.get("geo_required") and (form["_geo_lat"] is None or form["_geo_lng"] is None) and not errors:
        errors.append("开启位置签到前，请先填写实验室的经纬度（可点“使用当前位置”）。")
    return errors


def _validate_lab_form(form, lab=None):
    errors = []
    name = form["name"]
    if not name:
        errors.append("请填写实验室名称。")
    else:
        query = Lab.query.filter_by(name=name)
        if lab is not None:
            query = query.filter(Lab.id != lab.id)
        if query.first():
            errors.append("已存在同名实验室。")

    try:
        capacity = int(form["capacity"])
        if capacity < 1:
            raise ValueError
        form["_capacity"] = capacity
    except (TypeError, ValueError):
        errors.append("容量须为大于 0 的整数。")

    start = parse_time(form["open_start"])
    end = parse_time(form["open_end"])
    form["_open_start"] = start
    form["_open_end"] = end
    if start is None or end is None:
        errors.append("请填写有效的开放时间（如 08:00）。")
    elif start >= end:
        errors.append("开放结束时间必须晚于开始时间。")
    errors.extend(_validate_geo(form))
    from app.utils import is_mobile_phone
    phone = (form.get("contact_phone") or "").strip()
    if phone and not is_mobile_phone(phone):
        errors.append("实验室联系电话须为 11 位手机号。")
    return errors


@labs_bp.route("/")
@lab_manager_required
def list_labs():
    labs = Lab.query.order_by(Lab.name.asc()).all()
    return render_template("labs/list.html", labs=labs)


@labs_bp.route("/new", methods=["GET", "POST"])
@lab_manager_required
def create_lab():
    form = _form_from_lab()
    if request.method == "POST":
        form["name"] = (request.form.get("name") or "").strip()
        form["capacity"] = (request.form.get("capacity") or "").strip()
        form["open_start"] = (request.form.get("open_start") or "").strip()
        form["open_end"] = (request.form.get("open_end") or "").strip()
        form["enabled"] = request.form.get("enabled") == "on"
        form["contact_name"] = (request.form.get("contact_name") or "").strip()
        form["contact_phone"] = (request.form.get("contact_phone") or "").strip()
        _read_geo(form)
        errors = _validate_lab_form(form)
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            lab = Lab(
                name=form["name"],
                capacity=form["_capacity"],
                open_start=form["_open_start"],
                open_end=form["_open_end"],
                enabled=form["enabled"],
                contact_name=form.get("contact_name") or None,
                contact_phone=form.get("contact_phone") or None,
                geo_lat=form["_geo_lat"],
                geo_lng=form["_geo_lng"],
                geo_radius_m=form["_geo_radius_m"],
                geo_required=bool(form["geo_required"] and form["_geo_lat"] is not None),
            )
            db.session.add(lab)
            db.session.commit()
            record_audit("lab.create", summary=f"添加实验室 {lab.name}。", target_type="lab", target_id=lab.id, reversible=False, undo_blocked_reason="请在实验室管理中编辑或删除。")
            flash(f"已添加实验室：{lab.name}。", "success")
            return redirect(url_for("labs.list_labs"))
    return render_template("labs/form.html", form=form, lab=None)


@labs_bp.route("/<int:lab_id>/edit", methods=["GET", "POST"])
@lab_manager_required
def edit_lab(lab_id):
    lab = db.session.get(Lab, lab_id)
    if lab is None:
        flash("实验室不存在。", "error")
        return redirect(url_for("labs.list_labs"))
    form = _form_from_lab(lab)
    if request.method == "POST":
        form["name"] = (request.form.get("name") or "").strip()
        form["capacity"] = (request.form.get("capacity") or "").strip()
        form["open_start"] = (request.form.get("open_start") or "").strip()
        form["open_end"] = (request.form.get("open_end") or "").strip()
        form["enabled"] = request.form.get("enabled") == "on"
        form["contact_name"] = (request.form.get("contact_name") or "").strip()
        form["contact_phone"] = (request.form.get("contact_phone") or "").strip()
        _read_geo(form)
        errors = _validate_lab_form(form, lab=lab)
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            lab.name = form["name"]
            lab.capacity = form["_capacity"]
            lab.open_start = form["_open_start"]
            lab.open_end = form["_open_end"]
            lab.enabled = form["enabled"]
            lab.contact_name = form.get("contact_name") or None
            lab.contact_phone = form.get("contact_phone") or None
            old_geo = (lab.geo_lat, lab.geo_lng, lab.geo_radius_m, lab.geo_required)
            lab.geo_lat, lab.geo_lng = form["_geo_lat"], form["_geo_lng"]
            lab.geo_radius_m = form["_geo_radius_m"]
            lab.geo_required = bool(form["geo_required"] and form["_geo_lat"] is not None)
            geo_note = "（位置签到设置已修改）" if old_geo != (lab.geo_lat, lab.geo_lng, lab.geo_radius_m, lab.geo_required) else ""
            db.session.commit()
            record_audit("lab.update", summary=f"更新实验室 {lab.name}。{geo_note}", target_type="lab", target_id=lab.id, reversible=False, undo_blocked_reason="请在实验室管理中再编辑。")
            flash(f"已更新实验室：{lab.name}。", "success")
            return redirect(url_for("labs.list_labs"))
    return render_template("labs/form.html", form=form, lab=lab)


@labs_bp.route("/<int:lab_id>/delete", methods=["POST"])
@lab_manager_required
def delete_lab(lab_id):
    lab = db.session.get(Lab, lab_id)
    if lab is None:
        flash("实验室不存在。", "error")
        return redirect(url_for("labs.list_labs"))
    active = Booking.query.filter(
        Booking.lab_id == lab.id,
        Booking.status.in_(("pending", "approved")),
    ).count()
    if active:
        flash("该实验室仍有待审核或已通过的预约，不能删除。可先停用。", "error")
        return redirect(url_for("labs.list_labs"))
    if ToolLoan.query.filter_by(lab_id=lab.id).count():
        flash("该实验室仍有工具借用记录，不能删除。可先停用。", "error")
        return redirect(url_for("labs.list_labs"))
    if Booking.query.filter_by(lab_id=lab.id).first():
        flash("该实验室仍有关联预约记录，不能删除。可先停用。", "error")
        return redirect(url_for("labs.list_labs"))
    DutyShift.query.filter_by(lab_id=lab.id).delete()
    name = lab.name
    db.session.delete(lab)
    db.session.commit()
    record_audit("lab.delete", summary=f"删除实验室 {name}。", target_type="lab", reversible=False, undo_blocked_reason="实验室删除后无法自动恢复。")
    flash(f"已删除实验室：{name}。", "success")
    return redirect(url_for("labs.list_labs"))
