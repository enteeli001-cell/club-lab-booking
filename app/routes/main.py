from flask import Blueprint, Response, redirect, render_template, url_for
from flask_login import current_user, login_required

from app import db
from app.models import Booking, Lab, User
from app.utils import today

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.is_regular_user:
        return redirect(url_for("main.captain_home"))

    from app.models import ToolLoan
    from app.utils import local_now

    borrowing = [row[0] for row in db.session.query(ToolLoan.expected_return_at).filter(ToolLoan.status == "borrowing")]
    now = local_now()
    context = {
        "today_count": Booking.query.filter_by(status="approved", date=today()).count(),
        "loan_count": len(borrowing),
        "loan_overdue": sum(1 for expected in borrowing if expected and expected < now),
        "lab_count": Lab.query.filter_by(enabled=True).count(),
        "user_count": User.query.count() if current_user.can_manage_accounts else None,
        "pending_count": (
            Booking.query.filter_by(status="pending").count() if current_user.can_review else None
        ),
    }
    return render_template("admin_home.html", **context)


@main_bp.route("/captain")
@login_required
def captain_home():
    if not current_user.is_regular_user:
        return redirect(url_for("main.index"))
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload

    from app.models import ToolLoan
    from app.utils import local_now

    day = today()
    counts = dict(
        db.session.query(Booking.status, func.count(Booking.id))
        .filter(Booking.user_id == current_user.id, Booking.date >= day, Booking.status.in_(("pending", "approved")))
        .group_by(Booking.status)
        .all()
    )
    next_booking = (
        Booking.query.options(selectinload(Booking.lab))
        .filter(Booking.user_id == current_user.id, Booking.date >= day, Booking.status.in_(("pending", "approved")))
        .order_by(Booking.date.asc(), Booking.start_time.asc())
        .first()
    )
    loans = {"borrowing": 0, "overdue": 0}
    if current_user.can_borrow_tools:
        now = local_now()
        for expected, in db.session.query(ToolLoan.expected_return_at).filter(
            ToolLoan.user_id == current_user.id, ToolLoan.status == "borrowing"
        ):
            loans["overdue" if expected < now else "borrowing"] += 1
    summary = {
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "next_booking": next_booking,
        "loans": loans,
    }
    return render_template("captain_home.html", summary=summary)


@main_bp.route("/health")
def health():
    return "ok", 200

@main_bp.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Disallow: /_security_honeypot/\n"
        "Disallow: /admin-backup/\n"
        "Allow: /login\n"
        "Allow: /static/\n"
    )
    return Response(body, mimetype="text/plain")
