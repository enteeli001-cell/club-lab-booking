"""Forced no-show acknowledgement (captain) and no-show overview (staff)."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.activity import client_ip, record_audit
from app.decorators import stats_required
from app.models import NoShowAlert, User
from app.noshow import counts_by_user, resolve_email, threshold
from app.utils import local_now, safe_local_url

noshow_bp = Blueprint("noshow", __name__)


@noshow_bp.route("/noshow/ack", methods=["POST"])
@login_required
def acknowledge():
    if not current_user.is_regular_user:
        return redirect(url_for("main.index"))
    now = local_now()
    alerts = NoShowAlert.query.filter(
        NoShowAlert.user_id == current_user.id, NoShowAlert.acknowledged_at.is_(None)
    ).all()
    for alert in alerts:
        alert.acknowledged_at = now
        alert.ack_ip = (client_ip() or "")[:64] or None
    if alerts:
        db.session.commit()
        record_audit(
            "noshow.ack",
            summary=f"确认已知晓未签到提醒（累计 {max(a.level for a in alerts)} 次）。",
            target_type="user",
            target_id=current_user.id,
            reversible=False,
            undo_blocked_reason="确认记录不可撤销。",
        )
    target = request.form.get("next") or ""
    if safe_local_url(target) and not target.startswith("/noshow/"):
        return redirect(target)
    return redirect(url_for("main.index"))


@noshow_bp.route("/noshows/")
@stats_required
def overview():
    now = local_now()
    limit = threshold()
    only_high = request.args.get("view") == "high"
    grouped = counts_by_user(now)
    users = {u.id: u for u in User.query.filter(User.id.in_(list(grouped) or [0])).all()}
    alerts = {}
    for alert in NoShowAlert.query.order_by(NoShowAlert.level.asc()).all():
        alerts[alert.user_id] = alert  # keeps the highest level per user
    rows = []
    for user_id, bookings in grouped.items():
        user = users.get(user_id)
        if user is None:
            continue
        count = len(bookings)
        if only_high and count < limit:
            continue
        alert = alerts.get(user_id)
        last = max(bookings, key=lambda b: (b.date, b.start_time))
        rows.append(
            {
                "user": user,
                "count": count,
                "high": count >= limit,
                "last": last,
                "alert": alert,
                "has_email": bool(resolve_email(user)),
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["user"].student_id))
    return render_template("noshow/list.html", rows=rows, limit=limit, only_high=only_high)
