from __future__ import annotations

import csv
import re
import io
import os
import secrets
from functools import lru_cache
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

# Platform clock. Everything the app treats as "now" or "today" is the wall clock in APP_TIMEZONE
# (default Beijing time), independent of the host's own timezone / TZ variable / system clock zone.
DEFAULT_TIMEZONE = "Asia/Shanghai"


@lru_cache(maxsize=8)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def timezone_name() -> str:
    """APP_TIMEZONE from the Flask config (or the environment outside a request), default Asia/Shanghai."""
    name = None
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            name = current_app.config.get("APP_TIMEZONE")
    except Exception:
        name = None
    return name or os.environ.get("APP_TIMEZONE") or DEFAULT_TIMEZONE


def app_timezone() -> ZoneInfo:
    return _zone(timezone_name())


def timezone_label() -> str:
    name = timezone_name()
    return "北京时间" if name == DEFAULT_TIMEZONE else name


def aware_now() -> datetime:
    return datetime.now(app_timezone())

ROLE_ALIASES = {
    "总管理员": "superadmin",
    "超级管理员": "superadmin",
    "管理员": "admin",
    "审核人员": "admin",
    "干部": "admin",
    "参赛队长": "member",
    "普通成员": "member",
    "成员": "member",
    "队长": "member",
    "superadmin": "superadmin",
    "admin": "admin",
    "officer": "admin",
    "member": "member",
    "captain": "member",
}

ROLE_LABELS = {
    "superadmin": "总管理员",
    "admin": "管理员",
    "member": "参赛队长",
    "approved_member": "特批队员",
    "special_guest": "特批非参赛",
}


def safe_local_url(value: str | None) -> bool:
    """Only local absolute paths; browsers normalize backslashes as slashes."""
    return bool(value and value.startswith("/") and not value.startswith("//")
                and "\\" not in value and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value))


def local_now() -> datetime:
    return aware_now().replace(tzinfo=None)


def today() -> date:
    return local_now().date()


def parse_time(value) -> time | None:
    if value is None:
        return None
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time().replace(second=0, microsecond=0)
        except ValueError:
            continue
    return None


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def is_half_hour(t: time) -> bool:
    return t.minute in (0, 30) and t.second == 0 and t.microsecond == 0


def time_slots(start: time, end: time, include_end: bool = False) -> list[time]:
    slots = []
    anchor = date(2000, 1, 1)  # only the clock times matter here
    current = datetime.combine(anchor, start)
    last = datetime.combine(anchor, end)
    while current < last or (include_end and current <= last):
        slots.append(current.time())
        current += timedelta(minutes=30)
    return slots


def all_day_slots() -> list[time]:
    return time_slots(time(0, 0), time(23, 30), include_end=True)



