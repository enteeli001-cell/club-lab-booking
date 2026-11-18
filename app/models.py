from datetime import datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.utils import local_now
from app.quantities import ExactQuantity, amount_cents

ROLE_LABELS = {
    "superadmin": "总管理员",
    "admin": "管理员",
    "member": "参赛队长",
    "approved_member": "特批队员",
    "special_guest": "特批非参赛",
}

REGULAR_ROLES = ("member", "approved_member", "special_guest")

STAFF_ROLES = ("superadmin", "admin")

STATUS_LABELS = {
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
    "cancelled": "已取消",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    password_cipher = db.Column(db.Text, nullable=True)
    role = db.Column(db.String(16), nullable=False, default="member")
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    team_name = db.Column(db.String(80), nullable=True)
    last_phone = db.Column(db.String(32), nullable=True)
    last_email = db.Column(db.String(120), nullable=True)
    contact_phone = db.Column(db.String(32), nullable=True)
    # 首次登录资料完善：经 6 位验证码确认过的邮箱（材料购买验证码也发往这里）。
    verified_email = db.Column(db.String(120), nullable=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    profile_completed_at = db.Column(db.DateTime, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    organization = db.Column(db.String(120), nullable=True)
    special_reason = db.Column(db.String(200), nullable=True)
    valid_until = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    bookings = db.relationship(
        "Booking",
        back_populates="user",
        foreign_keys="Booking.user_id",
    )
    teammates = db.relationship(
        "TeamMember",
        back_populates="captain",
        cascade="all, delete-orphan",
        order_by="TeamMember.id",
    )
    tool_loans = db.relationship(
        "ToolLoan",
        back_populates="user",
        foreign_keys="ToolLoan.user_id",
    )

    def set_password(self, password: str, *, require_change: bool | None = None) -> None:
        self.password_hash = generate_password_hash(password)
        if self.role == "admin":
            # Ordinary admins never change their own password; only the
            # superadmin issues or resets it, so no forced change either.
            self.must_change_password = False
        elif require_change is not None:
            self.must_change_password = require_change
        elif self.role == "member" and password == self.student_id:
            self.must_change_password = True
        else:
            self.must_change_password = False
        # Recoverable Fernet ciphertext is kept only for staff, so the
        # superadmin can later view an administrator password. Never for captains.
        if self.role in STAFF_ROLES and password:
            try:
                from app.pwstore import encrypt_password
                self.password_cipher = encrypt_password(password)
            except Exception:
                self.password_cipher = None
        else:
            self.password_cipher = None

    @property
    def has_password_cipher(self) -> bool:
        return bool(self.password_cipher)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def is_captain(self) -> bool:
        return self.role == "member"

    @property
    def is_approved_member(self) -> bool:
        return self.role == "approved_member"

    @property
    def is_special_guest(self) -> bool:
        return self.role == "special_guest"

    @property
    def is_regular_user(self) -> bool:
        return self.role in REGULAR_ROLES

    @property
    def can_book(self) -> bool:
        return self.is_regular_user and self.is_account_active

    @property
    def can_borrow_tools(self) -> bool:
        return self.is_captain and self.is_account_active

    @property
    def email_verified(self) -> bool:
        return bool(self.email_verified_at and (self.verified_email or "").strip())

    @property
    def profile_complete(self) -> bool:
        """队长/特批账号：姓名、11 位手机号、已验证邮箱三项齐全。"""
        from app.utils import is_mobile_phone

        return bool((self.name or "").strip()) and is_mobile_phone(self.last_phone or "") and self.email_verified

    @property
    def is_account_active(self) -> bool:
        if not getattr(self, "enabled", True):
            return False
        if self.valid_until:
            from app.utils import today
            if today() > self.valid_until:
                return False
        return True

    def inactive_reason(self) -> str | None:
        if not getattr(self, "enabled", True):
            return "该账号已被停用。"
        if self.valid_until:
            from app.utils import today
            if today() > self.valid_until:
                return "该特批账号已超过有效期。"
        return None

    @property
    def teammates_label(self) -> str:
        if not self.teammates:
            return "—"
        return "、".join(f"{m.name}（{m.student_id}）" for m in self.teammates)

    @property
    def can_review(self) -> bool:
        return self.is_staff

    @property
    def can_view_stats(self) -> bool:
        return self.is_staff

    @property
    def can_manage_labs(self) -> bool:
        return self.is_staff

    @property
    def can_manage_accounts(self) -> bool:
        return self.is_superadmin

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


def _new_checkin_token() -> str:
    from app.checkin import new_checkin_token

    return new_checkin_token()


class Lab(db.Model):
    __tablename__ = "labs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    open_start = db.Column(db.Time, nullable=False)
    open_end = db.Column(db.Time, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    contact_name = db.Column(db.String(64), nullable=True)
    contact_phone = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    # Permanent per-lab check-in token (printed as a QR code). Never auto-rotated.
    checkin_token = db.Column(db.String(64), unique=True, nullable=True, index=True, default=_new_checkin_token)
    checkin_token_updated_at = db.Column(db.DateTime, nullable=True)
    # Location mode: when geo_required, check-in only works near these coordinates.
    geo_lat = db.Column(db.Float, nullable=True)
    geo_lng = db.Column(db.Float, nullable=True)
    geo_radius_m = db.Column(db.Integer, nullable=False, default=30, server_default="30")
    geo_required = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    bookings = db.relationship("Booking", back_populates="lab")
    tool_loans = db.relationship("ToolLoan", back_populates="lab")

    @property
    def geo_active(self) -> bool:
        """Location check is enforced only when switched on AND coordinates are set."""
        return bool(self.geo_required and self.geo_lat is not None and self.geo_lng is not None)

    @property
    def open_hours_label(self) -> str:
        return f"{self.open_start.strftime('%H:%M')}–{self.open_end.strftime('%H:%M')}"


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.Integer, db.ForeignKey("labs.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    purpose = db.Column(db.String(200), nullable=False)
    people_count = db.Column(db.Integer, nullable=False)
    captain_phone = db.Column(db.String(32), nullable=True)
    captain_email = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    reject_reason = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    checked_in_at = db.Column(db.DateTime, nullable=True)
    checkin_ip = db.Column(db.String(64), nullable=True)
    checkin_ua = db.Column(db.String(300), nullable=True)
    pledge_accepted_at = db.Column(db.DateTime, nullable=True)
    # Personal data: only shown to admins, never to other captains.
    checkin_lat = db.Column(db.Float, nullable=True)
    checkin_lng = db.Column(db.Float, nullable=True)
    checkin_accuracy = db.Column(db.Float, nullable=True)
    checkin_distance_m = db.Column(db.Float, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    reminder_attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    lab = db.relationship("Lab", back_populates="bookings")
    user = db.relationship("User", back_populates="bookings", foreign_keys=[user_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])
    companions = db.relationship(
        "BookingCompanion",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingCompanion.id",
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def time_range_label(self) -> str:
        return f"{self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')}"

    @property
    def duration_minutes(self) -> int:
        start = datetime.combine(self.date, self.start_time)
        end = datetime.combine(self.date, self.end_time)
        return int((end - start).total_seconds() // 60)

    @property
    def duration_label(self) -> str:
        minutes = self.duration_minutes
        hours, mins = divmod(minutes, 60)
        if hours and mins:
            return f"{hours}小时{mins}分钟"
        if hours:
            return f"{hours}小时"
        return f"{mins}分钟"

    def has_started(self) -> bool:
        start = datetime.combine(self.date, self.start_time)
        return start <= local_now().replace(tzinfo=None)

    @property
    def start_datetime(self):
        return datetime.combine(self.date, self.start_time)

    def minutes_until_start(self) -> float:
        return (self.start_datetime - local_now().replace(tzinfo=None)).total_seconds() / 60

    def can_self_change(self) -> bool:
        from app.utils import SELF_CHANGE_LEAD_MINUTES

        if self.status not in ("pending", "approved"):
            return False
        return self.minutes_until_start() > SELF_CHANGE_LEAD_MINUTES

    @property
    def checkin(self):
        """Check-in info dict (state/label/badge) for approved bookings, else None."""
        from app.checkin import checkin_info

        return checkin_info(self)

    @property
    def companions_label(self) -> str:
        if not self.companions:
            return "—"
        parts = []
        for item in self.companions:
            piece = f"{item.name}（{item.student_id}）"
            if item.contact:
                piece += f" {item.contact}"
            if item.source == "other":
                piece += "（其他）"
            parts.append(piece)
        return "、".join(parts)



class TeamMember(db.Model):
    __tablename__ = "team_members"

    id = db.Column(db.Integer, primary_key=True)
    captain_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    student_id = db.Column(db.String(32), nullable=False)
    phone = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    captain = db.relationship("User", back_populates="teammates")


class PasswordResetLog(db.Model):
    __tablename__ = "password_reset_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    ip = db.Column(db.String(64), nullable=False)
    student_id = db.Column(db.String(32), nullable=False, index=True)
    success = db.Column(db.Boolean, nullable=False, default=False)


class BookingCompanion(db.Model):
    __tablename__ = "booking_companions"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False, index=True)
    source = db.Column(db.String(16), nullable=False, default="member")
    name = db.Column(db.String(64), nullable=False)
    student_id = db.Column(db.String(32), nullable=False)
    contact = db.Column(db.String(80), nullable=True)
    team_member_id = db.Column(db.Integer, db.ForeignKey("team_members.id"), nullable=True)

    booking = db.relationship("Booking", back_populates="companions")


LOAN_STATUS_LABELS = {
    "borrowing": "借用中",
    "returned": "已归还",
    "overdue": "逾期",
}


class ToolLoan(db.Model):
    __tablename__ = "tool_loans"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lab_id = db.Column(db.Integer, db.ForeignKey("labs.id"), nullable=False, index=True)
    team_name = db.Column(db.String(80), nullable=True)
    contact = db.Column(db.String(32), nullable=False)
    borrow_at = db.Column(db.DateTime, nullable=False)
    expected_return_at = db.Column(db.DateTime, nullable=False)
    actual_return_at = db.Column(db.DateTime, nullable=True)
    borrow_photo = db.Column(db.String(80), nullable=False)
    return_photo = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="borrowing", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    user = db.relationship("User", back_populates="tool_loans", foreign_keys=[user_id])
    lab = db.relationship("Lab", back_populates="tool_loans")
    items = db.relationship(
        "ToolLoanItem",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="ToolLoanItem.id",
    )

    @property
    def is_overdue(self) -> bool:
        if self.status != "borrowing":
            return False
        return self.expected_return_at < local_now()

    @property
    def display_status(self) -> str:
        return "overdue" if self.is_overdue else self.status

    @property
    def display_status_label(self) -> str:
        return LOAN_STATUS_LABELS.get(self.display_status, self.display_status)

    @property
    def items_label(self) -> str:
        if not self.items:
            return "—"
        return "、".join(f"{item.name} × {item.quantity}" for item in self.items)


class ToolLoanItem(db.Model):
    __tablename__ = "tool_loan_items"

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("tool_loans.id"), nullable=False, index=True)
    catalog_id = db.Column(db.Integer, db.ForeignKey("tool_catalog.id"), nullable=True, index=True)
    name = db.Column(db.String(80), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    is_other = db.Column(db.Boolean, nullable=False, default=False)

    loan = db.relationship("ToolLoan", back_populates="items")
    catalog = db.relationship("ToolCatalog")


WEEKDAY_LABELS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


class DutyShift(db.Model):
    __tablename__ = "duty_shifts"

    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.Integer, db.ForeignKey("labs.id"), nullable=False, index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    specific_date = db.Column(db.Date, nullable=True, index=True)
    weekday = db.Column(db.Integer, nullable=True)  # 0=星期一 ... 6=星期日
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    lab = db.relationship("Lab", backref="duty_shifts")
    admin = db.relationship("User", backref="duty_shifts")

    @property
    def when_label(self) -> str:
        clock = f"{self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')}"
        if self.specific_date:
            return f"{self.specific_date.isoformat()} {clock}"
        if self.weekday is not None and 0 <= self.weekday <= 6:
            return f"每周{WEEKDAY_LABELS[self.weekday]} {clock}"
        return clock


class ToolCatalog(db.Model):
    __tablename__ = "tool_catalog"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    total_quantity = db.Column(db.Integer, nullable=False, default=0)
    is_major = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)


MAIL_STATUS_LABELS = {
    "sent": "已发送",
    "local": "本地存档",
    "failed": "失败",
}


class MailLog(db.Model):
    __tablename__ = "mail_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    recipient = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="local", index=True)
    error = db.Column(db.String(300), nullable=True)
    filename = db.Column(db.String(120), nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True, index=True)

    booking = db.relationship("Booking")

    @property
    def status_label(self) -> str:
        return MAIL_STATUS_LABELS.get(self.status, self.status)



class LoginLog(db.Model):
    __tablename__ = "login_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    student_id = db.Column(db.String(32), nullable=True, index=True)
    role = db.Column(db.String(16), nullable=True)
    ip = db.Column(db.String(64), nullable=True, index=True)
    user_agent = db.Column(db.String(300), nullable=True)
    device_id = db.Column(db.String(64), nullable=True, index=True)
    ua_fingerprint = db.Column(db.String(64), nullable=True)
    browser = db.Column(db.String(40), nullable=True)
    os_name = db.Column(db.String(40), nullable=True)
    success = db.Column(db.Boolean, nullable=False, default=False)
    reason = db.Column(db.String(120), nullable=True)

    user = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    actor_student_id = db.Column(db.String(32), nullable=True, index=True)
    actor_role = db.Column(db.String(16), nullable=True, index=True)
    ip = db.Column(db.String(64), nullable=True, index=True)
    device_id = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    target_type = db.Column(db.String(32), nullable=True)
    target_id = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(400), nullable=False)
    payload = db.Column(db.Text, nullable=True)
    reversible = db.Column(db.Boolean, nullable=False, default=False)
    undo_blocked_reason = db.Column(db.String(200), nullable=True)
    undone_at = db.Column(db.DateTime, nullable=True)
    undone_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    undo_of_id = db.Column(db.Integer, db.ForeignKey("audit_logs.id"), nullable=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
    undone_by = db.relationship("User", foreign_keys=[undone_by_id])


class Blacklist(db.Model):
    __tablename__ = "blacklist_entries"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    kind = db.Column(db.String(16), nullable=False, index=True)  # ip | device
    value = db.Column(db.String(128), nullable=False, index=True)
    reason = db.Column(db.String(200), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    lifted_at = db.Column(db.DateTime, nullable=True)
    lifted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lifted_by = db.relationship("User", foreign_keys=[lifted_by_id])

    def is_in_effect(self, now=None) -> bool:
        if not self.active:
            return False
        if self.expires_at is None:
            return True
        now = now or local_now()
        return now < self.expires_at


class SecurityEvent(db.Model):
    __tablename__ = "security_events"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)
    severity = db.Column(db.String(16), nullable=False, default="medium", index=True)
    ip = db.Column(db.String(64), nullable=True, index=True)
    country = db.Column(db.String(8), nullable=True)
    cf_headers = db.Column(db.String(400), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    device_id = db.Column(db.String(64), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    student_id = db.Column(db.String(32), nullable=True)
    method = db.Column(db.String(12), nullable=True)
    path = db.Column(db.String(300), nullable=True, index=True)
    query_summary = db.Column(db.String(400), nullable=True)
    referer = db.Column(db.String(300), nullable=True)
    detail = db.Column(db.String(400), nullable=True)
    count = db.Column(db.Integer, nullable=False, default=1)
    auto_blocked = db.Column(db.Boolean, nullable=False, default=False)
    read_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])


class SecurityWhitelist(db.Model):
    __tablename__ = "security_whitelist"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    kind = db.Column(db.String(16), nullable=False, index=True)  # ip | device
    value = db.Column(db.String(128), nullable=False, index=True)
    reason = db.Column(db.String(200), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(200), nullable=False)


class NoShowAlert(db.Model):
    """One forced no-show reminder per (captain, cumulative-count level)."""

    __tablename__ = "noshow_alerts"
    __table_args__ = (db.UniqueConstraint("user_id", "level", name="uq_noshow_user_level"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    level = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    acknowledged_at = db.Column(db.DateTime, nullable=True)
    ack_ip = db.Column(db.String(64), nullable=True)
    email_to = db.Column(db.String(120), nullable=True)
    email_status = db.Column(db.String(16), nullable=True)  # sent | local | failed | no_email

    user = db.relationship("User", foreign_keys=[user_id])


ANNOUNCEMENT_LEVELS = {1: "一级", 2: "二级", 3: "三级"}
ANNOUNCEMENT_LEVEL_HINTS = {
    1: "只在公告页显示，不提醒",
    2: "登录后强制弹出，需点击“我已知晓”",
    3: "最高级：强制弹出并置顶，同时发邮件给队长和特批账号",
}


class Announcement(db.Model):
    """Notice board entry. level 1 = list only, 2 = forced popup, 3 = popup + pinned + email."""

    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    level = db.Column(db.Integer, nullable=False, default=1)
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    # Bumped when the editor asks everybody to confirm again.
    ack_revision = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    # >0 once an email round is requested (level 3); bumped by "重新发送邮件".
    mail_revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Set to mail_revision once every recipient of that round has been handled (or retries are used up).
    mail_done_revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    # Free-text "发布人" shown to readers. The real account is always created_by_id (permissions,
    # audit and the author's own-popup exclusion never look at this name).
    publisher_name = db.Column(db.String(30), nullable=True)

    creator = db.relationship("User", foreign_keys=[created_by_id])
    updater = db.relationship("User", foreign_keys=[updated_by_id])
    reads = db.relationship("AnnouncementRead", back_populates="announcement", cascade="all, delete-orphan")
    acks = db.relationship("AnnouncementAck", back_populates="announcement", cascade="all, delete-orphan")
    mails = db.relationship("AnnouncementMail", back_populates="announcement", cascade="all, delete-orphan")

    @property
    def publisher_display(self) -> str:
        return (self.publisher_name or "").strip() or (self.creator.name if self.creator else "管理员")

    @property
    def level_label(self) -> str:
        return ANNOUNCEMENT_LEVELS.get(self.level, str(self.level))

    @property
    def is_edited(self) -> bool:
        return self.updated_at is not None


class AnnouncementRead(db.Model):
    """First time a user opened an announcement (one row per user and announcement)."""

    __tablename__ = "announcement_reads"
    __table_args__ = (db.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_read"),)

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    read_at = db.Column(db.DateTime, nullable=False, default=local_now)

    announcement = db.relationship("Announcement", back_populates="reads")
    user = db.relationship("User", foreign_keys=[user_id])


class AnnouncementAck(db.Model):
    """“我已知晓” for a popup announcement; `revision` is the ack_revision that was confirmed."""

    __tablename__ = "announcement_acks"
    __table_args__ = (db.UniqueConstraint("announcement_id", "user_id", name="uq_announcement_ack"),)

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    acked_at = db.Column(db.DateTime, nullable=False, default=local_now)
    ack_ip = db.Column(db.String(64), nullable=True)

    announcement = db.relationship("Announcement", back_populates="acks")
    user = db.relationship("User", foreign_keys=[user_id])


class AnnouncementMail(db.Model):
    """One email per (announcement, recipient, mail round); unique key makes the scan idempotent."""

    __tablename__ = "announcement_mails"
    __table_args__ = (
        db.UniqueConstraint("announcement_id", "user_id", "mail_revision", name="uq_announcement_mail"),
    )

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    mail_revision = db.Column(db.Integer, nullable=False, default=1)
    email_to = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending")  # pending | sent | local | failed | no_email
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    updated_at = db.Column(db.DateTime, nullable=False, default=local_now)

    announcement = db.relationship("Announcement", back_populates="mails")
    user = db.relationship("User", foreign_keys=[user_id])


# ---------------------------------------------------------------- materials (领取与购买)

MATERIAL_KIND_LABELS = {"free": "免费领取", "sale": "购买"}

MATERIAL_RECORD_STATUS_LABELS = {
    "active": "有效",
    "voided": "已作废",
}

PURCHASE_STATUS_LABELS = {
    "pending": "待验证",
    "confirmed": "已确认",
    "cancelled": "已取消",
    "voided": "已作废",
}


def format_cents(cents) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


class MaterialItem(db.Model):
    """A material kind. kind=free: per-team allotment; kind=sale: sold at unit_price_cents."""

    __tablename__ = "material_items"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(8), nullable=False, index=True)  # free | sale
    name = db.Column(db.String(80), nullable=False)
    spec = db.Column(db.String(80), nullable=True)  # 型号 / 规格
    unit = db.Column(db.String(16), nullable=False, default="个")
    _per_team_qty_legacy = db.Column("per_team_qty", db.Integer, nullable=False, default=0)
    per_team_qty_exact = db.Column(db.Text, nullable=True)
    per_team_qty = ExactQuantity("per_team_qty")
    unit_price_cents = db.Column(db.Integer, nullable=False, default=0)  # sale only
    _stock_legacy = db.Column("stock", db.Integer, nullable=False, default=0)
    stock_exact = db.Column(db.Text, nullable=True)
    stock = ExactQuantity("stock")
    _max_per_purchase_legacy = db.Column("max_per_purchase", db.Integer, nullable=True)
    max_per_purchase_exact = db.Column(db.Text, nullable=True)
    max_per_purchase = ExactQuantity("max_per_purchase", nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    updated_at = db.Column(db.DateTime, nullable=True)

    @property
    def label(self) -> str:
        return f"{self.name}（{self.spec}）" if self.spec else self.name

    @property
    def unit_price_label(self) -> str:
        return format_cents(self.unit_price_cents)

    @property
    def purchase_limit(self):
        """Most a captain may pick in one purchase right now."""
        limit = max(0, self.stock)
        if self.max_per_purchase:
            limit = min(limit, self.max_per_purchase)
        return limit


class MaterialClaim(db.Model):
    """A team's (captain's) one-off claim of the free allotment."""

    __tablename__ = "material_claims"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    team_name = db.Column(db.String(80), nullable=True)
    photo = db.Column(db.String(80), nullable=False, default="")
    status = db.Column(db.String(12), nullable=False, default="active", index=True)  # active | voided
    note = db.Column(db.String(200), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    items = db.relationship(
        "MaterialClaimItem", back_populates="claim", cascade="all, delete-orphan", order_by="MaterialClaimItem.id"
    )

    @property
    def status_label(self) -> str:
        return MATERIAL_RECORD_STATUS_LABELS.get(self.status, self.status)

    @property
    def items_label(self) -> str:
        if not self.items:
            return "—"
        return "、".join(f"{line.label} × {line.quantity}{line.unit}" for line in self.items)


class MaterialClaimItem(db.Model):
    __tablename__ = "material_claim_items"

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("material_claims.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("material_items.id"), nullable=True, index=True)
    name = db.Column(db.String(80), nullable=False)
    spec = db.Column(db.String(80), nullable=True)
    unit = db.Column(db.String(16), nullable=False, default="个")
    _quantity_legacy = db.Column("quantity", db.Integer, nullable=False)
    quantity_exact = db.Column(db.Text, nullable=True)
    quantity = ExactQuantity("quantity")

    claim = db.relationship("MaterialClaim", back_populates="items")
    item = db.relationship("MaterialItem")

    @property
    def label(self) -> str:
        return f"{self.name}（{self.spec}）" if self.spec else self.name


class MaterialPurchase(db.Model):
    """A purchase; stock is deducted only once the e-mailed 6-digit code is confirmed."""

    __tablename__ = "material_purchases"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    team_name = db.Column(db.String(80), nullable=True)
    photo = db.Column(db.String(80), nullable=False, default="")
    status = db.Column(db.String(12), nullable=False, default="pending", index=True)
    total_cents = db.Column(db.Integer, nullable=False, default=0)
    settled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    settled_at = db.Column(db.DateTime, nullable=True)
    settled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    email_to = db.Column(db.String(120), nullable=True)
    code_hash = db.Column(db.String(128), nullable=True)
    code_expires_at = db.Column(db.DateTime, nullable=True)
    code_sent_at = db.Column(db.DateTime, nullable=True)
    code_send_count = db.Column(db.Integer, nullable=False, default=0)
    code_attempts = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.String(200), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now, index=True)
    confirmed_at = db.Column(db.DateTime, nullable=True, index=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    settled_by = db.relationship("User", foreign_keys=[settled_by_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    items = db.relationship(
        "MaterialPurchaseItem", back_populates="purchase", cascade="all, delete-orphan", order_by="MaterialPurchaseItem.id"
    )

    @property
    def status_label(self) -> str:
        return PURCHASE_STATUS_LABELS.get(self.status, self.status)

    @property
    def total_label(self) -> str:
        return format_cents(self.total_cents)

    @property
    def items_label(self) -> str:
        if not self.items:
            return "—"
        return "、".join(f"{line.label} × {line.quantity}{line.unit}" for line in self.items)

    def recompute_total(self) -> int:
        self.total_cents = sum(line.subtotal_cents for line in self.items)
        return self.total_cents


class MaterialPurchaseItem(db.Model):
    __tablename__ = "material_purchase_items"

    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("material_purchases.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("material_items.id"), nullable=True, index=True)
    name = db.Column(db.String(80), nullable=False)
    spec = db.Column(db.String(80), nullable=True)
    unit = db.Column(db.String(16), nullable=False, default="个")
    unit_price_cents = db.Column(db.Integer, nullable=False)
    _quantity_legacy = db.Column("quantity", db.Integer, nullable=False)
    quantity_exact = db.Column(db.Text, nullable=True)
    quantity = ExactQuantity("quantity")

    purchase = db.relationship("MaterialPurchase", back_populates="items")
    item = db.relationship("MaterialItem")

    @property
    def label(self) -> str:
        return f"{self.name}（{self.spec}）" if self.spec else self.name

    @property
    def subtotal_cents(self) -> int:
        return amount_cents(self.unit_price_cents, self.quantity)

    @property
    def unit_price_label(self) -> str:
        return format_cents(self.unit_price_cents)

    @property
    def subtotal_label(self) -> str:
        return format_cents(self.subtotal_cents)


class EmailVerification(db.Model):
    """A pending e-mail binding for a captain / approved account (code digest only)."""

    __tablename__ = "email_verifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False)
    code_hash = db.Column(db.String(128), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    send_count = db.Column(db.Integer, nullable=False, default=0)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(12), nullable=False, default="pending", index=True)  # pending | verified | cancelled
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)
    verified_at = db.Column(db.DateTime, nullable=True)
    ip = db.Column(db.String(64), nullable=True)

    user = db.relationship("User")


class SimulationMaterial(db.Model):
    __tablename__ = "simulation_materials"
    id = db.Column(db.Integer, primary_key=True)
    seed_key = db.Column(db.String(64), unique=True, nullable=True)
    name = db.Column(db.String(80), nullable=False)
    elastic_modulus = db.Column(db.Float, nullable=False)
    shear_modulus = db.Column(db.Float, nullable=False)
    density = db.Column(db.Float, nullable=False)
    allowable_stress = db.Column(db.Float, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class SimulationSection(db.Model):
    __tablename__ = "simulation_sections"
    id = db.Column(db.Integer, primary_key=True)
    seed_key = db.Column(db.String(64), unique=True, nullable=True)
    name = db.Column(db.String(80), nullable=False)
    area = db.Column(db.Float, nullable=False)
    iy = db.Column(db.Float, nullable=False)
    iz = db.Column(db.Float, nullable=False)
    wy = db.Column(db.Float, nullable=False)
    wz = db.Column(db.Float, nullable=False)
    torsion_j = db.Column(db.Float, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class SimulationModel(db.Model):
    __tablename__ = "simulation_models"
    id = db.Column(db.Integer, primary_key=True)
    seed_key = db.Column(db.String(64), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), nullable=False, default="")
    data_json = db.Column(db.Text, nullable=False)
    result_json = db.Column(db.Text, nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, nullable=False, default=local_now)
