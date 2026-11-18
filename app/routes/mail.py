from flask import Blueprint, render_template, request
from flask_login import login_required

from app.decorators import reviewer_required
from app.models import MailLog

mail_bp = Blueprint("mail", __name__, url_prefix="/mail")


@mail_bp.route("/")
@reviewer_required
def list_logs():
    status = request.args.get("status", "all")
    if status not in ("all", "sent", "local", "failed"):
        status = "all"
    query = MailLog.query
    if status != "all":
        query = query.filter_by(status=status)
    logs = query.order_by(MailLog.created_at.desc()).limit(200).all()
    return render_template("mail/list.html", logs=logs, status=status)
