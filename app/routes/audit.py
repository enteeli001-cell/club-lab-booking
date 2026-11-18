from flask import Blueprint, render_template
from flask_login import current_user

from app.decorators import reviewer_required
from app.models import PasswordResetLog

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


@audit_bp.route("/password-resets")
@reviewer_required
def password_resets():
    logs = PasswordResetLog.query.order_by(PasswordResetLog.created_at.desc()).limit(500).all()
    return render_template("audit/password_resets.html", logs=logs)
