"""Superadmin views: regular-user activity, admin activity, blacklist, undo."""

from datetime import datetime, time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_

from app import db
from app.transactions import begin_serialized_write
from app.activity import (
    active_blacklist_hits,
    client_ip,
    current_device_id,
    load_payload,
    record_audit,
)
from app.decorators import superadmin_required
from app.models import AuditLog, Blacklist, LoginLog, REGULAR_ROLES, STAFF_ROLES, ToolCatalog, User
from app.utils import local_now, parse_date

activity_bp = Blueprint("activity", __name__, url_prefix="/activity")


def _parse_filters():
    q = (request.args.get("q") or "").strip()
    ip = (request.args.get("ip") or "").strip()
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    start_dt = datetime.combine(start, time.min) if start else None
    end_dt = datetime.combine(end, time.max) if end else None
    return q, ip, start, end, start_dt, end_dt


def _filter_logs(query, model, q, ip, start_dt, end_dt, *, staff: bool):
    if ip:
        query = query.filter(model.ip == ip)
    if start_dt:
        query = query.filter(model.created_at >= start_dt)
    if end_dt:
        query = query.filter(model.created_at <= end_dt)
    if q:
        like = f"%{q}%"
        if model is LoginLog:
            query = query.filter(or_(LoginLog.student_id.ilike(like), LoginLog.role.ilike(like)))
        else:
            query = query.filter(
                or_(
                    AuditLog.actor_student_id.ilike(like),
                    AuditLog.summary.ilike(like),
                    AuditLog.action.ilike(like),
                )
            )
    return query


@activity_bp.route("/users")
@superadmin_required
def users():
    q, ip, start, end, start_dt, end_dt = _parse_filters()
    logins = (
        _filter_logs(LoginLog.query, LoginLog, q, ip, start_dt, end_dt, staff=False)
        .filter(or_(LoginLog.role.in_(REGULAR_ROLES), LoginLog.role.is_(None)))
        .order_by(LoginLog.created_at.desc())
        .limit(300)
        .all()
    )
    actions = (
        _filter_logs(AuditLog.query, AuditLog, q, ip, start_dt, end_dt, staff=False)
        .filter(or_(AuditLog.actor_role.in_(REGULAR_ROLES), AuditLog.actor_role.is_(None)))
        .order_by(AuditLog.created_at.desc())
        .limit(300)
        .all()
    )
    counts = {}
    for row in (
        db.session.query(LoginLog.student_id, db.func.count(LoginLog.id))
        .filter(LoginLog.success.is_(True), LoginLog.student_id.isnot(None))
        .group_by(LoginLog.student_id)
        .all()
    ):
        counts[row[0]] = row[1]
    blocks = Blacklist.query.order_by(Blacklist.created_at.desc()).limit(100).all()
    return render_template(
        "activity/users.html",
        logins=logins,
        actions=actions,
        counts=counts,
        blocks=blocks,
        q=q,
        ip=ip,
        start=start.isoformat() if start else "",
        end=end.isoformat() if end else "",
        current_ip=client_ip(),
        current_device=current_device_id(),
        now=local_now(),
    )


@activity_bp.route("/admins")
@superadmin_required
def admins():
    q, ip, start, end, start_dt, end_dt = _parse_filters()
    logins = (
        _filter_logs(LoginLog.query, LoginLog, q, ip, start_dt, end_dt, staff=True)
        .filter(LoginLog.role.in_(STAFF_ROLES))
        .order_by(LoginLog.created_at.desc())
        .limit(300)
        .all()
    )
    logs = (
        _filter_logs(AuditLog.query, AuditLog, q, ip, start_dt, end_dt, staff=True)
        .filter(AuditLog.actor_role.in_(STAFF_ROLES))
        .order_by(AuditLog.created_at.desc())
        .limit(300)
        .all()
    )
    return render_template(
        "activity/admins.html",
        logins=logins,
        logs=logs,
        q=q,
        ip=ip,
        start=start.isoformat() if start else "",
        end=end.isoformat() if end else "",
    )


