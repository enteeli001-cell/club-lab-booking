from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.transactions import begin_serialized_write
from app.activity import record_audit
from app.models import Booking, Lab
from app.notices import BOOKING_CHANGE_LOCKED_NOTICE
from app.queries import booking_list_options
from app.utils import (
    apply_booking_companions,
    collect_booking_companions,
    find_booking_contacts,
    find_conflicts,
    format_contacts_label,
    intervals_overlap,
    safe_local_url,
    is_email_address,
    is_mobile_phone,
    parse_date,
    parse_time,
    time_slots,
    today,
    validate_booking,
    week_bounds,
)

bookings_bp = Blueprint("bookings", __name__)


def _labs():
    return Lab.query.filter_by(enabled=True).order_by(Lab.name.asc()).all()


def _default_lab_id(labs, current):
    if current:
        return current
    if len(labs) == 1:
        return str(labs[0].id)
    return ""


@bookings_bp.route("/bookings")
@login_required
def my_bookings():
    bookings = (
        Booking.query.options(*booking_list_options())
        .filter_by(user_id=current_user.id)
        .order_by(Booking.date.desc(), Booking.start_time.desc())
        .all()
    )
    return render_template("bookings/list.html", bookings=bookings)


@bookings_bp.route("/bookings/new", methods=["GET", "POST"])
@login_required
def create_booking():
    if not current_user.can_book:
        flash("当前账号无权提交测试预约。", "error")
        return redirect(url_for("main.index"))
    labs = _labs()
    form = {
        "lab_id": _default_lab_id(labs, request.values.get("lab_id", "")),
        "date": request.values.get("date", today().isoformat()),
        "start_time": request.values.get("start_time", "09:00"),
        "end_time": request.values.get("end_time", "10:00"),
        "purpose": request.values.get("purpose", "测试预约"),
        "captain_phone": request.values.get("captain_phone", current_user.last_phone or ""),
        "captain_email": request.values.get("captain_email", current_user.last_email or ""),
        "include_other": False,
        "other_rows": [{"name": "", "student_id": "", "contact": ""}],
        "selected_members": [],
    }

    if request.method == "POST":
        form["lab_id"] = (request.form.get("lab_id") or "").strip()
        form["date"] = (request.form.get("date") or "").strip()
        form["start_time"] = (request.form.get("start_time") or "").strip()
        form["end_time"] = (request.form.get("end_time") or "").strip()
        form["purpose"] = (request.form.get("purpose") or "").strip()
        form["captain_phone"] = (request.form.get("captain_phone") or "").strip()
        form["captain_email"] = (request.form.get("captain_email") or "").strip()
        form["include_other"] = (request.form.get("include_other") or "").strip().lower() in {
            "on",
            "1",
            "true",
            "yes",
        }
        form["selected_members"] = [str(v) for v in request.form.getlist("companion_member")]
        names = request.form.getlist("other_name")
        sids = request.form.getlist("other_student_id")
        contacts = request.form.getlist("other_contact")
        rows = []
        for i in range(max(len(names), len(sids), len(contacts), 1)):
            rows.append(
                {
                    "name": names[i] if i < len(names) else "",
                    "student_id": sids[i] if i < len(sids) else "",
                    "contact": contacts[i] if i < len(contacts) else "",
                }
            )
        form["other_rows"] = rows or [{"name": "", "student_id": "", "contact": ""}]

        lab = db.session.get(Lab, int(form["lab_id"])) if form["lab_id"].isdigit() else None
        booking_date = parse_date(form["date"])
        start = parse_time(form["start_time"])
        end = parse_time(form["end_time"])

        errors = []
        if lab is None:
            errors.append("请选择实验室。")
        if not form["purpose"]:
            errors.append("请填写用途。")
        elif len(form["purpose"]) > 200:
            errors.append("用途不能超过 200 字。")
        if not is_mobile_phone(form["captain_phone"]):
            errors.append("请填写有效的队长本人手机号。")
        if not is_email_address(form["captain_email"]):
            errors.append("请填写有效的队长邮箱。")

        companions, companion_errors = collect_booking_companions(request.form, current_user)
        errors.extend(companion_errors)
        used_companion_fields = bool(
            companions
            or form["include_other"]
            or request.form.getlist("companion_member")
            or request.form.getlist("other_name")
        )
        if used_companion_fields:
            people_count = 1 + len(companions)
        else:
            try:
                people_count = int((request.form.get("people_count") or "1").strip())
            except (TypeError, ValueError):
                people_count = None
        errors.extend(validate_booking(lab, booking_date, start, end, people_count))

        pending_conflicts = []
        if lab and booking_date and start and end:
            _approved, pending_conflicts = find_conflicts(lab.id, booking_date, start, end)

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            booking = Booking(
                lab=lab,
                user=current_user,
                date=booking_date,
                start_time=start,
                end_time=end,
                purpose=form["purpose"],
                people_count=people_count,
                captain_phone=form["captain_phone"],
                captain_email=form["captain_email"],
                status="pending",
            )
            db.session.add(booking)
            db.session.flush()
            apply_booking_companions(booking, companions)
            current_user.last_phone = form["captain_phone"]
            current_user.last_email = form["captain_email"]
            db.session.commit()
            record_audit("booking.create", summary=f"提交测试申请：{booking.lab.name} {booking.date} {booking.time_range_label}。", target_type="booking", target_id=booking.id, reversible=False, undo_blocked_reason="请在申请记录中自行取消或联系管理员。")
            if pending_conflicts:
                others = "、".join(f"{b.user.name} {b.time_range_label}" for b in pending_conflicts)
                flash(f"提示：该时段与其他待审核预约重叠超过 30 分钟（{others}），审核时以先通过者为准。", "warning")
            return redirect(url_for("bookings.submitted", booking_id=booking.id))

    return render_template("bookings/form.html", form=form, labs=labs, teammates=current_user.teammates)