MAX_BOOKING_MINUTES = 60
OVERLAP_ALLOW_MINUTES = 30
SELF_CHANGE_LEAD_MINUTES = 30
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd9N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def overlap_minutes(start1: time, end1: time, start2: time, end2: time) -> int:
    """Return overlap length in whole minutes. Adjacent intervals yield 0."""
    dummy = date(2000, 1, 1)
    latest_start = max(datetime.combine(dummy, start1), datetime.combine(dummy, start2))
    earliest_end = min(datetime.combine(dummy, end1), datetime.combine(dummy, end2))
    delta = (earliest_end - latest_start).total_seconds()
    if delta <= 0:
        return 0
    return int(delta // 60)


def is_significant_overlap(start1: time, end1: time, start2: time, end2: time) -> bool:
    """True when overlap exceeds the 30-minute allowance."""
    return overlap_minutes(start1, end1, start2, end2) > OVERLAP_ALLOW_MINUTES


def booking_duration_minutes(booking_date: date, start: time, end: time) -> int:
    return int((datetime.combine(booking_date, end) - datetime.combine(booking_date, start)).total_seconds() // 60)


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_mobile_phone(value: str) -> bool:
    return bool(re.fullmatch(r"1[3-9][0-9]{9}", (value or "").strip()))


def is_email_address(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (value or "").strip()))


def is_phone_or_email(value: str) -> bool:
    return is_mobile_phone(value) or is_email_address(value)


def collect_booking_companions(form, captain) -> tuple[list[dict], list[str]]:
    """Parse teammate checkboxes and optional "其他" rows from a booking form."""
    errors: list[str] = []
    companions: list[dict] = []
    seen_members: set[int] = set()
    for raw in form.getlist("companion_member"):
        text = str(raw).strip()
        if not text.isdigit():
            errors.append("所选同行队员无效。")
            continue
        mid = int(text)
        teammate = next((m for m in captain.teammates if m.id == mid), None)
        if teammate is None:
            errors.append("所选同行队员无效。")
            continue
        if mid in seen_members:
            continue
        seen_members.add(mid)
        companions.append(
            {
                "source": "member",
                "name": teammate.name,
                "student_id": teammate.student_id,
                "contact": None,
                "team_member_id": teammate.id,
            }
        )

    include_other = (form.get("include_other") or "").strip().lower() in {"on", "1", "true", "yes"}
    names = form.getlist("other_name") if include_other else []
    sids = form.getlist("other_student_id") if include_other else []
    contacts = form.getlist("other_contact") if include_other else []
    filled = 0
    for i in range(max(len(names), len(sids), len(contacts), 0)):
        name = (names[i] if i < len(names) else "").strip()
        sid = (sids[i] if i < len(sids) else "").strip()
        contact = (contacts[i] if i < len(contacts) else "").strip()
        if not name and not sid and not contact:
            continue
        filled += 1
        if not name or not sid or not contact:
            absent = "、".join(label for label, value in (("姓名", name), ("学号", sid), ("联系方式", contact)) if not value)
            errors.append(f"选择“其他”时，其他人员的姓名、学号和联系方式须全部填写。请填写 {absent}。")
            continue
        if not is_phone_or_email(contact):
            errors.append(f"其他人员“{name}”的联系方式须为手机号或邮箱。")
            continue
        companions.append(
            {
                "source": "other",
                "name": name,
                "student_id": sid,
                "contact": contact,
                "team_member_id": None,
            }
        )
    if include_other and filled == 0:
        errors.append("选择“其他”时，请至少填写一名其他人员的姓名、学号和联系方式。")
    return companions, errors


def apply_booking_companions(booking, companions: list[dict]) -> None:
    from app import db
    from app.models import BookingCompanion

    BookingCompanion.query.filter_by(booking_id=booking.id).delete()
    for item in companions:
        db.session.add(
            BookingCompanion(
                booking=booking,
                source=item["source"],
                name=item["name"],
                student_id=item["student_id"],
                contact=item.get("contact"),
                team_member_id=item.get("team_member_id"),
            )
        )


def sniff_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_uploaded_photo(file_storage, upload_dir) -> tuple[str | None, list[str]]:
    """Store a jpg/png/webp photo under upload_dir with a random name."""
    if file_storage is None or not getattr(file_storage, "filename", None):
        return None, ["请上传照片。"]
    ext = Path(file_storage.filename).suffix.lower()
    data = file_storage.read()
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass
    if not data:
        return None, ["请上传照片。"]
    if len(data) > PHOTO_MAX_BYTES:
        return None, ["照片不能超过 5MB。"]
    sniffed = sniff_image_ext(data)
    if sniffed is None or ext not in PHOTO_EXTENSIONS:
        return None, ["照片仅支持 jpg、png、webp 格式。"]
    if sniffed == ".jpg" and ext not in {".jpg", ".jpeg"}:
        return None, ["照片仅支持 jpg、png、webp 格式。"]
    if sniffed != ".jpg" and ext != sniffed:
        return None, ["照片仅支持 jpg、png、webp 格式。"]
    stored = secrets.token_hex(16) + sniffed
    dest = Path(upload_dir) / stored
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return stored, []


def write_minimal_png(path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MINIMAL_PNG_BYTES)
    return path


def collect_loan_items(form) -> tuple[list[dict], list[str]]:
    from app import db
    from app.models import ToolCatalog

    errors: list[str] = []
    items: list[dict] = []
    catalog_ids = form.getlist("tool_catalog_id")
    names = form.getlist("tool_name")
    qtys = form.getlist("tool_quantity")
    n = max(len(catalog_ids), len(names), len(qtys), 0)
    for i in range(n):
        raw_cat = (catalog_ids[i] if i < len(catalog_ids) else "").strip()
        name = (names[i] if i < len(names) else "").strip()
        raw_qty = (qtys[i] if i < len(qtys) else "").strip()
        if not raw_cat and not name and not raw_qty:
            continue
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            label = name or raw_cat or "工具"
            errors.append(f"“{label}”的数量须为正整数。")
            continue
        if qty < 1:
            label = name or raw_cat or "工具"
            errors.append(f"“{label}”的数量须为正整数。")
            continue
        is_other = raw_cat in {"", "other", "其他"}
        catalog_id = None
        if not is_other:
            if not raw_cat.isdigit():
                errors.append("请选择工具，或选择「其他」后填写名称。")
                continue
            cat = db.session.get(ToolCatalog, int(raw_cat))
            if cat is None:
                errors.append("所选工具不存在。")
                continue
            catalog_id = cat.id
            name = cat.name
        else:
            # 兼容仅提交工具名称的旧表单：名称能匹配清单则计入库存。
            if not catalog_ids and name:
                cat = ToolCatalog.query.filter_by(name=name).first()
                if cat is not None:
                    catalog_id = cat.id
                    is_other = False
            if is_other and not name:
                errors.append(
                    "选择「其他」时请填写工具名称。" if raw_cat in {"other", "其他"} else "请选择工具，或选择「其他」后填写名称。"
                )
                continue
        items.append({"name": name, "quantity": qty, "catalog_id": catalog_id, "is_other": is_other})
    if not items and not errors:
        errors.append("请至少选择或填写一件借用工具及数量。")
    return items, errors

def intervals_overlap(start1: time, end1: time, start2: time, end2: time) -> bool:
    """Half-open intervals [start, end). Adjacent endpoints do not overlap."""
    return start1 < end2 and start2 < end1



def find_duty_shifts(lab_id: int, booking_date: date, start: time, end: time, *, candidates=None):
    """Duty shifts for a lab whose slot overlaps the booking interval.

    ``candidates`` may carry this lab's preloaded shifts (batched list pages).
    """
    from app.models import DutyShift

    weekday = booking_date.weekday()
    if candidates is None:
        candidates = DutyShift.query.filter_by(lab_id=lab_id).all()
    matched = []
    for shift in candidates:
        day_ok = False
        if shift.specific_date is not None and shift.specific_date == booking_date:
            day_ok = True
        elif shift.specific_date is None and shift.weekday is not None and shift.weekday == weekday:
            day_ok = True
        if not day_ok:
            continue
        if intervals_overlap(start, end, shift.start_time, shift.end_time):
            matched.append(shift)
    return matched


def find_booking_contacts(booking, *, shifts=None) -> list[dict]:
    """Lookup order: overlapping duty admin, then reviewer, then lab default contact."""
    from app.i18n import t

    contacts = []
    shifts = find_duty_shifts(booking.lab_id, booking.date, booking.start_time, booking.end_time, candidates=shifts)
    for shift in shifts:
        admin = shift.admin
        contacts.append(
            {
                "name": admin.name if admin else t("值班管理员"),
                "phone": (admin.contact_phone if admin else None) or "",
                "source": "duty",
                "source_label": "值班管理员",
            }
        )
    if contacts:
        return contacts
    reviewer = getattr(booking, "reviewer", None)
    if reviewer is not None:
        return [
            {
                "name": reviewer.name,
                "phone": reviewer.contact_phone or "",
                "source": "reviewer",
                "source_label": "审核管理员",
            }
        ]
    lab = booking.lab
    if lab is not None and (lab.contact_name or lab.contact_phone):
        return [
            {
                "name": lab.contact_name or t("实验室联系人"),
                "phone": lab.contact_phone or "",
                "source": "lab",
                "source_label": "实验室默认联系人",
            }
        ]
    return []


def format_contacts_label(contacts: list[dict]) -> str:
    from app.i18n import t, current_lang
    if not contacts:
        return t("暂未配置可联系的管理员，请向总管理员咨询。")
    parts = []
    for item in contacts:
        phone = item.get("phone") or t("（未填写电话）")
        parts.append(f"{item['name']}（{t(item['source_label'])}）{phone}")
    return ("; " if current_lang() not in {"zh-Hans", "zh-Hant"} else "；").join(parts)

def find_conflicts(lab_id: int, booking_date: date, start: time, end: time, exclude_id: int | None = None):
    from app.models import Booking

    query = Booking.query.filter(
        Booking.lab_id == lab_id,
        Booking.date == booking_date,
        Booking.status.in_(("pending", "approved")),
    )
    if exclude_id is not None:
        query = query.filter(Booking.id != exclude_id)

    approved = []
    pending = []
    for booking in query.all():
        if not is_significant_overlap(start, end, booking.start_time, booking.end_time):
            continue
        if booking.status == "approved":
            approved.append(booking)
        else:
            pending.append(booking)
    return approved, pending


def validate_booking(
    lab,
    booking_date: date,
    start: time,
    end: time,
    people_count: int,
    *,
    exclude_id: int | None = None,
    check_past: bool = True,
) -> list[str]:
    errors = []
    if lab is None:
        errors.append("实验室不存在。")
        return errors
    if not lab.enabled:
        errors.append("该实验室已停用，无法预约。")
    if booking_date is None or start is None or end is None:
        errors.append("请填写完整的日期和起止时间。")
        return errors
    if start >= end:
        errors.append("结束时间必须晚于开始时间。")
    else:
        duration = booking_duration_minutes(booking_date, start, end)
        if duration > MAX_BOOKING_MINUTES:
            errors.append("单次预约时长不得超过 1 小时。")
    if start < lab.open_start or end > lab.open_end:
        errors.append(
            f"预约时间须在实验室开放时间内（{lab.open_start.strftime('%H:%M')}–{lab.open_end.strftime('%H:%M')}）。"
        )
    if check_past:
        start_dt = datetime.combine(booking_date, start)
        if start_dt < local_now():
            errors.append("不能预约已经过去的时间。")
    if people_count is None or people_count < 1:
        errors.append("使用人数至少为 1。")
    elif people_count > lab.capacity:
        errors.append(f"使用人数不能超过实验室容量（{lab.capacity} 人）。")

    approved, _pending = find_conflicts(lab.id, booking_date, start, end, exclude_id=exclude_id)
    if approved:
        items = "、".join(
            f"{b.time_range_label}（重叠 {overlap_minutes(start, end, b.start_time, b.end_time)} 分钟）"
            for b in approved
        )
        errors.append(f"与已通过的预约时间冲突：{items}。重叠不超过 30 分钟不算冲突。")
    return errors


def generate_password() -> str:
    # Avoid ambiguous characters; still a strong enough initial password.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def mask_person_name(name: str) -> str:
    """部分脱敏：至少两字时保留首尾、中间以 * 代替（张三 → 张*三；王小明 → 王*明）。单字则加 *。"""
    text = (name or "").strip()
    if not text:
        return ""
    if len(text) == 1:
        return text + "*"
    return text[0] + "*" + text[-1]


def resolve_account_password(role: str, student_id: str, provided: str | None) -> tuple[str, bool, list[str]]:
    """Return (password, is_default, errors). Captain default password is the student ID."""
    provided = (provided or "").strip()
    if provided:
        if len(provided) < 8 and not (role == "member" and provided == student_id):
            return "", False, ["自定义初始密码至少需要 8 位；参赛队长也可留空，默认等于学号。"]
        return provided, False, []
    if role == "member":
        if not student_id:
            return "", True, ["缺少学号，无法生成默认密码。"]
        return student_id, True, []
    return generate_password(), True, []






PASSWORD_RULES = [
    ("min_length", "长度不少于 6 位"),
    ("has_upper", "包含大写字母"),
    ("has_lower", "包含小写字母"),
    ("has_special", "包含至少一个特殊符号（非字母数字字符）"),
    ("not_student_id", "不能与学号相同"),
    ("not_old_password", "不能与当前密码相同"),
    ("confirmed", "两次输入的新密码一致"),
]

PASSWORD_RULE_ERRORS = {
    "min_length": "新密码长度不能少于 6 位。",
    "has_upper": "新密码必须包含大写字母。",
    "has_lower": "新密码必须包含小写字母。",
    "has_special": "新密码必须包含至少一个特殊符号。",
    "not_student_id": "新密码不能与学号相同。",
    "not_old_password": "新密码不能与旧密码相同。",
    "confirmed": "两次输入的新密码不一致。",
}


def evaluate_password_rules(new_password: str, *, student_id: str | None = None, current_plain: str | None = None, confirm: str | None = None) -> dict[str, bool]:
    """Front-end-aligned rule checks. current_plain is optional (typed current password)."""
    new_password = new_password or ""
    return {
        "min_length": len(new_password) >= 6,
        "has_upper": any("A" <= ch <= "Z" for ch in new_password),
        "has_lower": any("a" <= ch <= "z" for ch in new_password),
        "has_special": any(not ("A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9") for ch in new_password),
        "not_student_id": (not student_id) or (new_password != student_id),
        "not_old_password": (current_plain is None) or (new_password != current_plain),
        "confirmed": confirm is not None and new_password != "" and new_password == confirm,
    }


def validate_new_password(user, current: str, new: str, confirm: str) -> list[str]:
    """Server-side checks. Same-as-old is verified via the stored password hash."""
    if not user.check_password(current or ""):
        return ["当前密码不正确。"]
    results = evaluate_password_rules(
        new,
        student_id=user.student_id,
        current_plain=None,
        confirm=confirm,
    )
    # Override not_old_password with a hash comparison against the existing credential.
    results["not_old_password"] = not user.check_password(new or "")
    errors = []
    for key, _label in PASSWORD_RULES:
        if not results[key]:
            errors.append(PASSWORD_RULE_ERRORS[key])
    return errors




STAFF_PASSWORD_MIN_LENGTH = 12

STAFF_PASSWORD_RULES = [
    ("min_length", "长度不少于 12 位"),
    ("has_upper", "包含大写字母"),
    ("has_lower", "包含小写字母"),
    ("has_special", "包含至少一个特殊符号（非字母数字字符）"),
    ("not_old_password", "不能与当前密码相同"),
    ("confirmed", "两次输入的新密码一致"),
]


def _has_upper(value: str) -> bool:
    return any("A" <= ch <= "Z" for ch in value)


def _has_lower(value: str) -> bool:
    return any("a" <= ch <= "z" for ch in value)


def _has_special(value: str) -> bool:
    return any(not ("A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9") for ch in value)


def staff_password_complexity_errors(password: str) -> list[str]:
    password = password or ""
    errors = []
    if len(password) < STAFF_PASSWORD_MIN_LENGTH:
        errors.append("管理员密码长度不能少于 12 位。")
    if not _has_upper(password):
        errors.append("管理员密码必须包含大写字母。")
    if not _has_lower(password):
        errors.append("管理员密码必须包含小写字母。")
    if not _has_special(password):
        errors.append("管理员密码必须包含至少一个特殊符号。")
    return errors


def generate_staff_password(length: int = 16) -> str:
    """Generate a compliant admin password. Never log the return value."""
    if length < STAFF_PASSWORD_MIN_LENGTH:
        length = STAFF_PASSWORD_MIN_LENGTH
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    special = "!@#$%^&*?"
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    alphabet = upper + lower + digits + special
    chars.extend(secrets.choice(alphabet) for _ in range(length - 4))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def iter_staff_users(exclude_id: int | None = None):
    from app.models import User

    query = User.query.filter(User.role.in_(("superadmin", "admin")))
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.all()


def staff_password_is_unique(password: str, exclude_id: int | None = None) -> bool:
    if not password:
        return False
    for user in iter_staff_users(exclude_id=exclude_id):
        if user.check_password(password):
            return False
    return True


def generate_unique_staff_password(exclude_id: int | None = None) -> str:
    for _ in range(30):
        candidate = generate_staff_password()
        if staff_password_is_unique(candidate, exclude_id=exclude_id):
            return candidate
    raise RuntimeError("无法生成与其他管理员不重复的密码，请手工指定。")


def find_staff_by_password(password: str):
    matches = [
        user
        for user in iter_staff_users()
        if user.check_password(password or "") and getattr(user, "is_account_active", True)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def validate_new_staff_password(user, current: str, new: str, confirm: str) -> list[str]:
    if not user.check_password(current or ""):
        return ["当前密码不正确。"]
    errors = staff_password_complexity_errors(new)
    if user.check_password(new or ""):
        errors.append("新密码不能与旧密码相同。")
    if new != confirm:
        errors.append("两次输入的新密码不一致。")
    if new and not staff_password_is_unique(new, exclude_id=user.id):
        errors.append("新密码不能与其他管理员的密码相同。")
    return errors


def evaluate_staff_password_rules(new_password: str, *, current_plain: str | None = None, confirm: str | None = None) -> dict[str, bool]:
    new_password = new_password or ""
    return {
        "min_length": len(new_password) >= STAFF_PASSWORD_MIN_LENGTH,
        "has_upper": _has_upper(new_password),
        "has_lower": _has_lower(new_password),
        "has_special": _has_special(new_password),
        "not_old_password": (current_plain is None) or (new_password != current_plain),
        "confirmed": confirm is not None and new_password != "" and new_password == confirm,
    }


def normalize_role(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    return ROLE_ALIASES.get(key)


def _cell_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _header_map(headers: list[str]) -> dict[str, int]:
    aliases = {
        "姓名": "name",
        "name": "name",
        "学号": "student_id",
        "账号": "student_id",
        "student_id": "student_id",
        "角色": "role",
        "role": "role",
        "初始密码": "password",
        "密码": "password",
        "password": "password",
    }
    mapping = {}
    for idx, raw in enumerate(headers):
        key = aliases.get(_cell_str(raw))
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def _row_to_record(mapping: dict[str, int], row: list) -> dict | None:
    def get(field: str) -> str:
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return _cell_str(row[idx])

    name = get("name")
    student_id = get("student_id")
    role_raw = get("role")
    password = get("password")
    if not any((name, student_id, role_raw, password)):
        return None
    return {
        "name": name,
        "student_id": student_id,
        "role_raw": role_raw,
        "password": password,
    }



def _split_name_id(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    for sep in ("/", "／", ",", "，"):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    return text, ""


def parse_members_from_row(headers: list[str], row: list) -> tuple[str, list[dict]]:
    """Extract 队名 and 队员N姓名/学号 columns from an import row."""
    team_name = ""
    paired: dict[int, dict] = {}
    for idx, raw in enumerate(headers):
        label = _cell_str(raw)
        value = _cell_str(row[idx]) if idx < len(row) else ""
        if label in {"队名", "队伍", "队伍名称", "team", "team_name"}:
            team_name = value
            continue
        m = re.fullmatch(r"队员\s*(\d+)\s*姓名", label)
        if m:
            paired.setdefault(int(m.group(1)), {})["name"] = value
            continue
        m = re.fullmatch(r"队员\s*(\d+)\s*学号", label)
        if m:
            paired.setdefault(int(m.group(1)), {})["student_id"] = value
            continue
        m = re.fullmatch(r"队员\s*(\d+)\s*姓名[/／]学号", label)
        if m:
            name, sid = _split_name_id(value)
            paired.setdefault(int(m.group(1)), {}).update(name=name, student_id=sid)
            continue
        m = re.fullmatch(r"队员\s*(\d+)\s*手机", label)
        if m:
            paired.setdefault(int(m.group(1)), {})["phone"] = value
            continue
        m = re.fullmatch(r"队员\s*(\d+)\s*(联系方式|电话|手机号)", label)
        if m:
            paired.setdefault(int(m.group(1)), {})["phone"] = value
    members = []
    for n in sorted(paired):
        name = (paired[n].get("name") or "").strip()
        sid = (paired[n].get("student_id") or "").strip()
        phone = (paired[n].get("phone") or "").strip()
        if name or sid or phone:
            members.append({"name": name, "student_id": sid, "phone": phone or None})
    return team_name, members


def collect_team_form(form) -> tuple[str, list[dict], list[str]]:
    """Read team_name and member_name[] / member_student_id[] / member_phone[] from a form."""
    team_name = (form.get("team_name") or "").strip()
    names = form.getlist("member_name")
    ids = form.getlist("member_student_id")
    phones = form.getlist("member_phone")
    members = []
    errors = []
    for i in range(max(len(names), len(ids), len(phones))):
        name = (names[i] if i < len(names) else "").strip()
        sid = (ids[i] if i < len(ids) else "").strip()
        phone = (phones[i] if i < len(phones) else "").strip()
        if not name and not sid and not phone:
            continue
        if not name or not sid:
            errors.append(f"第 {i + 1} 名队员的姓名和学号须同时填写。")
            continue
        if phone and not is_mobile_phone(phone):
            errors.append(f"第 {i + 1} 名队员的手机号格式不正确。")
            continue
        members.append({"name": name, "student_id": sid, "phone": phone or None})
    return team_name, members, errors


def apply_team_roster(captain, team_name: str, members: list[dict]) -> list[str]:
    from app import db
    from app.models import TeamMember

    errors = []
    seen = set()
    clean = []
    for item in members:
        name = (item.get("name") or "").strip()
        sid = (item.get("student_id") or "").strip()
        if not name or not sid:
            errors.append("队员姓名和学号须同时填写。")
            continue
        if sid == captain.student_id:
            errors.append(f"队员学号不能与队长学号相同（{sid}）。")
            continue
        if sid in seen:
            errors.append(f"队员学号重复：{sid}。")
            continue
        seen.add(sid)
        clean.append({"name": name, "student_id": sid, "phone": (item.get("phone") or "").strip() or None})
    if errors:
        return errors
    captain.team_name = team_name or None
    from app.models import BookingCompanion
    member_ids = [member.id for member in captain.teammates]
    if member_ids:
        BookingCompanion.query.filter(BookingCompanion.team_member_id.in_(member_ids)).update(
            {"team_member_id": None}, synchronize_session="fetch")
    TeamMember.query.filter_by(captain_id=captain.id).delete()
    for item in clean:
        db.session.add(TeamMember(captain_id=captain.id, name=item["name"], student_id=item["student_id"], phone=item.get("phone")))
    return []


def parse_user_import(file_storage) -> tuple[list[dict], list[str]]:
    """Parse an uploaded CSV or Excel file into raw user records."""
    filename = (file_storage.filename or "").lower()
    data = file_storage.read()
    file_storage.stream.seek(0)
    errors = []
    rows: list[list] = []

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
            wb.close()
        except Exception:
            return [], ["无法解析 Excel 文件，请上传有效的 .xlsx 文件。"]
    else:
        # Default to CSV, try utf-8-sig then gbk.
        text = None
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return [], ["无法识别 CSV 编码，请使用 UTF-8 或 GBK。"]
        reader = csv.reader(io.StringIO(text))
        rows = [list(row) for row in reader]

    if not rows:
        return [], ["文件为空。"]

    headers = [_cell_str(h) for h in rows[0]]
    mapping = _header_map(headers)
    missing = [label for label, key in (("姓名", "name"), ("学号", "student_id"), ("角色", "role")) if key not in mapping]
    if missing:
        return [], [f"缺少必要列：{'、'.join(missing)}。"]

    records = []
    for i, row in enumerate(rows[1:], start=2):
        row_list = list(row) if row is not None else []
        record = _row_to_record(mapping, row_list)
        if record is None:
            continue
        team_name, members = parse_members_from_row(headers, row_list)
        record["team_name"] = team_name
        record["members"] = members
        record["row"] = i
        records.append(record)
    if not records:
        errors.append("没有读到有效的数据行。")
    return records, errors


def week_bounds(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday

def password_is_unique_globally(password: str, exclude_id: int | None = None) -> bool:
    """True when no existing account uses this password."""
    from app.models import User

    if not password:
        return False
    query = User.query
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    for user in query.all():
        if user.check_password(password):
            return False
    return True


def validate_new_superadmin_password(user, current: str, new: str, confirm: str) -> list[str]:
    """Required: current password, non-empty, confirm, not old, globally unique.
    Complexity is recommended in the UI but not enforced.
    """
    if not user.check_password(current or ""):
        return ["当前密码不正确。"]
    errors = []
    if not (new or "").strip():
        errors.append("新密码不能为空。")
    if new != confirm:
        errors.append("两次输入的新密码不一致。")
    if new and user.check_password(new):
        errors.append("新密码不能与旧密码相同。")
    if new and not password_is_unique_globally(new, exclude_id=user.id):
        errors.append("新密码不能与其他账号的密码相同。")
    return errors


def parse_team_import(file_storage) -> tuple[list[dict], list[str]]:
    """Parse an Excel/CSV of competition teams. Duplicate student IDs are updates."""
    filename = (file_storage.filename or "").lower()
    data = file_storage.read()
    file_storage.stream.seek(0)
    rows: list[list] = []
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
            wb.close()
        except Exception:
            return [], ["无法解析 Excel 文件，请上传有效的 .xlsx 文件。"]
    else:
        text = None
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return [], ["无法识别 CSV 编码，请使用 UTF-8 或 GBK。"]
        rows = [list(row) for row in csv.reader(io.StringIO(text))]
    if not rows:
        return [], ["文件为空。"]
    headers = [_cell_str(h) for h in rows[0]]
    alias = {
        "队名": "team_name", "队伍": "team_name", "队伍名称": "team_name",
        "队长姓名": "captain_name", "队长学号": "captain_student_id",
        "队长手机": "captain_phone", "队长手机号": "captain_phone", "队长联系方式": "captain_phone",
        "队长邮箱": "captain_email", "队长邮件": "captain_email",
    }
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        key = alias.get(_cell_str(raw))
        if key and key not in mapping:
            mapping[key] = idx
    missing = [label for label, key in (("队名", "team_name"), ("队长姓名", "captain_name"), ("队长学号", "captain_student_id")) if key not in mapping]
    if missing:
        return [], [f"缺少必要列：{'、'.join(missing)}。"]
    records = []
    file_errors: list[str] = []
    for i, row in enumerate(rows[1:], start=2):
        row_list = list(row) if row is not None else []
        def col(key: str) -> str:
            idx = mapping.get(key)
            if idx is None or idx >= len(row_list):
                return ""
            return _cell_str(row_list[idx])
        team_name, members = parse_members_from_row(headers, row_list)
        if not team_name:
            team_name = col("team_name")
        rec = {
            "row": i,
            "team_name": team_name,
            "name": col("captain_name"),
            "student_id": col("captain_student_id"),
            "phone": col("captain_phone"),
            "email": col("captain_email"),
            "members": members,
        }
        if not rec["name"] and not rec["student_id"] and not rec["team_name"] and not members:
            continue
        records.append(rec)
    if not records:
        file_errors.append("没有读到有效的数据行。")
    return records, file_errors


def generate_regular_password(length: int = 12) -> str:
    """A captain-rule-compliant random password. Never log the return value."""
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    special = "!@#$%^&*?"
    chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits), secrets.choice(special)]
    alphabet = upper + lower + digits + special
    chars.extend(secrets.choice(alphabet) for _ in range(max(0, length - 4)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def format_datetime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Unified local time display. Dates use YYYY-MM-DD; datetimes use YYYY-MM-DD HH:MM."""
    if value is None or value == "":
        return "—"
    if hasattr(value, "strftime"):
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)
    return str(value)



def protect_excel_text(workbook) -> None:
    """Exports contain untrusted text, never executable spreadsheet formulas."""
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
