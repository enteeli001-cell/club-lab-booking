"""Superadmin security dashboard — passive records only, no outbound probing."""

from datetime import timedelta
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from app import db
from app.activity import client_ip, current_device_id, record_audit
from app.decorators import superadmin_required
from app.models import Blacklist, SecurityEvent, SecurityWhitelist
from app.security import (
    EVENT_LABELS,
    SEVERITY_LABELS,
    is_default_whitelisted_ip,
    mark_high_events_read,
    unread_high_count,
)
from app.utils import local_now, parse_date

security_bp = Blueprint("security", __name__, url_prefix="/security")


def _filtered_query():
    q = SecurityEvent.query
    typ = (request.args.get("type") or "").strip()
    sev = (request.args.get("severity") or "").strip()
    ip = (request.args.get("ip") or "").strip()
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    if typ and typ in EVENT_LABELS:
        q = q.filter_by(event_type=typ)
    if sev and sev in SEVERITY_LABELS:
        q = q.filter_by(severity=sev)
    if ip:
        q = q.filter(SecurityEvent.ip.contains(ip))
    if start:
        q = q.filter(SecurityEvent.created_at >= start)
    if end:
        q = q.filter(SecurityEvent.created_at < end + timedelta(days=1))
    return q, typ, sev, ip, start, end


def _counts_since(since):
    rows = (
        db.session.query(SecurityEvent.event_type, db.func.count(SecurityEvent.id))
        .filter(SecurityEvent.created_at >= since)
        .group_by(SecurityEvent.event_type)
        .all()
    )
    return {typ: n for typ, n in rows}


def _top_ips(since, limit=8):
    return (
        db.session.query(SecurityEvent.ip, db.func.count(SecurityEvent.id).label("n"))
        .filter(SecurityEvent.created_at >= since, SecurityEvent.ip.isnot(None))
        .group_by(SecurityEvent.ip)
        .order_by(db.func.count(SecurityEvent.id).desc())
        .limit(limit)
        .all()
    )


def _active_block(kind, value):
    if not value:
        return None
    rows = Blacklist.query.filter_by(kind=kind, value=value, active=True).all()
    now = local_now()
    for row in rows:
        if row.is_in_effect(now):
            return row
    return None


@security_bp.route("/")
@superadmin_required
def dashboard():
    mark_high_events_read()
    q, typ, sev, ip, start, end = _filtered_query()
    events = q.order_by(SecurityEvent.created_at.desc()).limit(200).all()
    now = local_now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    latest_high = (
        SecurityEvent.query.filter(SecurityEvent.severity.in_(("high", "critical")))
        .order_by(SecurityEvent.created_at.desc())
        .limit(8)
        .all()
    )
    whitelist = SecurityWhitelist.query.order_by(SecurityWhitelist.id.desc()).all()
    active_blocks = [
        b for b in Blacklist.query.filter_by(active=True).order_by(Blacklist.id.desc()).limit(80).all()
        if b.is_in_effect(now)
    ]
    today_counts = _counts_since(today)
    week_counts = _counts_since(week)
    return render_template(
        "security/index.html",
        events=events,
        event_labels=EVENT_LABELS,
        severity_labels=SEVERITY_LABELS,
        typ=typ,
        sev=sev,
        ip=ip,
        start=start.isoformat() if start else "",
        end=end.isoformat() if end else "",
        today_counts=today_counts,
        week_counts=week_counts,
        today_total=sum(today_counts.values()),
        week_total=sum(week_counts.values()),
        top_ips=_top_ips(week),
        latest_high=latest_high,
        whitelist=whitelist,
        active_blocks=active_blocks,
        unread=0,
    )


