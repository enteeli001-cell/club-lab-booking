from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import Booking, ToolLoan
from app.queries import booking_contacts_map, booking_list_options, loan_list_options
from app.utils import format_contacts_label

records_bp = Blueprint("records", __name__)


@records_bp.route("/records")
@login_required
def index():
    if not current_user.is_regular_user:
        if current_user.is_staff:
            tab = request.args.get("tab", "bookings")
            if tab == "tools":
                return redirect(url_for("tools.admin_loans"))
            return redirect(url_for("review.list_reviews"))
        return redirect(url_for("main.index"))

    tab = request.args.get("tab", "bookings")
    if tab not in ("bookings", "tools"):
        tab = "bookings"

    bookings = (
        Booking.query.options(*booking_list_options(reviewer=True))
        .filter_by(user_id=current_user.id)
        .order_by(Booking.date.desc(), Booking.start_time.desc())
        .all()
    )
    locked = [b for b in bookings if b.status in ("pending", "approved") and not b.can_self_change()]
    contacts_by_id = booking_contacts_map(locked)

    loans = (
        ToolLoan.query.options(*loan_list_options())
        .filter_by(user_id=current_user.id)
        .order_by(ToolLoan.borrow_at.desc())
        .all()
    )
    return render_template(
        "records/index.html",
        tab=tab,
        bookings=bookings,
        loans=loans,
        contacts_by_id=contacts_by_id,
        format_contacts_label=format_contacts_label,
        admin_view=False,
    )
