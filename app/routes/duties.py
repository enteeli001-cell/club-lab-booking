from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import db
from app.activity import record_audit
from app.decorators import superadmin_required
from app.models import WEEKDAY_LABELS, DutyShift, Lab, User
from app.utils import parse_date, parse_time

duties_bp = Blueprint("duties", __name__, url_prefix="/duties")


def _staff_users():
    return User.query.filter(User.role.in_(("admin", "superadmin"))).order_by(User.role.asc(), User.student_id.asc()).all()


def _form(shift=None):
    if shift is None:
        return {
            "lab_id": "",
            "admin_id": "",
            "mode": "date",
            "specific_date": "",
            "weekday": "0",
            "start_time": "08:00",
            "end_time": "18:00",
        }
    return {
        "lab_id": str(shift.lab_id),
        "admin_id": str(shift.admin_id),
        "mode": "week" if shift.specific_date is None else "date",
        "specific_date": shift.specific_date.isoformat() if shift.specific_date else "",
        "weekday": "" if shift.weekday is None else str(shift.weekday),
        "start_time": shift.start_time.strftime("%H:%M"),
        "end_time": shift.end_time.strftime("%H:%M"),
    }


def _parse(form):
    errors = []
    form["lab_id"] = (request.form.get("lab_id") or "").strip()
    form["admin_id"] = (request.form.get("admin_id") or "").strip()
    form["mode"] = (request.form.get("mode") or "date").strip()
    form["specific_date"] = (request.form.get("specific_date") or "").strip()
    form["weekday"] = (request.form.get("weekday") or "").strip()
    form["start_time"] = (request.form.get("start_time") or "").strip()
    form["end_time"] = (request.form.get("end_time") or "").strip()

    lab = db.session.get(Lab, int(form["lab_id"])) if form["lab_id"].isdigit() else None
    admin = db.session.get(User, int(form["admin_id"])) if form["admin_id"].isdigit() else None
    start = parse_time(form["start_time"])
    end = parse_time(form["end_time"])
    if lab is None:
        errors.append("请选择实验室。")
    if admin is None or not admin.is_staff:
        errors.append("请选择值班管理员。")
    if start is None or end is None:
        errors.append("请填写值班起止时间。")
    elif start >= end:
        errors.append("值班结束时间必须晚于开始时间。")

    specific_date = None
    weekday = None
    if form["mode"] == "week":
        if not form["weekday"].isdigit() or not (0 <= int(form["weekday"]) <= 6):
            errors.append("请选择星期。")
        else:
            weekday = int(form["weekday"])
    else:
        specific_date = parse_date(form["specific_date"])
        if specific_date is None:
            errors.append("请选择值班日期。")
    return lab, admin, specific_date, weekday, start, end, errors


@duties_bp.route("/")
@superadmin_required
def list_duties():
    shifts = DutyShift.query.order_by(DutyShift.specific_date.asc(), DutyShift.weekday.asc(), DutyShift.start_time.asc()).all()
    return render_template("duties/list.html", shifts=shifts, weekday_labels=WEEKDAY_LABELS)


@duties_bp.route("/new", methods=["GET", "POST"])
@superadmin_required
def create_duty():
    form = _form()
    if request.method == "POST":
        lab, admin, specific_date, weekday, start, end, errors = _parse(form)
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            db.session.add(
                DutyShift(
                    lab=lab,
                    admin=admin,
                    specific_date=specific_date,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                )
            )
            db.session.commit()
            record_audit("duty.create", summary="添加值班安排。", target_type="duty", reversible=False, undo_blocked_reason="请在值班安排中删除。")
            flash("已添加值班安排。", "success")
            return redirect(url_for("duties.list_duties"))
    return render_template(
        "duties/form.html",
        form=form,
        shift=None,
        labs=Lab.query.order_by(Lab.name.asc()).all(),
        staff=_staff_users(),
        weekday_labels=WEEKDAY_LABELS,
    )


@duties_bp.route("/<int:shift_id>/edit", methods=["GET", "POST"])
@superadmin_required
def edit_duty(shift_id):
    shift = db.session.get(DutyShift, shift_id)
    if shift is None:
        flash("值班安排不存在。", "error")
        return redirect(url_for("duties.list_duties"))
    form = _form(shift)
    if request.method == "POST":
        lab, admin, specific_date, weekday, start, end, errors = _parse(form)
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            shift.lab = lab
            shift.admin = admin
            shift.specific_date = specific_date
            shift.weekday = weekday
            shift.start_time = start
            shift.end_time = end
            db.session.commit()
            record_audit("duty.update", summary="更新值班安排。", target_type="duty", target_id=shift.id, reversible=False, undo_blocked_reason="请在值班安排中再编辑。")
            flash("已更新值班安排。", "success")
            return redirect(url_for("duties.list_duties"))
    return render_template(
        "duties/form.html",
        form=form,
        shift=shift,
        labs=Lab.query.order_by(Lab.name.asc()).all(),
        staff=_staff_users(),
        weekday_labels=WEEKDAY_LABELS,
    )


@duties_bp.route("/<int:shift_id>/delete", methods=["POST"])
@superadmin_required
def delete_duty(shift_id):
    shift = db.session.get(DutyShift, shift_id)
    if shift is None:
        flash("值班安排不存在。", "error")
    else:
        db.session.delete(shift)
        db.session.commit()
        record_audit("duty.delete", summary="删除值班安排。", target_type="duty", reversible=False, undo_blocked_reason="删除后请重新添加。")
        flash("已删除值班安排。", "success")
    return redirect(url_for("duties.list_duties"))