@security_bp.route("/export.xlsx")
@superadmin_required
def export_xlsx():
    q, typ, sev, ip, start, end = _filtered_query()
    rows = q.order_by(SecurityEvent.created_at.desc()).limit(2000).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "security-events"
    headers = [
        "时间", "类型", "级别", "IP", "国家", "Cloudflare头", "设备ID",
        "账号", "方法", "路径", "参数摘要", "Referer", "说明", "24小时累计", "是否自动拉黑",
    ]
    ws.append(headers)
    for ev in rows:
        ws.append([
            ev.created_at.strftime("%Y-%m-%d %H:%M:%S") if ev.created_at else "",
            EVENT_LABELS.get(ev.event_type, ev.event_type),
            SEVERITY_LABELS.get(ev.severity, ev.severity),
            ev.ip or "",
            ev.country or "",
            ev.cf_headers or "",
            ev.device_id or "",
            ev.student_id or "",
            ev.method or "",
            ev.path or "",
            ev.query_summary or "",
            ev.referer or "",
            ev.detail or "",
            ev.count,
            "是" if ev.auto_blocked else "否",
        ])
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    record_audit("security.export", summary="导出安全事件 Excel。", reversible=False, undo_blocked_reason="导出不可撤回。")
    return send_file(
        buf,
        as_attachment=True,
        download_name="security-events.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@security_bp.route("/events/<int:event_id>/block", methods=["POST"])
@superadmin_required
def block_event(event_id):
    ev = db.session.get(SecurityEvent, event_id)
    if ev is None:
        flash("事件不存在。", "error")
        return redirect(url_for("security.dashboard"))
    kind = (request.form.get("kind") or "ip").strip()
    if kind not in ("ip", "device"):
        kind = "ip"
    value = ev.ip if kind == "ip" else ev.device_id
    if not value:
        flash("该事件没有可拉黑的 IP 或设备。", "error")
        return redirect(url_for("security.dashboard"))
    my_ip = client_ip()
    my_device = current_device_id()
    if kind == "ip" and (value == my_ip or is_default_whitelisted_ip(value)):
        flash("不能拉黑自己当前的 IP 或本机直连地址。", "error")
        return redirect(url_for("security.dashboard"))
    if kind == "device" and my_device and value == my_device:
        flash("不能拉黑自己当前的设备。", "error")
        return redirect(url_for("security.dashboard"))
    if _active_block(kind, value):
        flash("该目标已在拉黑名单中。", "warning")
        return redirect(url_for("security.dashboard"))
    hours = (request.form.get("hours") or "24").strip()
    try:
        hours_n = int(hours)
    except ValueError:
        hours_n = 24
    expires = local_now() + timedelta(hours=max(1, hours_n)) if hours_n > 0 else None
    row = Blacklist(
        created_by_id=current_user.id,
        kind=kind,
        value=value,
        reason=f"安全防护中心手动拉黑（事件 #{ev.id}）",
        expires_at=expires,
        active=True,
    )
    db.session.add(row)
    db.session.commit()
    record_audit(
        "blacklist.add",
        summary=f"从安全防护中心拉黑{('IP' if kind == 'ip' else '设备')} {value}。",
        target_type="blacklist",
        target_id=row.id,
        reversible=False,
        undo_blocked_reason="请使用解除拉黑。",
    )
    flash("已拉黑。", "success")
    return redirect(url_for("security.dashboard"))


@security_bp.route("/blocks/<int:block_id>/lift", methods=["POST"])
@superadmin_required
def lift_block(block_id):
    row = db.session.get(Blacklist, block_id)
    if row is None:
        flash("记录不存在。", "error")
        return redirect(url_for("security.dashboard"))
    row.active = False
    row.lifted_at = local_now()
    row.lifted_by_id = current_user.id
    db.session.commit()
    record_audit("blacklist.lift", summary=f"解除拉黑 {row.value}。", target_type="blacklist", target_id=row.id, reversible=False, undo_blocked_reason="可重新拉黑。")
    flash("已解除拉黑。", "success")
    return redirect(url_for("security.dashboard"))


@security_bp.route("/whitelist", methods=["POST"])
@superadmin_required
def add_whitelist():
    kind = (request.form.get("kind") or "").strip()
    value = (request.form.get("value") or "").strip()
    reason = (request.form.get("reason") or "").strip() or "手动加入白名单"
    if kind not in ("ip", "device") or not value:
        flash("请填写白名单类型和值。", "error")
        return redirect(url_for("security.dashboard"))
    if SecurityWhitelist.query.filter_by(kind=kind, value=value).first():
        flash("该值已在白名单中。", "warning")
        return redirect(url_for("security.dashboard"))
    row = SecurityWhitelist(created_by_id=current_user.id, kind=kind, value=value, reason=reason[:200])
    db.session.add(row)
    db.session.commit()
    record_audit("security.whitelist", summary=f"将{kind} {value}加入安全白名单。", reversible=False, undo_blocked_reason="可再删除白名单。")
    flash("已加入白名单。自动拉黑将跳过该值。", "success")
    return redirect(url_for("security.dashboard"))


@security_bp.route("/whitelist/<int:item_id>/delete", methods=["POST"])
@superadmin_required
def delete_whitelist(item_id):
    row = db.session.get(SecurityWhitelist, item_id)
    if row is None:
        flash("记录不存在。", "error")
        return redirect(url_for("security.dashboard"))
    if is_default_whitelisted_ip(row.value) and row.kind == "ip":
        flash("127.0.0.1 等本机直连地址为默认白名单，无需删除。", "warning")
        return redirect(url_for("security.dashboard"))
    db.session.delete(row)
    db.session.commit()
    flash("已移出白名单。", "success")
    return redirect(url_for("security.dashboard"))


# ---------------------------------------------------------------- personal data wipe

def _wipe_context(**extra):
    from app import wipe

    ctx = dict(
        phrase=wipe.WIPE_PHRASE,
        deleted=wipe.DELETED_SUMMARY,
        kept=wipe.KEPT_SUMMARY,
        locked=wipe.locked(),
        max_attempts=wipe.MAX_FAILED_ATTEMPTS,
        window_minutes=wipe.ATTEMPT_WINDOW_SECONDS // 60,
    )
    ctx.update(extra)
    return ctx


@security_bp.route("/wipe", methods=["GET"])
@superadmin_required
def wipe_page():
    return render_template("security/wipe.html", **_wipe_context())


@security_bp.route("/wipe/stats.xlsx")
@superadmin_required
def wipe_stats_xlsx():
    """Anonymised statistics: no names, student numbers, phones, mailboxes, purposes or locations."""
    from app.models import Booking

    wb = Workbook()
    ws = wb.active
    ws.title = "匿名预约统计"
    ws.append(["序号", "实验室", "日期", "开始", "结束", "状态", "人数", "是否签到", "已确认安全承诺", "匿名队伍编号"])
    labels = {}
    for index, booking in enumerate(Booking.query.order_by(Booking.date.asc(), Booking.start_time.asc(), Booking.id.asc()).all(), 1):
        team = labels.setdefault(booking.user_id, f"队伍{len(labels) + 1:03d}")
        ws.append([
            index, booking.lab.name if booking.lab else "", booking.date.isoformat(),
            booking.start_time.strftime("%H:%M"), booking.end_time.strftime("%H:%M"), booking.status_label,
            booking.people_count, "是" if booking.checked_in_at else "否", "是" if booking.pledge_accepted_at else "否", team,
        ])
    for column in range(1, 11):
        ws.column_dimensions[get_column_letter(column)].width = 16
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    record_audit("wipe.stats_export", summary="下载匿名统计 Excel（清除个人信息前）。", reversible=False, undo_blocked_reason="下载操作无需撤销。")
    return send_file(buf, as_attachment=True, download_name="匿名预约统计.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@security_bp.route("/wipe", methods=["POST"])
@superadmin_required
def wipe_run():
    from app import wipe

    def refuse(reason, message, status=400):
        record_audit("wipe.attempt", summary=f"清除个人信息被拒绝：{reason}。", reversible=False, undo_blocked_reason="未执行任何清除。")
        flash(message, "error")
        return render_template("security/wipe.html", **_wipe_context()), status

    if wipe.locked():
        return refuse("尝试次数过多，已临时锁定", f"失败次数过多，请 {wipe.ATTEMPT_WINDOW_SECONDS // 60} 分钟后再试。", 429)
    phrase = (request.form.get("phrase") or "").strip()
    password = request.form.get("password") or ""
    if phrase != wipe.WIPE_PHRASE:
        wipe.register_failure()
        return refuse("确认文字不正确", "确认文字不正确，必须完整输入：" + wipe.WIPE_PHRASE)
    if not password or not current_user.check_password(password):
        wipe.register_failure()
        return refuse("总管理员密码错误", "密码错误，未执行清除。")
    wipe.clear_failures()
    backup = None
    try:
        backup = wipe.make_final_backup()
        counts = wipe.wipe_personal_data()
    except Exception:
        db.session.rollback()
        from flask import current_app

        current_app.logger.exception("清除个人信息失败（数据库已回滚）")
        record_audit("wipe.attempt", summary="清除个人信息失败，数据库已回滚，未做任何更改。", reversible=False, undo_blocked_reason="未执行任何清除。")
        flash("清除失败，数据库已回滚，没有任何数据被修改。详情见服务器日志。", "error")
        return render_template("security/wipe.html", **_wipe_context()), 500
    counts.update(wipe.delete_data_files())
    wipe.vacuum()
    summary = "已清除个人信息：" + "，".join(f"{k}={v}" for k, v in counts.items()) + f"；备份 {backup.name}。"
    record_audit("wipe.done", summary=summary[:400], payload={"counts": counts, "backup": backup.name}, reversible=False, undo_blocked_reason="清除不可撤销。")
    from app.i18n import t

    flash(t("个人信息已全部清除。队长账号已停用并匿名化；清除前的数据库备份：") + backup.name, "success")
    return render_template("security/wipe.html", **_wipe_context(done=counts, backup=backup.name))
