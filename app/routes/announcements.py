"""Announcement board: list/detail for everyone, publishing and read reports for staff."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app import announcements as ann
from app import db
from app.activity import client_ip, record_audit
from app.decorators import lab_manager_required
from app.models import ANNOUNCEMENT_LEVEL_HINTS, ANNOUNCEMENT_LEVELS, Announcement, AnnouncementRead
from app.utils import local_now, safe_local_url

announcements_bp = Blueprint("announcements", __name__, url_prefix="/announcements")


def _get_or_404(announcement_id):
    announcement = db.session.get(Announcement, announcement_id)
    if announcement is None:
        abort(404)
    return announcement


def _manageable_or_403(announcement_id):
    announcement = _get_or_404(announcement_id)
    if not ann.can_manage(current_user, announcement):
        abort(403)
    return announcement


def _ordered(query):
    return query.order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())


@announcements_bp.route("/")
@login_required
def list_announcements():
    items = _ordered(Announcement.query.options(selectinload(Announcement.creator), selectinload(Announcement.updater))).all()
    read_ids = {
        row[0]
        for row in db.session.query(AnnouncementRead.announcement_id).filter(
            AnnouncementRead.user_id == current_user.id
        )
    }
    return render_template(
        "announcements/list.html", items=items, read_ids=read_ids, levels=ANNOUNCEMENT_LEVELS
    )


@announcements_bp.route("/<int:announcement_id>")
@login_required
def detail(announcement_id):
    announcement = _get_or_404(announcement_id)
    ann.record_read(current_user, announcement)
    return render_template(
        "announcements/detail.html",
        a=announcement,
        can_manage=ann.can_manage(current_user, announcement),
    )


@announcements_bp.route("/manage")
@lab_manager_required
def manage():
    query = Announcement.query.options(selectinload(Announcement.creator), selectinload(Announcement.updater))
    if not current_user.is_superadmin:
        query = query.filter(Announcement.created_by_id == current_user.id)
    items = _ordered(query).all()
    return render_template(
        "announcements/manage.html", items=items, stats=ann.reader_stats(items), scope_all=current_user.is_superadmin
    )


def _form_defaults(announcement=None):
    if announcement is None:
        return {"title": "", "body": "", "level": "1", "pinned": False, "publisher_name": current_user.name}
    return {
        "publisher_name": announcement.publisher_display,
        "title": announcement.title,
        "body": announcement.body,
        "level": str(announcement.level),
        "pinned": announcement.pinned,
    }


def _read_form(existing=None):
    """Validate the form. Level and pin are only taken from the request for the superadmin."""
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    publisher = " ".join((request.form.get("publisher_name") or "").split())
    errors = []
    if not publisher:
        errors.append("请填写发布人。")
    elif len(publisher) > ann.PUBLISHER_MAX:
        errors.append(f"发布人不能超过 {ann.PUBLISHER_MAX} 个字。")
    if not title:
        errors.append("请填写公告标题。")
    elif len(title) > ann.TITLE_MAX:
        errors.append(f"标题不能超过 {ann.TITLE_MAX} 个字。")
    if not body:
        errors.append("请填写公告内容。")
    elif len(body) > ann.BODY_MAX:
        errors.append(f"内容不能超过 {ann.BODY_MAX} 个字。")
    if current_user.is_superadmin:
        try:
            level = int(request.form.get("level") or 1)
        except ValueError:
            level = 0
        if level not in ANNOUNCEMENT_LEVELS:
            errors.append("请选择有效的公告等级。")
            level = existing.level if existing else 1
        pinned_requested = request.form.get("pinned") in ("1", "on", "true")
    else:
        # Admins publish level 1 only; when editing their own notice the level and pin stay as they are.
        level = existing.level if existing else 1
        pinned_requested = existing.pinned if existing else False
    if level == 3:
        pinned = True
    elif level == 2:
        pinned = pinned_requested
    else:
        pinned = False
    return {"title": title, "body": body, "level": level, "pinned": pinned, "publisher_name": publisher}, errors


@announcements_bp.route("/new", methods=["GET", "POST"])
@lab_manager_required
def create():
    form = _form_defaults()
    if request.method == "POST":
        data, errors = _read_form()
        form = {**data, "level": str(data["level"])}
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            announcement = Announcement(
                **data,
                created_by_id=current_user.id,
                created_at=local_now(),
                mail_revision=1 if data["level"] == 3 else 0,
            )
            db.session.add(announcement)
            db.session.commit()
            record_audit(
                "announcement.create",
                summary=f"发布{announcement.level_label}公告《{announcement.title}》。",
                target_type="announcement",
                target_id=announcement.id,
                payload={"level": announcement.level, "pinned": announcement.pinned, "publisher_name": announcement.publisher_name},
            )
            flash(
                "公告已发布。三级公告的邮件会由后台在一分钟内自动发送。" if announcement.level == 3 else "公告已发布。",
                "success",
            )
            return redirect(url_for("announcements.detail", announcement_id=announcement.id))
    return render_template(
        "announcements/form.html",
        a=None,
        form=form,
        levels=ANNOUNCEMENT_LEVELS,
        level_hints=ANNOUNCEMENT_LEVEL_HINTS,
    )


@announcements_bp.route("/<int:announcement_id>/edit", methods=["GET", "POST"])
@lab_manager_required
def edit(announcement_id):
    announcement = _manageable_or_403(announcement_id)
    form = _form_defaults(announcement)
    if request.method == "POST":
        data, errors = _read_form(announcement)
        form = {**data, "level": str(data["level"])}
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            is_super = current_user.is_superadmin
            reack = is_super and data["level"] >= 2 and request.form.get("reack") in ("1", "on", "true")
            resend = is_super and data["level"] == 3 and request.form.get("resend") in ("1", "on", "true")
            old_publisher = announcement.publisher_display
            changed = any(
                (old_publisher if key == "publisher_name" else getattr(announcement, key)) != value
                for key, value in data.items()
            )
            if changed or reack or resend:
                before_level = announcement.level
                for key, value in data.items():
                    setattr(announcement, key, value)
                announcement.updated_by_id = current_user.id
                announcement.updated_at = local_now()
                if reack:
                    announcement.ack_revision += 1
                if announcement.level == 3:
                    if announcement.mail_revision == 0:
                        announcement.mail_revision = 1  # first time at level 3: one mail round
                    elif resend:
                        announcement.mail_revision += 1
                db.session.commit()
                extra = []
                if old_publisher != announcement.publisher_name:
                    extra.append(f"发布人由“{old_publisher}”改为“{announcement.publisher_name}”")
                if reack:
                    extra.append("要求重新确认")
                if resend:
                    extra.append("重新发送邮件")
                record_audit(
                    "announcement.update",
                    summary=(
                        f"修改{'' if announcement.created_by_id == current_user.id else '他人发布的'}公告《{announcement.title}》"
                        + (f"（{'、'.join(extra)}）" if extra else "")
                        + "。"
                    ),
                    target_type="announcement",
                    target_id=announcement.id,
                    payload={
                        "owner_id": announcement.created_by_id,
                        "level": [before_level, announcement.level],
                        "publisher_name": [old_publisher, announcement.publisher_name],
                        "reack": reack,
                        "resend": resend,
                    },
                )
                flash("公告已更新。", "success")
            else:
                flash("内容没有变化。", "warning")
            return redirect(url_for("announcements.detail", announcement_id=announcement.id))
    return render_template(
        "announcements/form.html",
        a=announcement,
        form=form,
        levels=ANNOUNCEMENT_LEVELS,
        level_hints=ANNOUNCEMENT_LEVEL_HINTS,
    )


@announcements_bp.route("/<int:announcement_id>/delete", methods=["POST"])
@lab_manager_required
def delete(announcement_id):
    announcement = _manageable_or_403(announcement_id)
    title, owner_id, level = announcement.title, announcement.created_by_id, announcement.level
    mine = owner_id == current_user.id
    db.session.delete(announcement)
    db.session.commit()
    record_audit(
        "announcement.delete",
        summary=f"删除{'' if mine else '他人发布的'}公告《{title}》。",
        target_type="announcement",
        target_id=announcement_id,
        payload={"owner_id": owner_id, "level": level},
        reversible=False,
        undo_blocked_reason="公告及其阅读记录已删除，无法恢复。",
    )
    flash("公告已删除。", "success")
    return redirect(url_for("announcements.manage"))


@announcements_bp.route("/<int:announcement_id>/reads")
@lab_manager_required
def reads(announcement_id):
    announcement = _manageable_or_403(announcement_id)
    report = ann.reads_report(announcement)
    show = request.args.get("show", "all")
    rows = report["rows"]
    if show == "read":
        rows = [r for r in rows if r["read_at"]]
    elif show == "unread":
        rows = [r for r in rows if not r["read_at"]]
    else:
        show = "all"
    return render_template("announcements/reads.html", a=announcement, report=report, rows=rows, show=show)


@announcements_bp.route("/ack", methods=["POST"])
@login_required
def acknowledge():
    items = []
    for raw in request.form.getlist("items"):
        try:
            announcement_id, revision = (int(part) for part in raw.split(":", 1))
        except ValueError:
            continue
        items.append((announcement_id, revision))
    acked = ann.acknowledge(current_user, items, (client_ip() or "")[:64] or None)
    if acked:
        record_audit(
            "announcement.ack",
            summary=f"确认已知晓 {len(acked)} 条公告。",
            target_type="user",
            target_id=current_user.id,
            payload={"announcement_ids": acked},
            reversible=False,
            undo_blocked_reason="确认记录不可撤销。",
        )
    target = request.form.get("next") or ""
    if safe_local_url(target) and not target.startswith("/announcements/ack"):
        return redirect(target)
    return redirect(url_for("main.index"))