@bookings_bp.route("/bookings/<int:booking_id>/submitted")
@login_required
def submitted(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_id != current_user.id:
        flash("只能查看自己的预约。", "error")
        return redirect(url_for("bookings.my_bookings"))
    return render_template("bookings/submitted.html", booking=booking)


def _records_redirect():
    target = request.form.get("next") or request.args.get("next") or ""
    if safe_local_url(target):
        return redirect(target)
    if current_user.is_regular_user:
        return redirect(url_for("records.index", tab="bookings"))
    return redirect(url_for("bookings.my_bookings"))


def _locked_message(booking) -> str:
    contacts = format_contacts_label(find_booking_contacts(booking))
    return f"{BOOKING_CHANGE_LOCKED_NOTICE} 值班或可联系管理员：{contacts}"


def _fill_form_from_booking(booking):
    others = [c for c in booking.companions if c.source == "other"]
    return {
        "lab_id": str(booking.lab_id),
        "date": booking.date.isoformat(),
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
        "purpose": booking.purpose,
        "captain_phone": booking.captain_phone or current_user.last_phone or "",
        "captain_email": booking.captain_email or current_user.last_email or "",
        "include_other": bool(others),
        "other_rows": [
            {"name": c.name, "student_id": c.student_id, "contact": c.contact or ""}
            for c in others
        ] or [{"name": "", "student_id": "", "contact": ""}],
        "selected_members": [str(c.team_member_id) for c in booking.companions if c.team_member_id],
    }


def _parse_booking_post(form_state):
    labs = _labs()
    form_state["lab_id"] = (request.form.get("lab_id") or "").strip()
    form_state["date"] = (request.form.get("date") or "").strip()
    form_state["start_time"] = (request.form.get("start_time") or "").strip()
    form_state["end_time"] = (request.form.get("end_time") or "").strip()
    form_state["purpose"] = (request.form.get("purpose") or "").strip()
    form_state["captain_phone"] = (request.form.get("captain_phone") or "").strip()
    form_state["captain_email"] = (request.form.get("captain_email") or "").strip()
    form_state["include_other"] = (request.form.get("include_other") or "").strip().lower() in {
        "on", "1", "true", "yes",
    }
    form_state["selected_members"] = [str(v) for v in request.form.getlist("companion_member")]
    names = request.form.getlist("other_name")
    sids = request.form.getlist("other_student_id")
    contacts = request.form.getlist("other_contact")
    rows = []
    for i in range(max(len(names), len(sids), len(contacts), 1)):
        rows.append({
            "name": names[i] if i < len(names) else "",
            "student_id": sids[i] if i < len(sids) else "",
            "contact": contacts[i] if i < len(contacts) else "",
        })
    form_state["other_rows"] = rows or [{"name": "", "student_id": "", "contact": ""}]
    lab = db.session.get(Lab, int(form_state["lab_id"])) if form_state["lab_id"].isdigit() else None
    booking_date = parse_date(form_state["date"])
    start = parse_time(form_state["start_time"])
    end = parse_time(form_state["end_time"])
    return labs, lab, booking_date, start, end


@bookings_bp.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@login_required
def edit_booking(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_id != current_user.id:
        flash("只能修改自己的预约。", "error")
        return _records_redirect()
    if booking.status not in ("pending", "approved"):
        flash("该预约当前状态不可修改。", "error")
        return _records_redirect()
    if not booking.can_self_change():
        flash(_locked_message(booking), "error")
        return _records_redirect()

    labs = _labs()
    form = _fill_form_from_booking(booking)
    if request.method == "POST":
        labs, lab, booking_date, start, end = _parse_booking_post(form)
        errors = []
        if lab is None:
            errors.append("请选择实验室。")
        if not form["purpose"]:
            errors.append("请填写用途。")
        elif len(form["purpose"]) > 200:
            errors.append("用途不能超过 200 字。")
        if not is_mobile_phone(form["captain_phone"]):
            errors.append("请填写有效的队长本人手机号。")
        if not is_email_address(form["captain_email"]):
            errors.append("请填写有效的队长邮箱。")
        companions, companion_errors = collect_booking_companions(request.form, current_user)
        errors.extend(companion_errors)
        used_companion_fields = bool(
            companions or form["include_other"] or request.form.getlist("companion_member") or request.form.getlist("other_name")
        )
        if used_companion_fields:
            people_count = 1 + len(companions)
        else:
            try:
                people_count = int((request.form.get("people_count") or str(booking.people_count)).strip())
            except (TypeError, ValueError):
                people_count = booking.people_count
        errors.extend(validate_booking(lab, booking_date, start, end, people_count, exclude_id=booking.id))
        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            booking.lab = lab
            booking.date = booking_date
            booking.start_time = start
            booking.end_time = end
            booking.purpose = form["purpose"]
            booking.people_count = people_count
            booking.captain_phone = form["captain_phone"]
            booking.captain_email = form["captain_email"]
            booking.status = "pending"
            booking.reviewed_by_id = None
            booking.reviewed_at = None
            booking.reject_reason = None
            # A rescheduled booking must be re-reminded and re-checked-in after re-approval.
            booking.reminder_sent_at = None
            booking.reminder_attempts = 0
            booking.checked_in_at = None
            booking.checkin_ip = None
            booking.checkin_ua = None
            booking.pledge_accepted_at = None
            booking.checkin_lat = booking.checkin_lng = None
            booking.checkin_accuracy = booking.checkin_distance_m = None
            apply_booking_companions(booking, companions)
            current_user.last_phone = form["captain_phone"]
            current_user.last_email = form["captain_email"]
            db.session.commit()
            record_audit("booking.edit", summary=f"修改测试申请：{booking.lab.name} {booking.date} {booking.time_range_label}。", target_type="booking", target_id=booking.id, reversible=False, undo_blocked_reason="修改后请再编辑或取消。")
            flash("预约已修改并重新提交审核。", "success")
            return redirect(url_for("bookings.submitted", booking_id=booking.id))
    return render_template(
        "bookings/form.html",
        form=form,
        labs=labs,
        teammates=current_user.teammates,
        editing=booking,
    )

@bookings_bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.user_id != current_user.id:
        flash("只能取消自己的预约。", "error")
        return _records_redirect()
    if booking.status not in ("pending", "approved"):
        flash("该预约当前状态不可取消。", "error")
        return _records_redirect()
    if not booking.can_self_change():
        flash(_locked_message(booking), "error")
        return _records_redirect()
    booking.status = "cancelled"
    db.session.commit()
    record_audit("booking.cancel", summary=f"取消测试申请：{booking.lab.name} {booking.date} {booking.time_range_label}。", target_type="booking", target_id=booking.id, reversible=False, undo_blocked_reason="取消后请重新提交申请。")
    flash("预约已取消。", "success")
    return _records_redirect()


def _schedule_item(booking, is_staff: bool):
    """What the occupancy view may show for one booking.

    Staff see everything. A captain sees full details only for their own bookings; for anyone else's
    booking only the time range and status are passed to the template (no name, student id, team,
    contact, purpose or even the booking id), so nothing identifying can reach the HTML.
    """
    mine = booking.user_id == current_user.id
    full = is_staff or mine
    return SimpleNamespace(
        date=booking.date,
        start_time=booking.start_time,
        end_time=booking.end_time,
        time_range_label=booking.time_range_label,
        status=booking.status,
        status_label=booking.status_label,
        mine=mine,
        full=full,
        name=booking.user.name if full else None,
        purpose=booking.purpose if full else None,
        checkin=booking.checkin if is_staff else None,
    )


@bookings_bp.route("/schedule")
@login_required
def schedule():
    labs = Lab.query.order_by(Lab.name.asc()).all()
    if not labs:
        return render_template("bookings/schedule.html", labs=[], lab=None)

    lab_id = request.args.get("lab_id", type=int)
    lab = db.session.get(Lab, lab_id) if lab_id else labs[0]
    if lab is None:
        lab = labs[0]

    view = request.args.get("view", "day")
    if view not in ("day", "week"):
        view = "day"

    selected = parse_date(request.args.get("date")) or today()
    if view == "week":
        start_day, end_day = week_bounds(selected)
        days = [start_day + timedelta(days=i) for i in range(7)]
    else:
        start_day = end_day = selected
        days = [selected]

    only_missing = current_user.is_staff and request.args.get("checkin") == "missing"
    # Captains never load other people's user rows: occupied slots are anonymised below.
    is_staff = current_user.is_staff
    query = Booking.query.options(*booking_list_options(user=is_staff)).filter(
        Booking.lab_id == lab.id,
        Booking.date >= start_day,
        Booking.date <= end_day,
        Booking.status.in_(("pending", "approved")),
    )
    if only_missing:
        query = query.filter(Booking.status == "approved", Booking.checked_in_at.is_(None))
    bookings = [_schedule_item(b, is_staff) for b in query.order_by(Booking.start_time.asc()).all()]

    slots = time_slots(lab.open_start, lab.open_end, include_end=False)
    grid = {}
    for booking in bookings:
        for slot in slots:
            slot_end = (datetime.combine(booking.date, slot) + timedelta(minutes=30)).time()
            if intervals_overlap(slot, slot_end, booking.start_time, booking.end_time):
                key = (booking.date.isoformat(), slot.strftime("%H:%M"))
                grid.setdefault(key, []).append(booking)

    prev_date = (start_day - timedelta(days=1 if view == "day" else 7)).isoformat()
    next_date = (start_day + timedelta(days=1 if view == "day" else 7)).isoformat()

    return render_template(
        "bookings/schedule.html",
        labs=labs,
        lab=lab,
        view=view,
        only_missing=only_missing,
        selected=selected,
        days=days,
        slots=slots,
        bookings=bookings,
        grid=grid,
        prev_date=prev_date,
        next_date=next_date,
        start_day=start_day,
        end_day=end_day,
    )
