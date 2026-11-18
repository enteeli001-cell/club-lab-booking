"""Announcement board: permissions, forced popups, read tracking and level-3 mail round."""

from __future__ import annotations

from datetime import datetime

from flask import current_app, g
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models import (
    REGULAR_ROLES,
    Announcement,
    AnnouncementAck,
    AnnouncementMail,
    AnnouncementRead,
    Booking,
    User,
)

TITLE_MAX = 100
PUBLISHER_MAX = 30
BODY_MAX = 5000


# ----------------------------------------------------------------- permissions

def can_manage(user, announcement) -> bool:
    """Superadmin: every announcement. Admin: only their own. Everyone else: none."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superadmin:
        return True
    return bool(user.is_admin and announcement.created_by_id == user.id)


def allowed_levels(user) -> tuple[int, ...]:
    return (1, 2, 3) if user.is_superadmin else (1,)


# --------------------------------------------------------------------- targets

def target_users(exclude_id: int | None = None) -> list:
    """Valid captains and special accounts: the people who are expected to read."""
    from app.utils import today

    day = today()
    query = User.query.filter(
        User.role.in_(REGULAR_ROLES),
        User.enabled.is_(True),
        or_(User.valid_until.is_(None), User.valid_until >= day),
    )
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.order_by(User.student_id.asc()).all()


def resolve_emails(users) -> dict[int, str]:
    """Account email first, else the email on the user's latest booking form; users without one are absent."""
    result: dict[int, str] = {}
    missing = []
    for user in users:
        email = (user.last_email or "").strip()
        if email:
            result[user.id] = email
        else:
            missing.append(user.id)
    if missing:
        rows = (
            db.session.query(Booking.user_id, Booking.captain_email)
            .filter(Booking.user_id.in_(missing), Booking.captain_email.isnot(None), Booking.captain_email != "")
            .order_by(Booking.created_at.asc(), Booking.id.asc())
            .all()
        )
        for user_id, email in rows:  # later rows overwrite earlier ones: latest wins
            if (email or "").strip():
                result[user_id] = email.strip()
    return result


# ------------------------------------------------------------ read / ack state

def record_read(user, announcement) -> bool:
    """Store the first time `user` opened the announcement. Returns True if a row was created."""
    existing = AnnouncementRead.query.filter_by(announcement_id=announcement.id, user_id=user.id).first()
    if existing is not None:
        return False
    try:
        with db.session.begin_nested():
            db.session.add(AnnouncementRead(announcement_id=announcement.id, user_id=user.id))
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False


