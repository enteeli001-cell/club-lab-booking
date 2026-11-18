import hmac
from app.i18n import t

from flask import Blueprint, current_app, jsonify, request

from app import db
from app.transactions import begin_serialized_write
from app.models import ROLE_LABELS, Booking, DutyShift, Lab, ToolLoan, User
from app.activity import record_audit
from app.notices import BOOKING_CHANGE_LOCKED_NOTICE
from app.routes.users import _issue_password
from app.mailer import send_reject_notice
from app.utils import (
    find_conflicts,
    is_email_address,
    is_mobile_phone,
    find_booking_contacts,
    format_contacts_label,
    local_now,
    parse_date,
    parse_time,
    validate_booking,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _error(message, status):
    return jsonify(ok=False, error=t(message)), status


def _ok(payload=None, status=200):
    body = {"ok": True}
    if payload is not None:
        body.update(payload)
    return jsonify(body), status


def _bearer_token():
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


def _lab_json(lab):
    return {
        "id": lab.id,
        "name": lab.name,
        "capacity": lab.capacity,
        "open_start": lab.open_start.strftime("%H:%M"),
        "open_end": lab.open_end.strftime("%H:%M"),
        "enabled": lab.enabled,
    }


def _user_json(user, include_sensitive=False):
    data = {
        "id": user.id,
        "name": user.name,
        "student_id": user.student_id,
        "role": user.role,
        "role_label": user.role_label,
    }
    return data


def _booking_json(booking):
    return {
        "id": booking.id,
        "lab_id": booking.lab_id,
        "lab_name": booking.lab.name,
        "user_id": booking.user_id,
        "student_id": booking.user.student_id,
        "user_name": booking.user.name,
        "date": booking.date.isoformat(),
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
        "purpose": booking.purpose,
        "people_count": booking.people_count,
        "captain_phone": booking.captain_phone,
        "captain_email": booking.captain_email,
        "companions": [
            {
                "source": c.source,
                "name": c.name,
                "student_id": c.student_id,
                "contact": c.contact,
            }
            for c in booking.companions
        ],
        "status": booking.status,
        "status_label": booking.status_label,
        "reject_reason": booking.reject_reason,
        "duration_minutes": booking.duration_minutes,
    }


def _conflict_json(booking, actor=None):
    """Staff (and the owner) get names; any other captain only learns that the slot is taken."""
    if actor is None or not (actor.can_review or booking.user_id == actor.id):
        return {
            "occupied": True,
            "date": booking.date.isoformat(),
            "start_time": booking.start_time.strftime("%H:%M"),
            "end_time": booking.end_time.strftime("%H:%M"),
            "status": booking.status,
        }
    return {
        "id": booking.id,
        "student_id": booking.user.student_id,
        "user_name": booking.user.name,
        "date": booking.date.isoformat(),
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
        "status": booking.status,
    }


@api_bp.before_request
def _require_bearer():
    if request.endpoint == "api.health":
        return None
    expected = current_app.config.get("API_TOKEN") or ""
    provided = _bearer_token()
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        return _error("无效或缺失的 Bearer 令牌。", 401)
    if request.is_json and request.get_data(cache=True):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error("JSON 请求体须为对象。", 400)
        text_fields = {"name", "student_id", "role", "password", "purpose", "captain_email", "captain_phone", "reason"}
        if any(key in data and not isinstance(data[key], str) for key in text_fields):
            return _error("文本字段须为字符串。", 400)
        if "enabled" in data and not isinstance(data["enabled"], bool):
            return _error("enabled 须为布尔值。", 400)
    return None


def _actor():
    student_id = (request.headers.get("X-Student-Id") or "").strip()
    if not student_id:
        return None, _error("请通过请求头 X-Student-Id 指定操作用户学号。", 400)
    user = User.query.filter_by(student_id=student_id).first()
    if user is None:
        return None, _error("指定的用户不存在。", 404)
    if not user.is_account_active:
        return None, _error(user.inactive_reason() or "该账号当前不可用。", 403)
    if user.must_change_password:
        return None, _error("首次登录请先修改初始密码后再使用系统。", 403)
    from app.routes.profile import needs_profile
    if needs_profile(user):
        return None, _error("请先完善个人信息并验证邮箱后再使用系统。", 403)
    from app.toggles import api_feature_error

    disabled = api_feature_error(user)
    if disabled:
        return None, _error(disabled, 403)
    return user, None


@api_bp.get("/health")
def health():
    return _ok({"service": "club-lab-system", "status": "up"})


@api_bp.get("/me")
def me():
    actor, err = _actor()
    if err:
        return err
    return _ok({"user": _user_json(actor)})


@api_bp.get("/labs")
def list_labs():
    actor, err = _actor()
    if err:
        return err
    query = Lab.query.order_by(Lab.name.asc())
    if not actor.can_manage_labs:
        query = query.filter_by(enabled=True)
    return _ok({"labs": [_lab_json(lab) for lab in query.all()]})


@api_bp.post("/labs")
def create_lab():
    actor, err = _actor()
    if err:
        return err
    if not actor.can_manage_labs:
        return _error("需要管理员权限。", 403)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    open_start = parse_time(data.get("open_start"))
    open_end = parse_time(data.get("open_end"))
    enabled = bool(data.get("enabled", True))
    try:
        capacity = int(data.get("capacity"))
    except (TypeError, ValueError):
        return _error("容量须为大于 0 的整数。", 400)
    if not name:
        return _error("请填写实验室名称。", 400)
    if capacity < 1:
        return _error("容量须为大于 0 的整数。", 400)
    if open_start is None or open_end is None or open_start >= open_end:
        return _error("请填写有效的开放时间，且结束须晚于开始。", 400)
    if Lab.query.filter_by(name=name).first():
        return _error("已存在同名实验室。", 400)
    lab = Lab(name=name, capacity=capacity, open_start=open_start, open_end=open_end, enabled=enabled)
    db.session.add(lab)
    db.session.commit()
    record_audit("lab.create", actor=actor, summary="通过 API 执行：lab.create。", target_type="lab", target_id=lab.id, reversible=False)
    return _ok({"lab": _lab_json(lab)}, 201)


@api_bp.get("/labs/<int:lab_id>")
def get_lab(lab_id):
    actor, err = _actor()
    if err:
        return err
    lab = db.session.get(Lab, lab_id)
    if lab is None or (not actor.can_manage_labs and not lab.enabled):
        return _error("实验室不存在。", 404)
    return _ok({"lab": _lab_json(lab)})


@api_bp.put("/labs/<int:lab_id>")
def update_lab(lab_id):
    actor, err = _actor()
    if err:
        return err
    if not actor.can_manage_labs:
        return _error("需要管理员权限。", 403)
    lab = db.session.get(Lab, lab_id)
    if lab is None:
        return _error("实验室不存在。", 404)
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return _error("请填写实验室名称。", 400)
        exists = Lab.query.filter(Lab.name == name, Lab.id != lab.id).first()
        if exists:
            return _error("已存在同名实验室。", 400)
        lab.name = name
    if "capacity" in data:
        try:
            capacity = int(data.get("capacity"))
        except (TypeError, ValueError):
            return _error("容量须为大于 0 的整数。", 400)
        if capacity < 1:
            return _error("容量须为大于 0 的整数。", 400)
        lab.capacity = capacity
    if "open_start" in data:
        start = parse_time(data.get("open_start"))
        if start is None:
            return _error("开放开始时间无效。", 400)
        lab.open_start = start
    if "open_end" in data:
        end = parse_time(data.get("open_end"))
        if end is None:
            return _error("开放结束时间无效。", 400)
        lab.open_end = end
    if lab.open_start >= lab.open_end:
        return _error("开放结束时间必须晚于开始时间。", 400)
    if "enabled" in data:
        lab.enabled = bool(data.get("enabled"))
    db.session.commit()
    record_audit("lab.update", actor=actor, summary="通过 API 执行：lab.update。", target_type="lab", target_id=lab.id, reversible=False)
    return _ok({"lab": _lab_json(lab)})


@api_bp.delete("/labs/<int:lab_id>")
def delete_lab(lab_id):
    actor, err = _actor()
    if err:
        return err
    if not actor.can_manage_labs:
        return _error("需要管理员权限。", 403)
    lab = db.session.get(Lab, lab_id)
    if lab is None:
        return _error("实验室不存在。", 404)
    active = Booking.query.filter(
        Booking.lab_id == lab.id,
        Booking.status.in_(("pending", "approved")),
    ).count()
    if active:
        return _error("该实验室仍有待审核或已通过的预约，不能删除。", 400)
    if ToolLoan.query.filter_by(lab_id=lab.id).first():
        return _error("该实验室仍有工具借用记录，不能删除。可先停用。", 400)
    # Preserve historical bookings and their mail/companion evidence.
    if Booking.query.filter_by(lab_id=lab.id).first():
        return _error("该实验室仍有关联预约记录，不能删除。可先停用。", 400)
    DutyShift.query.filter_by(lab_id=lab.id).delete()
    db.session.delete(lab)
    db.session.commit()
    record_audit("lab.delete", actor=actor, summary="通过 API 删除实验室。", target_type="lab", reversible=False)
    return _ok({"deleted": True})


@api_bp.get("/users")
def list_users():
    actor, err = _actor()
    if err:
        return err
    if not actor.can_manage_accounts:
        return _error("需要总管理员权限。", 403)
    users = User.query.order_by(User.role.asc(), User.student_id.asc()).all()
    return _ok({"users": [_user_json(u) for u in users]})


@api_bp.post("/users")
def create_user():
    actor, err = _actor()
    if err:
        return err
    if not actor.can_manage_accounts:
        return _error("需要总管理员权限。", 403)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    student_id = (data.get("student_id") or "").strip()
    role = (data.get("role") or "member").strip()
    password = (data.get("password") or "").strip()
    if not name or not student_id:
        return _error("请填写姓名和学号/账号。", 400)
    if role not in ROLE_LABELS:
        return _error("角色无效。", 400)
    if User.query.filter_by(student_id=student_id).first():
        return _error("该学号/账号已存在。", 400)
    password, auto, pw_errors = _issue_password(role, student_id, password)
    if pw_errors:
        return _error(pw_errors[0], 400)
    user = User(name=name, student_id=student_id, role=role)
    user.set_password(password, require_change=True)
    db.session.add(user)
    db.session.commit()
    record_audit("user.create", actor=actor, summary="通过 API 执行：user.create。", target_type="user", target_id=user.id, reversible=False)
    payload = {"user": _user_json(user), "password_auto_generated": auto}
    if auto:
        payload["initial_password"] = password
    return _ok(payload, 201)


@api_bp.get("/bookings")
def list_bookings():
    actor, err = _actor()
    if err:
        return err
    query = Booking.query
    if not actor.can_review:
        query = query.filter_by(user_id=actor.id)
    else:
        student_id = (request.args.get("student_id") or "").strip()
        if student_id:
            user = User.query.filter_by(student_id=student_id).first()
            if user is None:
                return _error("指定的成员不存在。", 404)
            query = query.filter_by(user_id=user.id)
    status = (request.args.get("status") or "").strip()
    if status:
        query = query.filter_by(status=status)
    lab_id = request.args.get("lab_id", type=int)
    if lab_id:
        query = query.filter_by(lab_id=lab_id)
    bookings = query.order_by(Booking.date.desc(), Booking.start_time.desc()).all()
    return _ok({"bookings": [_booking_json(b) for b in bookings]})


def _parse_booking_payload(data):
    lab_id = data.get("lab_id")
    lab = db.session.get(Lab, int(lab_id)) if str(lab_id).isdigit() or isinstance(lab_id, int) else None
    booking_date = parse_date(data.get("date"))
    start = parse_time(data.get("start_time"))
    end = parse_time(data.get("end_time"))
    purpose = (data.get("purpose") or "").strip()
    try:
        people_count = int(data.get("people_count"))
    except (TypeError, ValueError):
        people_count = None
    return lab, booking_date, start, end, purpose, people_count


@api_bp.post("/bookings")
def create_booking():
    actor, err = _actor()
    if err:
        return err
    if not actor.can_book:
        return _error("当前账号无权提交测试预约。", 403)
    data = request.get_json(silent=True) or {}
    lab, booking_date, start, end, purpose, people_count = _parse_booking_payload(data)
    errors = []
    if lab is None:
        errors.append("请选择实验室。")
    if not purpose:
        errors.append("请填写用途。")
    elif len(purpose) > 200:
        errors.append("用途不能超过 200 字。")
    errors.extend(validate_booking(lab, booking_date, start, end, people_count))
    captain_email = (data.get("captain_email") or actor.last_email or "").strip()
    captain_phone = (data.get("captain_phone") or actor.last_phone or "").strip()
    if not is_mobile_phone(captain_phone):
        errors.append("请填写有效的队长本人手机号。")
    if not is_email_address(captain_email):
        errors.append("请填写有效的队长邮箱。")
    if errors:
        return _error(" ".join(errors), 400)
    _approved, pending = find_conflicts(lab.id, booking_date, start, end)
    booking = Booking(
        lab=lab,
        user=actor,
        date=booking_date,
        start_time=start,
        end_time=end,
        purpose=purpose,
        people_count=people_count,
        captain_phone=captain_phone or None,
        captain_email=captain_email,
        status="pending",
    )
    actor.last_email = captain_email
    if captain_phone:
        actor.last_phone = captain_phone
    db.session.add(booking)
    db.session.commit()
    record_audit("booking.create", actor=actor, summary="通过 API 执行：booking.create。", target_type="booking", target_id=booking.id, reversible=False)
    warning = None
    if pending:
        warning = "该时段与其他待审核预约重叠，审核时以先通过者为准。"
    payload = {"booking": _booking_json(booking)}
    if warning:
        payload["warning"] = warning
        payload["pending_conflicts"] = [_conflict_json(b, actor) for b in pending]
    return _ok(payload, 201)


@api_bp.get("/bookings/<int:booking_id>")
def get_booking(booking_id):
    actor, err = _actor()
    if err:
        return err
    booking = db.session.get(Booking, booking_id)
    if booking is None:
        return _error("预约不存在。", 404)
    if booking.user_id != actor.id and not actor.can_review:
        return _error("只能查看自己的预约。", 403)
    return _ok({"booking": _booking_json(booking)})


@api_bp.delete("/bookings/<int:booking_id>")
def cancel_booking(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    actor, err = _actor()
    if err:
        return err
    booking = db.session.get(Booking, booking_id)
    if booking is None:
        return _error("预约不存在。", 404)
    if booking.user_id != actor.id:
        return _error("只能取消自己的预约。", 403)
    if booking.status not in ("pending", "approved"):
        return _error("该预约当前状态不可取消。", 400)
    if not booking.can_self_change():
        contacts = format_contacts_label(find_booking_contacts(booking))
        return _error(f"{BOOKING_CHANGE_LOCKED_NOTICE} 值班或可联系管理员：{contacts}", 400)
    booking.status = "cancelled"
    db.session.commit()
    record_audit("booking.cancel", actor=actor, summary="通过 API 执行：booking.cancel。", target_type="booking", target_id=booking.id, reversible=False)
    return _ok({"booking": _booking_json(booking)})


@api_bp.post("/conflicts")
def check_conflicts():
    actor, err = _actor()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    lab, booking_date, start, end, _purpose, people_count = _parse_booking_payload(data)
    exclude_id = data.get("exclude_id")
    try:
        exclude_id = int(exclude_id) if exclude_id is not None and str(exclude_id) != "" else None
    except (TypeError, ValueError):
        return _error("exclude_id 无效。", 400)
    if people_count is None:
        people_count = 1
    errors = validate_booking(
        lab, booking_date, start, end, people_count, exclude_id=exclude_id, check_past=True
    )
    approved, pending = ([], [])
    if lab and booking_date and start and end:
        approved, pending = find_conflicts(lab.id, booking_date, start, end, exclude_id=exclude_id)
    return _ok(
        {
            "valid": not errors,
            "errors": errors,
            "approved_conflicts": [_conflict_json(b, actor) for b in approved],
            "pending_conflicts": [_conflict_json(b, actor) for b in pending],
        }
    )


@api_bp.post("/bookings/<int:booking_id>/approve")
def approve_booking(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    actor, err = _actor()
    if err:
        return err
    if not actor.can_review:
        return _error("需要干部或管理员权限。", 403)
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.status != "pending":
        return _error("只能审核待审核的预约。", 400)
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
        return _error(" ".join(errors), 400)
    _approved, pending = find_conflicts(
        booking.lab_id, booking.date, booking.start_time, booking.end_time, exclude_id=booking.id
    )
    booking.status = "approved"
    booking.reviewed_by_id = actor.id
    booking.reviewed_at = local_now()
    booking.reject_reason = None
    db.session.commit()
    record_audit("booking.approve", actor=actor, summary="通过 API 执行：booking.approve。", target_type="booking", target_id=booking.id, reversible=False)
    payload = {"booking": _booking_json(booking)}
    if pending:
        payload["warning"] = "仍有重叠的待审核预约，通过那些预约时将被拒绝。"
        payload["pending_conflicts"] = [_conflict_json(b, actor) for b in pending]
    return _ok(payload)


@api_bp.post("/bookings/<int:booking_id>/reject")
def reject_booking(booking_id):
    if request.method != "GET":
        begin_serialized_write()
    actor, err = _actor()
    if err:
        return err
    if not actor.can_review:
        return _error("需要干部或管理员权限。", 403)
    booking = db.session.get(Booking, booking_id)
    if booking is None or booking.status != "pending":
        return _error("只能审核待审核的预约。", 400)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return _error("驳回时请填写理由。", 400)
    booking.status = "rejected"
    booking.reject_reason = reason[:200]
    booking.reviewed_by_id = actor.id
    booking.reviewed_at = local_now()
    db.session.commit()
    record_audit("booking.reject", actor=actor, summary="通过 API 执行：booking.reject。", target_type="booking", target_id=booking.id, reversible=False)
    mail_log = send_reject_notice(booking, booking.reject_reason)
    payload = {"booking": _booking_json(booking), "mail_status": mail_log.status}
    if mail_log.status == "failed":
        payload["mail_warning"] = "拒绝已保存，但邮件发送失败，已记入发送记录。"
    return _ok(payload)


@api_bp.get("/stats")
def stats():
    from app.routes.stats import _filtered_bookings, _minutes_label, _summarize

    actor, err = _actor()
    if err:
        return err
    if not actor.can_view_stats:
        return _error("需要干部或管理员权限。", 403)
    start = parse_date(request.args.get("start_date"))
    end = parse_date(request.args.get("end_date"))
    if start is None or end is None:
        return _error("请提供 start_date 和 end_date（YYYY-MM-DD）。", 400)
    if start > end:
        start, end = end, start
    lab_id = request.args.get("lab_id", type=int)
    user_id = request.args.get("user_id", type=int)
    bookings = _filtered_bookings(start, end, lab_id, user_id)
    lab_rows, user_rows, totals = _summarize(bookings)
    return _ok(
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "totals": {
                "count": totals["count"],
                "minutes": totals["minutes"],
                "duration_label": _minutes_label(totals["minutes"]),
            },
            "by_lab": [
                {**row, "duration_label": _minutes_label(row["minutes"])} for row in lab_rows
            ],
            "by_user": [
                {**row, "duration_label": _minutes_label(row["minutes"])} for row in user_rows
            ],
        }
    )