@activity_bp.route("/blacklist", methods=["POST"])
@superadmin_required
def add_block():
    kind = (request.form.get("kind") or "").strip()
    value = (request.form.get("value") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    expires_raw = (request.form.get("expires_at") or "").strip()
    if kind not in ("ip", "device"):
        flash("请选择拉黑类型：IP 或设备。", "error")
        return redirect(url_for("activity.users"))
    if not value:
        flash("请填写要拉黑的 IP 或设备 ID。", "error")
        return redirect(url_for("activity.users"))
    if not reason:
        flash("请填写拉黑原因。", "error")
        return redirect(url_for("activity.users"))
    my_ip = client_ip()
    my_device = current_device_id()
    if kind == "ip" and my_ip and value == my_ip:
        flash("不能拉黑自己当前的 IP，否则可能无法继续管理。", "error")
        return redirect(url_for("activity.users"))
    if kind == "device" and my_device and value == my_device:
        flash("不能拉黑自己当前的设备，否则可能无法继续管理。", "error")
        return redirect(url_for("activity.users"))
    expires_at = None
    if expires_raw:
        try:
            expires_at = datetime.strptime(expires_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("到期时间格式无效。", "error")
            return redirect(url_for("activity.users"))
    row = Blacklist(
        created_by_id=current_user.id,
        kind=kind,
        value=value,
        reason=reason,
        expires_at=expires_at,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    record_audit(
        "blacklist.add",
        summary=f"拉黑{('IP' if kind == 'ip' else '设备')} {value}" + ("（永久）" if not expires_at else f"（至 {expires_at}）") + f"：{reason}",
        target_type="blacklist",
        target_id=row.id,
        payload={"kind": kind, "value": value, "reason": reason},
        reversible=False,
        undo_blocked_reason="请使用「解除拉黑」。",
    )
    flash("已加入拉黑名单。", "success")
    return redirect(url_for("activity.users"))


@activity_bp.route("/blacklist/<int:block_id>/lift", methods=["POST"])
@superadmin_required
def lift_block(block_id):
    row = db.session.get(Blacklist, block_id)
    if row is None:
        flash("记录不存在。", "error")
        return redirect(url_for("activity.users"))
    row.active = False
    row.lifted_at = local_now()
    row.lifted_by_id = current_user.id
    db.session.commit()
    record_audit(
        "blacklist.lift",
        summary=f"解除拉黑{('IP' if row.kind == 'ip' else '设备')} {row.value}。",
        target_type="blacklist",
        target_id=row.id,
        reversible=False,
        undo_blocked_reason="如需再次限制，请重新拉黑。",
    )
    flash("已解除拉黑。", "success")
    return redirect(url_for("activity.users"))


def _undo_review(log):
    from app.models import Booking

    booking = db.session.get(Booking, log.target_id)
    if booking is None:
        return False, "预约记录已不存在。"
    if booking.status not in ("approved", "rejected"):
        return False, f"当前状态为「{booking.status_label}」，无法撤销审核。"
    expected_status = {"booking.approve": "approved", "booking.reject": "rejected"}.get(log.action)
    if booking.status != expected_status or booking.reviewed_by_id != log.actor_id or (booking.reviewed_at and log.created_at and booking.reviewed_at > log.created_at):
        return False, "该预约已有后续审核，不能撤销历史审核。"
    old = booking.status
    booking.status = "pending"
    booking.reviewed_by_id = None
    booking.reviewed_at = None
    if old == "rejected":
        booking.reject_reason = None
    db.session.commit()
    note = "已撤销审核并恢复为待审核。"
    if old == "rejected":
        note += " 若拒绝理由邮件已发出，无法撤回。"
    return True, note


def _undo_disable(log):
    user = db.session.get(User, log.target_id)
    if user is None:
        return False, "账号已不存在。"
    if user.enabled:
        return False, "该账号当前已是启用状态。"
    user.enabled = True
    db.session.commit()
    return True, f"已重新启用 {user.name}（{user.student_id}）。"


def _undo_catalog(log):
    payload = load_payload(log)
    if log.action == "catalog.update":
        item = db.session.get(ToolCatalog, log.target_id)
        before = payload.get("before") or {}
        if item is None:
            return False, "工具清单项已不存在。"
        if not before:
            return False, "日志中没有可恢复的修改前数据。"
        from app.inventory import borrowed_quantity
        if before.get("total_quantity", item.total_quantity) < borrowed_quantity(item.id):
            return False, "恢复后的总数量少于当前已借出数量，不能撤销。"
        item.name = before.get("name", item.name)
        item.total_quantity = before.get("total_quantity", item.total_quantity)
        item.is_major = before.get("is_major", item.is_major)
        db.session.commit()
        return True, f"已撤销对「{item.name}」的清单修改。"
    if log.action == "catalog.create":
        item = db.session.get(ToolCatalog, log.target_id)
        if item is None:
            return False, "该工具已不存在。"
        from app.models import ToolLoanItem

        if ToolLoanItem.query.filter_by(catalog_id=item.id).count():
            return False, "该工具已有借用记录，不能撤销新增。"
        name = item.name
        db.session.delete(item)
        db.session.commit()
        return True, f"已撤销新增工具「{name}」。"
    if log.action == "catalog.delete":
        snap = payload.get("item") or payload
        if not snap.get("name"):
            return False, "日志中没有可恢复的工具数据。"
        if ToolCatalog.query.filter_by(name=snap["name"]).first():
            return False, "已存在同名工具，无法恢复删除。"
        item = ToolCatalog(
            name=snap["name"],
            total_quantity=int(snap.get("total_quantity") or 0),
            is_major=bool(snap.get("is_major", True)),
        )
        db.session.add(item)
        db.session.commit()
        return True, f"已恢复工具「{item.name}」。"
    return False, "不支持撤销该清单操作。"


_UNDOERS = {
    "booking.approve": _undo_review,
    "booking.reject": _undo_review,
    "account.disable": _undo_disable,
    "catalog.update": _undo_catalog,
    "catalog.create": _undo_catalog,
    "catalog.delete": _undo_catalog,
}


@activity_bp.route("/logs/<int:log_id>/undo", methods=["POST"])
@superadmin_required
def undo(log_id):
    if request.method != "GET":
        begin_serialized_write()
    log = db.session.get(AuditLog, log_id)
    if log is None:
        flash("日志不存在。", "error")
        return redirect(url_for("activity.admins"))
    if log.undone_at:
        flash("该操作已经撤销过。", "error")
        return redirect(url_for("activity.admins"))
    if not log.reversible:
        flash(log.undo_blocked_reason or "该操作不可撤销。", "error")
        return redirect(url_for("activity.admins"))
    handler = _UNDOERS.get(log.action)
    if handler is None:
        flash("该操作类型暂不支持撤销。", "error")
        return redirect(url_for("activity.admins"))
    ok, message = handler(log)
    if not ok:
        flash(message, "error")
        return redirect(url_for("activity.admins"))
    log.undone_at = local_now()
    log.undone_by_id = current_user.id
    db.session.commit()
    record_audit(
        "audit.undo",
        summary=f"撤销操作 #{log.id}（{log.action}）：{message}",
        target_type="audit",
        target_id=log.id,
        payload={"undo_of": log.id, "action": log.action},
        reversible=False,
        undo_blocked_reason="撤销本身不能再撤销。",
    )
    flash(message, "success")
    return redirect(url_for("activity.admins"))