def pending_popups(user) -> list:
    """Level 2/3 announcements the user still has to confirm: highest level first, then newest."""
    if user.must_change_password and not user.is_admin:
        return []
    ack = AnnouncementAck
    return (
        Announcement.query.options(selectinload(Announcement.creator))
        .outerjoin(ack, and_(ack.announcement_id == Announcement.id, ack.user_id == user.id))
        .filter(
            Announcement.level >= 2,
            Announcement.created_by_id != user.id,
            or_(ack.id.is_(None), ack.revision < Announcement.ack_revision),
        )
        .order_by(Announcement.level.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .all()
    )


def unread_count(user) -> int:
    read = AnnouncementRead
    return (
        db.session.query(func.count(Announcement.id))
        .outerjoin(read, and_(read.announcement_id == Announcement.id, read.user_id == user.id))
        .filter(read.id.is_(None))
        .scalar()
        or 0
    )


def page_context(user) -> dict:
    """Per-request cached popup list and unread badge for base.html."""
    cached = getattr(g, "_announcement_ctx", None)
    if cached is not None and cached[0] == user.id:
        return cached[1]
    ctx = {"popups": pending_popups(user), "unread": unread_count(user)}
    g._announcement_ctx = (user.id, ctx)
    return ctx


def acknowledge(user, items: list[tuple[int, int]], ip: str | None) -> list[int]:
    """Confirm (announcement_id, revision) pairs shown in the popup. Ack also counts as having read it."""
    from app.utils import local_now

    acked = []
    now = local_now()
    for announcement_id, revision in items:
        announcement = db.session.get(Announcement, announcement_id)
        if announcement is None or announcement.level < 2:
            continue
        # Never record a revision the user could not have seen.
        revision = max(1, min(revision, announcement.ack_revision))
        row = AnnouncementAck.query.filter_by(announcement_id=announcement_id, user_id=user.id).first()
        if row is None:
            try:
                with db.session.begin_nested():
                    db.session.add(
                        AnnouncementAck(
                            announcement_id=announcement_id,
                            user_id=user.id,
                            revision=revision,
                            acked_at=now,
                            ack_ip=ip,
                        )
                    )
            except IntegrityError:
                row = AnnouncementAck.query.filter_by(announcement_id=announcement_id, user_id=user.id).first()
        if row is not None and revision > row.revision:
            row.revision = revision
            row.acked_at = now
            row.ack_ip = ip
        db.session.commit()
        record_read(user, announcement)
        acked.append(announcement_id)
    return acked


def reader_stats(announcements, targets=None) -> dict[int, tuple[int, int]]:
    """{announcement_id: (read_count, should_read_count)} counting only valid captains/special accounts."""
    announcements = list(announcements)
    if not announcements:
        return {}
    targets = targets if targets is not None else target_users()
    target_ids = {u.id for u in targets}
    counts = {a.id: 0 for a in announcements}
    rows = (
        db.session.query(AnnouncementRead.announcement_id, AnnouncementRead.user_id)
        .filter(AnnouncementRead.announcement_id.in_(list(counts)))
        .all()
    )
    for announcement_id, user_id in rows:
        if user_id in target_ids:
            counts[announcement_id] += 1
    return {a.id: (counts[a.id], len(target_ids)) for a in announcements}


def reads_report(announcement) -> dict:
    """Rows for the “阅读情况” page: every valid captain/special account with read, confirm and mail state."""
    targets = target_users(exclude_id=announcement.created_by_id)
    reads = {
        r.user_id: r.read_at
        for r in AnnouncementRead.query.filter_by(announcement_id=announcement.id).all()
    }
    acks = {a.user_id: a for a in AnnouncementAck.query.filter_by(announcement_id=announcement.id).all()}
    mails = {}
    if announcement.level == 3 and announcement.mail_revision:
        for mail in AnnouncementMail.query.filter_by(
            announcement_id=announcement.id, mail_revision=announcement.mail_revision
        ):
            mails[mail.user_id] = mail
    rows = []
    for user in targets:
        ack = acks.get(user.id)
        mail = mails.get(user.id)
        rows.append(
            {
                "user": user,
                "read_at": reads.get(user.id),
                "ack": ack if ack and ack.revision >= announcement.ack_revision else None,
                "mail": mail,
                "mail_label": MAIL_LABELS.get(mail.status, mail.status) if mail else ("待发送" if announcement.level == 3 else None),
            }
        )
    rows.sort(key=lambda r: (r["read_at"] is None, r["read_at"] or datetime.min, r["user"].student_id))
    read_count = sum(1 for r in rows if r["read_at"])
    return {"rows": rows, "read_count": read_count, "total": len(rows)}


MAIL_LABELS = {
    "pending": "待发送",
    "sent": "已发送",
    "local": "已发送（本地归档）",
    "failed": "发送失败",
    "no_email": "无邮箱",
}


# ------------------------------------------------------------------- mail scan

def mail_subject(announcement) -> str:
    prefix = "【更新】" if announcement.mail_revision > 1 else ""
    return f"【社团实验室预约管理系统】{prefix}重要公告：{announcement.title}"[:190]


def mail_body(announcement, user) -> str:
    who = announcement.publisher_display
    stamp = (announcement.updated_at or announcement.created_at).strftime("%Y-%m-%d %H:%M")
    return (
        f"{user.name}，你好：\n\n"
        f"系统发布了一条三级（最高级）公告：\n\n"
        f"标题：{announcement.title}\n"
        f"发布人：{who}\n"
        f"时间：{stamp}\n\n"
        f"{announcement.body}\n\n"
        f"请登录系统，在“公告”页查看，并在弹窗中点击“我已知晓”。\n"
        f"（此邮件由系统自动发送）\n"
    )


def scan_announcement_mail(now: datetime | None = None) -> dict:
    """Send each level-3 announcement once per recipient (idempotent; retries failures at most twice).

    A recipient row (announcement, user, mail round) is inserted first under a unique key, then claimed with
    a conditional UPDATE on `attempts`, so two workers can never send the same mail twice.
    """
    from app.mailer import send_mail
    from app.utils import local_now

    now = now or local_now()
    max_attempts = 1 + int(current_app.config.get("ANNOUNCEMENT_MAIL_MAX_RETRIES", 2))
    sent = failed = skipped = 0
    announcements = Announcement.query.filter(
        Announcement.level == 3,
        Announcement.mail_revision > 0,
        Announcement.mail_done_revision < Announcement.mail_revision,
    ).all()
    for announcement in announcements:
        announcement_id = announcement.id
        revision = announcement.mail_revision
        users = target_users(exclude_id=announcement.created_by_id)
        existing = {
            m.user_id: m
            for m in AnnouncementMail.query.filter_by(announcement_id=announcement_id, mail_revision=revision)
        }
        todo = [
            u
            for u in users
            if u.id not in existing
            or (existing[u.id].status in ("pending", "failed") and existing[u.id].attempts < max_attempts)
        ]
        emails = resolve_emails(todo)
        for user in todo:
            row = existing.get(user.id)
            if row is None:
                row = AnnouncementMail(
                    announcement_id=announcement_id, user_id=user.id, mail_revision=revision, status="pending"
                )
                db.session.add(row)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    continue
            email = emails.get(user.id)
            if not email:
                row.status = "no_email"
                row.updated_at = now
                db.session.commit()
                skipped += 1
                continue
            row_id, seen = row.id, row.attempts
            claim = db.session.execute(
                text(
                    "UPDATE announcement_mails SET attempts = attempts + 1, email_to = :email "
                    "WHERE id = :id AND status IN ('pending', 'failed') AND attempts = :seen"
                ),
                {"id": row_id, "seen": seen, "email": email},
            )
            db.session.commit()
            if claim.rowcount != 1:
                continue
            db.session.expire_all()
            announcement = db.session.get(Announcement, announcement_id)
            if announcement is None:
                break
            try:
                log = send_mail(email, mail_subject(announcement), mail_body(announcement, user))
                status = log.status if log.status in ("sent", "local") else "failed"
            except Exception:
                db.session.rollback()
                current_app.logger.exception("三级公告邮件发送失败")
                status = "failed"
            db.session.execute(
                text("UPDATE announcement_mails SET status = :status, updated_at = :now WHERE id = :id"),
                {"status": status, "now": now.strftime("%Y-%m-%d %H:%M:%S.%f"), "id": row_id},
            )
            db.session.commit()
            if status == "failed":
                failed += 1
            else:
                sent += 1
        # Done when nothing is left to send or retry for this round.
        left = AnnouncementMail.query.filter(
            AnnouncementMail.announcement_id == announcement_id,
            AnnouncementMail.mail_revision == revision,
            AnnouncementMail.status.in_(("pending", "failed")),
            AnnouncementMail.attempts < max_attempts,
        ).count()
        if not left:
            db.session.execute(
                text(
                    "UPDATE announcements SET mail_done_revision = :rev "
                    "WHERE id = :id AND mail_revision = :rev"
                ),
                {"rev": revision, "id": announcement_id},
            )
            db.session.commit()
    return {"sent": sent, "failed": failed, "skipped_no_email": skipped}
