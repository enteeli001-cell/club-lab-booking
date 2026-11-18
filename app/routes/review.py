from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case

from app import db
from app.transactions import begin_serialized_write
from app.activity import record_audit
from app.decorators import reviewer_required
from app.mailer import send_reject_notice
from app.models import Booking
from app.queries import booking_list_options, pending_conflict_map
from app.utils import find_conflicts, local_now, validate_booking

review_bp = Blueprint("review", __name__, url_prefix="/review")

REVIEW_PAGE_SIZE = 50


@review_bp.route("/")
@reviewer_required
def list_reviews():
    status = request.args.get("status", "pending")
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    checkin = request.args.get("checkin", "")
    if checkin not in ("missing", "done"):
        checkin = ""
    query = Booking.query.options(*booking_list_options(user=True))
    if checkin:
        status = "approved"
    if status != "all":
        query = query.filter_by(status=status)
    if checkin == "missing":
        query = query.filter(Booking.checked_in_at.is_(None))
    elif checkin == "done":
        query = query.filter(Booking.checked_in_at.isnot(None))
    query = query.order_by(
        case((Booking.status == "pending", 0), else_=1),
        Booking.created_at.desc(),
        Booking.id.desc(),
    )
    page = request.args.get("page", 1, type=int) or 1
    # Server-side pagination: 50 rows per page; out-of-range pages are clamped, never 404.
    pagination = query.paginate(page=max(1, page), per_page=REVIEW_PAGE_SIZE, error_out=False)
    if pagination.pages and pagination.page > pagination.pages:
        pagination = query.paginate(page=pagination.pages, per_page=REVIEW_PAGE_SIZE, error_out=False)
    bookings = pagination.items

    pending_hints = pending_conflict_map(bookings)
    return render_template(
        "review/list.html",
        bookings=bookings,
        status=status,
        checkin=checkin,
        pending_hints=pending_hints,
        pagination=pagination,
        page_args={"status": status, **({"checkin": checkin} if checkin else {})},
    )


@review_bp.route("/<int:booking_id>/approve", methods=["POST"])
@reviewer_required
def approve(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.status != "pending":
        flash("只能审核待审核的预约。", "error")
        return redirect(url_for("review.list_reviews"))

    errors = validate_booking(
        booking.lab,
        booking.date,
        booking.start_time,
        booking.end_time,
        booking.people_count,
        exclude_id=booking.id,
        check_past=True,
    )
    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("review.list_reviews", status="pending"))

    _approved, pending = find_conflicts(
        booking.lab_id, booking.date, booking.start_time, booking.end_time, exclude_id=booking.id
    )
    booking.status = "approved"
    booking.reviewed_by_id = current_user.id
    booking.reviewed_at = local_now()
    booking.reject_reason = None
    db.session.commit()
    record_audit("booking.approve", summary=f"通过 {booking.user.name} 的预约（{booking.lab.name} {booking.date} {booking.time_range_label}）。", target_type="booking", target_id=booking.id, reversible=True)
    flash(f"已通过 {booking.user.name} 的预约（{booking.lab.name} {booking.date} {booking.time_range_label}）。", "success")
    if pending:
        others = "、".join(f"{b.user.name} {b.time_range_label}" for b in pending)
        flash(f"提示：仍有重叠的待审核预约（{others}），通过那些预约时将被拒绝。", "warning")
    return redirect(url_for("review.list_reviews", status="pending"))


@review_bp.route("/<int:booking_id>/reject", methods=["POST"])
@reviewer_required
def reject(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.status != "pending":
        flash("只能审核待审核的预约。", "error")
        return redirect(url_for("review.list_reviews"))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("拒绝时请填写理由。", "error")
        return redirect(url_for("review.list_reviews", status="pending"))
    booking.status = "rejected"
    booking.reject_reason = reason[:200]
    booking.reviewed_by_id = current_user.id
    booking.reviewed_at = local_now()
    db.session.commit()
    record_audit("booking.reject", summary=f"拒绝 {booking.user.name} 的预约（{booking.lab.name} {booking.date} {booking.time_range_label}）。", target_type="booking", target_id=booking.id, payload={"reason": booking.reject_reason}, reversible=True)
    mail_log = send_reject_notice(booking, booking.reject_reason)
    flash(f"已拒绝 {booking.user.name} 的预约。", "success")
    if mail_log.status == "failed":
        flash("拒绝已保存，但邮件发送失败，已记入发送记录。", "warning")
    return redirect(url_for("review.list_reviews", status="pending"))
