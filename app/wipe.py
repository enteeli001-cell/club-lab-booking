"""One-click personal data wipe (super admin only, see routes/security.py).

Design: every table is listed explicitly in `wipe_personal_data`; tables that hold no personal data
(labs, tool catalog, duty shifts, app settings, ...) are left alone so anonymised statistics keep
working.  The whole database change happens in ONE transaction; files and VACUUM follow after the commit.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from flask import current_app
from sqlalchemy import update

from app import db
from app.models import (
    MaterialClaim,
    MaterialPurchase,
    EmailVerification,
    AnnouncementAck,
    AnnouncementMail,
    AuditLog,
    Blacklist,
    Booking,
    BookingCompanion,
    LoginLog,
    MailLog,
    NoShowAlert,
    PasswordResetLog,
    SecurityEvent,
    SecurityWhitelist,
    TeamMember,
    ToolLoan,
    ToolLoanItem,
    User,
)
from app.utils import local_now

WIPE_PHRASE = "确定消除所有个人信息"
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 15 * 60
CLEARED = "（已清除）"
MASK = "【已清除】"
STAFF = ("superadmin", "admin")

# (what is removed, shown on the confirmation page)
DELETED_SUMMARY = [
    "参赛队长、特殊账号：姓名、学号、队伍、手机、邮箱、单位、特批原因 → 改为「已注销-0001」式匿名编号，账号停用且密码作废",
    "预约申请：用途、队长手机和邮箱、驳回原因、签到 IP / 设备、签到经纬度、定位误差和距离",
    "同行人员、队员名单：全部删除",
    "邮件记录和 outbox 里的邮件文件：全部删除；公告邮件的收件邮箱、未签到提醒的邮箱和 IP 清空",
    "工具借用：队伍名、联系方式、借出和归还照片（文件一并删除）；自填的其他工具名称",
    "登录日志、密码重置日志、安全事件里的 IP、设备、浏览器、学号；操作日志中普通账号的 IP 和设备",
    "操作日志、黑白名单说明里出现过的姓名、学号、手机、邮箱：替换为「【已清除】」",
    "上传的照片、导出的 Excel、导入预览文件",
]
KEPT_SUMMARY = [
    "管理员和总管理员账号（仍可登录）",
    "实验室、预约的日期时间和状态、是否签到、人数、安全承诺时间戳（用于匿名统计）",
    "公告内容和阅读次数（只保留匿名编号）、工具清单、值班安排、系统设置",
    "本次清除之前自动生成的备份文件（backups/ 目录，不会被删除；确认不再需要后请人工销毁）",
]


# ------------------------------------------------------------------ attempt throttling

def _attempts() -> list:
    return current_app.extensions.setdefault("wipe_failed_attempts", [])


def recent_failures() -> int:
    cutoff = time.monotonic() - ATTEMPT_WINDOW_SECONDS
    kept = [t for t in _attempts() if t >= cutoff]
    _attempts()[:] = kept
    return len(kept)


def register_failure() -> None:
    _attempts().append(time.monotonic())


def locked() -> bool:
    return recent_failures() >= MAX_FAILED_ATTEMPTS


def clear_failures() -> None:
    _attempts().clear()


# ------------------------------------------------------------------ backup + files

def backup_dir() -> Path:
    path = Path(current_app.config.get("BACKUP_DIR") or Path(current_app.config["DATA_DIR"]).parent / "backups")
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_final_backup() -> Path:
    """Consistent copy of the live SQLite file (online backup API, safe with WAL)."""
    target = backup_dir() / f"app-before-wipe-{local_now().strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(current_app.config["DB_PATH"])
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    target.chmod(0o600)
    return target


def _delete_files(directory: Path) -> int:
    count = 0
    if directory.is_dir():
        for child in directory.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                count += 1
            else:
                try:
                    child.unlink()
                    count += 1
                except OSError:
                    pass
    return count


def delete_data_files() -> dict:
    data = Path(current_app.config["DATA_DIR"])
    return {
        "photos": _delete_files(Path(current_app.config["UPLOAD_DIR"])),
        "outbox": _delete_files(Path(current_app.config["OUTBOX_DIR"])),
        "exports": _delete_files(data / "exports"),
        "import_previews": _delete_files(data / "import_previews"),
    }


# ------------------------------------------------------------------ the transaction

def _personal_strings(anon_users) -> set[str]:
    values: set[str] = set()

    def add(*items):
        for item in items:
            text = (str(item).strip() if item is not None else "")
            if len(text) >= 2:
                values.add(text)

    for user in anon_users:
        add(user.student_id, user.name, user.team_name, user.last_phone, user.last_email, user.verified_email, user.contact_phone,
            user.organization, user.special_reason)
    for row in TeamMember.query.all():
        add(row.name, row.student_id, row.phone)
    for row in BookingCompanion.query.all():
        add(row.name, row.student_id, row.contact)
    for row in Booking.query.all():
        add(row.captain_phone, row.captain_email)
    for row in ToolLoan.query.all():
        add(row.team_name, row.contact)
    staff = {u.student_id for u in User.query.filter(User.role.in_(STAFF)).all()}
    staff |= {u.name for u in User.query.filter(User.role.in_(STAFF)).all()}
    return values - staff


def _mask(text, needles):
    if not text:
        return text
    for needle in needles:
        if needle in text:
            text = text.replace(needle, MASK)
    return text


def wipe_personal_data() -> dict:
    """Run the database part inside one transaction. Returns counts. Caller commits nothing else."""
    counts: dict[str, int] = {}
    members = User.query.filter(User.role.notin_(STAFF)).order_by(User.id.asc()).all()
    needles = sorted(_personal_strings(members), key=len, reverse=True)
    anon_ids = {}
    for index, user in enumerate(members, start=1):
        anon_ids[user.id] = f"已注销-{index:04d}"

    # users: captains and special accounts -> anonymous, disabled, password void
    from werkzeug.security import generate_password_hash
    import secrets

    old_student_ids = {}
    for user in members:
        old_student_ids[user.id] = user.student_id
        anon = anon_ids[user.id]
        user.student_id = anon
        user.name = anon
        user.password_hash = generate_password_hash(secrets.token_urlsafe(32))
        user.password_cipher = None
        user.must_change_password = False
        user.enabled = False
        user.team_name = user.last_phone = user.last_email = user.contact_phone = None
        user.verified_email = user.email_verified_at = user.profile_completed_at = None
        user.organization = user.special_reason = user.valid_until = None
    counts["accounts_anonymised"] = len(members)

    counts["bookings_cleared"] = Booking.query.count()
    db.session.execute(
        update(Booking).values(
            purpose=CLEARED, captain_phone=None, captain_email=None,
            checkin_ip=None, checkin_ua=None, checkin_lat=None, checkin_lng=None,
            checkin_accuracy=None, checkin_distance_m=None,
        )
    )
    db.session.execute(update(Booking).where(Booking.reject_reason.isnot(None)).values(reject_reason=CLEARED))
    counts["companions_deleted"] = BookingCompanion.query.delete(synchronize_session=False)
    counts["team_members_deleted"] = TeamMember.query.delete(synchronize_session=False)
    counts["mail_logs_deleted"] = MailLog.query.delete(synchronize_session=False)
    counts["email_verifications_deleted"] = EmailVerification.query.delete(synchronize_session=False)
    db.session.execute(update(AnnouncementMail).values(email_to=None))
    db.session.execute(update(AnnouncementAck).values(ack_ip=None))
    db.session.execute(update(NoShowAlert).values(ack_ip=None, email_to=None))
    counts["tool_loans_cleared"] = ToolLoan.query.count()
    db.session.execute(update(ToolLoan).values(team_name=None, contact=CLEARED, borrow_photo="", return_photo=None))
    db.session.execute(update(ToolLoanItem).where(ToolLoanItem.is_other.is_(True)).values(name="其他工具"))
    counts["material_claims_cleared"] = MaterialClaim.query.count()
    db.session.execute(update(MaterialClaim).values(team_name=None, photo="", ip=None, user_agent=None, note=None))
    counts["material_purchases_cleared"] = MaterialPurchase.query.count()
    db.session.execute(update(MaterialPurchase).values(
        team_name=None, photo="", ip=None, user_agent=None, note=None, email_to=None, code_hash=None,
    ))

    # logs: network identity of non-staff traffic; typed student numbers everywhere
    anon_by_old = {old: anon_ids[uid] for uid, old in old_student_ids.items()}
    logins = 0
    for row in LoginLog.query.all():
        staff_row = row.role in STAFF
        if staff_row:
            continue
        row.student_id = anon_by_old.get(row.student_id, "已注销")
        row.ip = row.user_agent = row.device_id = row.ua_fingerprint = row.browser = row.os_name = None
        row.reason = _mask(row.reason, needles)
        logins += 1
    counts["login_logs_cleared"] = logins
    counts["password_reset_logs_cleared"] = PasswordResetLog.query.count()
    db.session.execute(update(PasswordResetLog).values(student_id="已注销", ip=""))
    counts["security_events_cleared"] = SecurityEvent.query.count()
    db.session.execute(
        update(SecurityEvent).values(
            ip=None, country=None, cf_headers=None, user_agent=None, device_id=None, student_id=None,
            query_summary=None, referer=None, detail=None,
        )
    )
    audit = 0
    for row in AuditLog.query.all():
        if row.actor_role not in STAFF:
            row.actor_student_id = anon_by_old.get(row.actor_student_id, "已注销") if row.actor_student_id else None
            row.ip = row.device_id = row.user_agent = None
        row.summary = _mask(row.summary, needles)
        row.payload = _mask(row.payload, needles)
        audit += 1
    counts["audit_logs_scrubbed"] = audit
    for model in (Blacklist, SecurityWhitelist):
        for row in model.query.all():
            row.reason = _mask(row.reason, needles)
    db.session.commit()
    return counts


def vacuum() -> None:
    """Fold the WAL into the main file and rewrite it so deleted rows cannot be recovered from free pages.

    Uses its own connection so the request's SQLAlchemy session (and current_user) stay usable.
    """
    db.session.commit()  # no transaction may be open while VACUUM runs
    con = sqlite3.connect(current_app.config["DB_PATH"], timeout=30, isolation_level=None)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
