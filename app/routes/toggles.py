"""总管理员：队长功能开关 + 维护模式。"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import toggles
from app.activity import record_audit
from app.decorators import superadmin_required

toggles_bp = Blueprint("toggles", __name__, url_prefix="/switches")


@toggles_bp.route("/")
@superadmin_required
def features():
    states = toggles.feature_states()
    rows = [{"key": key, "label": label, "desc": desc, "enabled": states[key]} for key, label, desc in toggles.FEATURES]
    return render_template("toggles/features.html", rows=rows, off_count=sum(1 for r in rows if not r["enabled"]))


@toggles_bp.route("/features/<key>", methods=["POST"])
@superadmin_required
def set_feature(key):
    if key not in toggles.FEATURE_LABELS:
        flash("未知的功能开关。", "error")
        return redirect(url_for("toggles.features"))
    value = request.form.get("enabled")
    if value not in {"0", "1"}:
        flash("请求无效。", "error")
        return redirect(url_for("toggles.features"))
    enabled = value == "1"
    label = toggles.FEATURE_LABELS[key]
    if toggles.feature_enabled(key) == enabled:
        flash(f"「{label}」已经是{'开启' if enabled else '关闭'}状态。", "info")
        return redirect(url_for("toggles.features"))
    toggles.set_feature(key, enabled)
    record_audit(
        "toggle.feature",
        summary=f"{'开启' if enabled else '关闭'}队长功能「{label}」。",
        target_type="feature",
        payload={"feature": key, "enabled": enabled},
        reversible=False,
        undo_blocked_reason="请在功能开关页面重新切换。",
    )
    flash(f"已{'开启' if enabled else '关闭'}「{label}」。", "success")
    return redirect(url_for("toggles.features"))


@toggles_bp.route("/maintenance")
@superadmin_required
def maintenance():
    return render_template(
        "toggles/maintenance.html",
        enabled=toggles.maintenance_enabled(),
        message=toggles.maintenance_message(),
        message_max=toggles.MAINTENANCE_MESSAGE_MAX,
        retry_minutes=toggles.MAINTENANCE_RETRY_AFTER // 60,
    )


@toggles_bp.route("/maintenance/message", methods=["POST"])
@superadmin_required
def maintenance_message():
    message = " ".join((request.form.get("message") or "").split())
    if not message:
        flash("提示文案不能为空。", "error")
    elif len(message) > toggles.MAINTENANCE_MESSAGE_MAX:
        flash(f"提示文案不能超过 {toggles.MAINTENANCE_MESSAGE_MAX} 个字。", "error")
    elif message == toggles.maintenance_message():
        flash("提示文案没有变化。", "info")
    else:
        old = toggles.maintenance_message()
        toggles.set_maintenance_message(message)
        record_audit(
            "maintenance.message",
            summary=f"修改维护模式提示文案为：{message}",
            target_type="maintenance",
            payload={"old": old, "new": message},
            reversible=False,
            undo_blocked_reason="请在维护模式页面重新修改。",
        )
        flash("提示文案已保存。", "success")
    return redirect(url_for("toggles.maintenance"))


@toggles_bp.route("/maintenance/state", methods=["POST"])
@superadmin_required
def maintenance_state():
    value = request.form.get("enabled")
    if value not in {"0", "1"}:
        flash("请求无效。", "error")
        return redirect(url_for("toggles.maintenance"))
    enabled = value == "1"
    if enabled and request.form.get("confirm") != "yes":
        flash("开启维护模式需要二次确认：请勾选确认项后再提交。", "error")
        return redirect(url_for("toggles.maintenance"))
    if toggles.maintenance_enabled() == enabled:
        flash(f"维护模式已经是{'开启' if enabled else '关闭'}状态。", "info")
        return redirect(url_for("toggles.maintenance"))
    toggles.set_maintenance(enabled)
    record_audit(
        "maintenance.on" if enabled else "maintenance.off",
        summary="开启维护模式（网站升级中），除总管理员外全部访问返回 503。" if enabled else "关闭维护模式，网站恢复正常访问。",
        target_type="maintenance",
        payload={"enabled": enabled},
        reversible=False,
        undo_blocked_reason="请在维护模式页面重新切换。",
    )
    flash("维护模式已开启：除总管理员外，所有访问者只会看到升级提示。" if enabled else "维护模式已关闭，网站恢复正常访问。",
          "warning" if enabled else "success")
    return redirect(url_for("toggles.maintenance"))
