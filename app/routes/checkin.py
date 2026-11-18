"""Booking check-in by scanning the fixed QR code posted in each lab."""

from flask import (
    Blueprint,
    jsonify,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from app import db
from app.i18n import t
from app.activity import client_ip, current_user_agent, record_audit
from app.checkin import (
    checkin_url,
    check_location,
    checkin_window,
    in_window,
    new_checkin_token,
    public_base_url,
    qr_png_bytes,
)
from app.decorators import lab_manager_required, superadmin_required
from app.models import Booking, Lab
from app.transactions import begin_serialized_write
from app.utils import local_now

checkin_bp = Blueprint("checkin", __name__)


# ---------------------------------------------------------------- scan flow

def _client_key() -> str:
    return f"checkin:{client_ip() or 'unknown'}"


def _rate_limited() -> bool:
    limiter = current_app.extensions["checkin_limiter"]
    limit = current_app.config.get("CHECKIN_RATE_LIMIT", 30)
    window = current_app.config.get("CHECKIN_RATE_WINDOW", 60)
    return not limiter.allow(_client_key(), limit, window)


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def _page(kind, title, message, *, status=200, lab=None, booking=None, other=None, code=None):
    if _wants_json():
        if kind != "success" and status == 200:
            status = 400
        payload = {"ok": kind == "success", "kind": kind, "code": code or kind, "title": t(title), "message": t(message)}
        return jsonify(payload), status
    return (
        render_template(
            "checkin/result.html",
            kind=kind,
            title=title,
            message=message,
            lab=lab,
            booking=booking,
            other=other,
        ),
        status,
    )


def _evaluate(lab, user, now):
    """Return (kind, booking) describing what scanning this lab's code means for the user."""
    todays = (
        Booking.query.filter(
            Booking.user_id == user.id,
            Booking.status == "approved",
            Booking.date == now.date(),
        )
        .order_by(Booking.start_time.asc())
        .all()
    )
    here = [b for b in todays if b.lab_id == lab.id]
    elsewhere = [b for b in todays if b.lab_id != lab.id]

    ready = [b for b in here if b.checked_in_at is None and in_window(b, now)]
    if ready:
        return "ready", ready[0]
    done_now = [b for b in here if b.checked_in_at is not None and in_window(b, now)]
    if done_now:
        return "already", done_now[0]
    early = [b for b in here if b.checked_in_at is None and now < checkin_window(b)[0]]
    if early:
        return "early", early[0]
    if elsewhere:
        # A booking at another lab that is open for check-in (or upcoming) means a wrong QR code.
        candidates = [b for b in elsewhere if b.checked_in_at is None and now <= checkin_window(b)[1]]
        if candidates:
            candidates.sort(key=lambda b: (not in_window(b, now), b.start_time))
            return "wrong_lab", candidates[0]
    expired = [b for b in here if b.checked_in_at is None]
    if expired:
        return "expired", expired[-1]
    done_past = [b for b in here if b.checked_in_at is not None]
    if done_past:
        return "already", done_past[-1]
    return "none", None


def _result_for(kind, booking, lab, *, status_ok=200):
    if kind == "ready":
        return render_template("checkin/confirm.html", lab=lab, booking=booking), status_ok
    if kind == "already":
        stamp = booking.checked_in_at.strftime("%Y-%m-%d %H:%M")
        return _page("info", "已签到过", f"你在 {stamp} 已经完成本次预约的签到，无需重复操作。", lab=lab, booking=booking)
    if kind == "early":
        opens = checkin_window(booking)[0].strftime("%H:%M")
        return _page("warn", "还没到签到时间", f"你今天在本实验室的预约是 {booking.time_range_label}，请在 {opens} 之后再扫码签到。", lab=lab, booking=booking)
    if kind == "expired":
        return _page("error", "已过签到时间", f"你今天 {booking.time_range_label} 的预约签到时间已结束，不能再补签。如有疑问请联系实验室管理员。", lab=lab, booking=booking)
    if kind == "wrong_lab":
        return _page(
            "error",
            "扫错实验室了",
            f"你今天的预约在「{booking.lab.name}」（{booking.time_range_label}），而这张二维码属于「{lab.name}」。请前往「{booking.lab.name}」扫描门口的二维码。",
            lab=lab,
            booking=booking,
        )
    return _page("error", "没有找到可签到的预约", f"你今天在「{lab.name}」没有已通过的预约。请先在系统中预约并等待管理员审核通过。", lab=lab)


def _guard(token):
    """Shared checks for GET/POST. Returns (response_or_None, lab)."""
    if not current_user.is_authenticated:
        if _wants_json():
            return (jsonify({"ok": False, "kind": "error", "code": "login", "title": t("请先登录"), "message": t("登录已失效，请重新登录后再签到。")}), 401), None
        flash("请先登录，登录后会自动回到签到页面。", "warning")
        return redirect(url_for("auth.login", next=request.path)), None
    if _rate_limited():
        return _page("error", "操作太频繁", "请求次数过多，请稍后再试。", status=429), None
    lab = Lab.query.filter_by(checkin_token=token).first() if token else None
    if lab is None:
        return _page("error", "二维码无效", "这个二维码无效或已被更换，请联系实验室管理员重新获取并扫描最新的二维码。", status=404), None
    if not current_user.is_regular_user:
        return _page("info", "无需签到", "管理员账号无需签到。预约队长请用自己的账号扫码。", lab=lab), None
    return None, lab


@checkin_bp.route("/checkin/<token>", methods=["GET"])
def scan(token):
    early, lab = _guard(token)
    if early is not None:
        return early
    kind, booking = _evaluate(lab, current_user, local_now())
    return _result_for(kind, booking, lab)


def _confirm_error(lab, booking, code, message, status=400):
    """A rejected confirm: JSON for the in-page scanner, the confirm page (with the message) otherwise."""
    if _wants_json():
        return jsonify({"ok": False, "kind": "error", "code": code, "title": t("签到未完成"), "message": t(message)}), status
    return render_template("checkin/confirm.html", lab=lab, booking=booking, error=message, error_code=code), status


@checkin_bp.route("/checkin/<token>", methods=["POST"])
def confirm(token):
    early, lab = _guard(token)
    if early is not None:
        return early
    begin_serialized_write()
    now = local_now()
    kind, booking = _evaluate(lab, current_user, now)
    if kind != "ready":
        db.session.rollback()
        return _result_for(kind, booking, lab)
    wanted = (request.form.get("booking_id") or "").strip()
    if wanted and wanted != str(booking.id):
        db.session.rollback()
        return _confirm_error(lab, booking, "booking_mismatch", "你点击的预约不是当前可签到的预约，请刷新页面后重试。")
    # 1) Safety pledge: without it nothing is written.
    if request.form.get("pledge") != "1":
        db.session.rollback()
        return _confirm_error(lab, booking, "pledge", "请先阅读并勾选“我已阅读并承诺遵守以上要求”，再签到。")
    # 2) Location mode (only for labs that switched it on and have coordinates).
    verdict = None
    if lab.geo_active:
        verdict = check_location(lab, request.form.get("lat"), request.form.get("lng"), request.form.get("accuracy"))
        if not verdict["ok"]:
            db.session.rollback()
            return _confirm_error(lab, booking, "geo", verdict["message"], status=403)
    booking.checked_in_at = now
    booking.pledge_accepted_at = now
    booking.checkin_ip = (client_ip() or "")[:64] or None
    booking.checkin_ua = (current_user_agent() or "")[:300] or None
    if verdict is not None:
        booking.checkin_lat = verdict["lat"]
        booking.checkin_lng = verdict["lng"]
        booking.checkin_accuracy = verdict["accuracy"]
        booking.checkin_distance_m = verdict["distance"]
    db.session.commit()
    where = f"，距离实验室约 {verdict['distance']:.0f} 米" if verdict else ""
    record_audit(
        "booking.checkin",
        summary=f"扫码签到成功（{lab.name} {booking.date} {booking.time_range_label}），已确认安全承诺{where}。",
        target_type="booking",
        target_id=booking.id,
        reversible=False,
        undo_blocked_reason="签到记录不可撤销。",
    )
    return _page("success", "签到成功", f"{lab.name} · {booking.date} {booking.time_range_label}，签到时间 {now.strftime('%H:%M:%S')}。", lab=lab, booking=booking)


# ------------------------------------------------------------- admin pages

def _labs():
    return Lab.query.order_by(Lab.name.asc()).all()


@checkin_bp.route("/checkin-codes/")
@lab_manager_required
def admin_codes():
    base = public_base_url()
    rows = [{"lab": lab, "url": checkin_url(lab, base)} for lab in _labs()]
    return render_template("checkin/admin.html", rows=rows, base_url=base)


@checkin_bp.route("/checkin-codes/<int:lab_id>.png")
@lab_manager_required
def qr_png(lab_id):
    lab = db.session.get(Lab, lab_id)
    if lab is None or not lab.checkin_token:
        abort(404)
    data = qr_png_bytes(checkin_url(lab), scale=12)
    response = Response(data, mimetype="image/png")
    if request.args.get("download"):
        response.headers["Content-Disposition"] = f"attachment; filename=checkin-qr-{lab.id}.png"
    return response


@checkin_bp.route("/checkin-codes/<int:lab_id>/print")
@lab_manager_required
def print_view(lab_id):
    lab = db.session.get(Lab, lab_id)
    if lab is None or not lab.checkin_token:
        abort(404)
    return render_template("checkin/print.html", lab=lab, url=checkin_url(lab))


@checkin_bp.route("/checkin-codes/<int:lab_id>/reset", methods=["POST"])
@superadmin_required
def reset_code(lab_id):
    lab = db.session.get(Lab, lab_id)
    if lab is None:
        flash("实验室不存在。", "error")
        return redirect(url_for("checkin.admin_codes"))
    if request.form.get("confirm") != "yes":
        flash("需要确认后才能重置二维码。", "error")
        return redirect(url_for("checkin.admin_codes"))
    lab.checkin_token = new_checkin_token()
    lab.checkin_token_updated_at = local_now()
    db.session.commit()
    record_audit(
        "checkin.reset",
        summary=f"重置实验室「{lab.name}」的签到二维码，旧二维码已失效。",
        target_type="lab",
        target_id=lab.id,
        reversible=False,
        undo_blocked_reason="旧二维码已失效，无法恢复。",
    )
    flash(f"已重置「{lab.name}」的签到二维码，旧码立即失效，请重新打印张贴。", "success")
    return redirect(url_for("checkin.admin_codes"))
