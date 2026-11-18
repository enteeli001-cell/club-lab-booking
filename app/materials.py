"""材料领取与购买：库存扣减、验证码与统计的业务逻辑。

* 免费领取（kind=free）：总管理员设定每队份数（各队相同），队长拍照后一次性领取，自动扣库存。
* 购买（kind=sale）：队长选数量、拍照提交 -> 向绑定邮箱发送 6 位数字验证码（5 分钟有效，
  只存 HMAC 摘要，错误次数和重发频率受限）-> 输入验证码后在同一个写事务里复核库存并扣减。

库存扣减使用精确有理数比较和条件 UPDATE，并在 BEGIN IMMEDIATE 写事务内执行，
多进程/多线程并发也不会超卖。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from collections import defaultdict
from fractions import Fraction

from sqlalchemy import func, text

from app import db
from app.models import (
    MaterialClaim,
    MaterialClaimItem,
    MaterialItem,
    MaterialPurchase,
    MaterialPurchaseItem,
    User,
    format_cents,
)
from app import verification as vc
from app.utils import local_now
from app.quantities import parse_quantity, as_quantity, amount_cents, QUANTITY_ERROR
from app.verification import (  # noqa: F401  (re-exported for callers/tests)
    CODE_LENGTH,
    CODE_MAX_ATTEMPTS,
    CODE_MAX_SENDS,
    CODE_RESEND_COOLDOWN_SECONDS,
    CODE_TTL_SECONDS,
    generate_code,
    mask_email,
)
MAX_PRICE_CENTS = 1_000_000  # 10,000.00 per unit
MAX_STOCK = 1_000_000
CODE_MAIL_SUBJECT = "材料购买验证码"


# ---------------------------------------------------------------- parsing helpers

def parse_price_cents(raw) -> int | None:
    """'12.5' -> 1250. None when invalid, negative, more than 2 decimals or too large."""
    value = (str(raw if raw is not None else "")).strip()
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount > Decimal(MAX_PRICE_CENTS) / 100:
        return None
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        return None
    cents = int(scaled)
    if cents > MAX_PRICE_CENTS:
        return None
    return cents


def parse_non_negative_int(raw, *, maximum: int = MAX_STOCK) -> int | None:
    value = (str(raw if raw is not None else "")).strip()
    if not value.isascii() or not value.isdigit() or len(value) > 16:
        return None
    number = int(value)
    return number if number <= maximum else None


def bound_email(user: User) -> str | None:
    """队长绑定（已通过验证码验证）的邮箱；未验证时返回 None。"""
    if user.email_verified_at and (user.verified_email or "").strip():
        return user.verified_email.strip()
    return None


# ---------------------------------------------------------------- item lists

def items_of(kind: str, *, enabled_only: bool = True) -> list[MaterialItem]:
    query = MaterialItem.query.filter_by(kind=kind)
    if enabled_only:
        query = query.filter_by(enabled=True)
    return query.order_by(MaterialItem.sort_order.asc(), MaterialItem.id.asc()).all()


def allotment_items() -> list[MaterialItem]:
    """Enabled free items with a positive per-team quantity."""
    return [item for item in items_of("free") if item.per_team_qty > 0]


def allotment_shortages(items: list[MaterialItem]) -> list[str]:
    return [
        f"「{item.label}」库存不足（剩余 {item.stock}{item.unit}，每队 {item.per_team_qty}{item.unit}）。"
        for item in items
        if item.stock < item.per_team_qty
    ]


def active_claim(user_id: int) -> MaterialClaim | None:
    return MaterialClaim.query.filter_by(user_id=user_id, status="active").order_by(MaterialClaim.id.desc()).first()


# ---------------------------------------------------------------- stock

def _change_stock(item_id: int, delta) -> bool:
    """Exact read/compare/CAS inside a serialized write; never use SQL REAL math.

    The conditional update also protects against a stale snapshot. Read directly
    from SQLite because raw updates bypass the ORM identity map.
    """
    row = db.session.execute(text(
        "SELECT stock, stock_exact FROM material_items WHERE id = :id"
    ), {"id": item_id}).mappings().first()
    if row is None:
        return False
    remaining = as_quantity(row["stock_exact"] if row["stock_exact"] is not None else row["stock"]) + as_quantity(delta)
    if remaining < 0:
        return False
    result = db.session.execute(text(
        "UPDATE material_items SET stock_exact = :new, stock = :whole "
        "WHERE id = :id AND stock_exact IS :old_exact AND stock = :old_whole"
    ), {"id": item_id, "new": str(remaining), "whole": remaining.numerator // remaining.denominator,
        "old_exact": row["stock_exact"], "old_whole": row["stock"]})
    return result.rowcount == 1


def take_stock(item_id: int, quantity) -> bool:
    quantity = as_quantity(quantity)
    if quantity < 0:
        raise ValueError("Negative material deduction")
    return _change_stock(item_id, -quantity) if quantity else True


def give_back_stock(item_id: int | None, quantity) -> None:
    if item_id is None or quantity <= 0:
        return
    if not _change_stock(item_id, quantity):
        raise RuntimeError("Material stock changed during a serialized write")


def expire_session_items() -> None:
    """Raw UPDATEs bypass the ORM; reload MaterialItem rows on next access."""
    for obj in list(db.session.identity_map.values()):
        if isinstance(obj, MaterialItem):
            db.session.expire(obj)


# ---------------------------------------------------------------- purchase selection

def collect_purchase_selection(form, items: list[MaterialItem]) -> tuple[list[tuple[MaterialItem, Fraction]], list[str]]:
    """Read qty_<item_id> fields. Returns [(item, qty)] with qty > 0 and the validation errors."""
    picked: list[tuple[MaterialItem, Fraction]] = []
    errors: list[str] = []
    for item in items:
        raw = (form.get(f"qty_{item.id}") or "").strip()
        if raw in ("", "0"):
            continue
        qty = parse_quantity(raw)
        if qty is None:
            errors.append(QUANTITY_ERROR)
            continue
        if item.stock <= 0:
            errors.append(f"「{item.label}」已售罄，不能选择。")
            continue
        if qty > item.stock:
            errors.append(f"「{item.label}」库存仅剩 {item.stock}{item.unit}，购买数量不能超过库存。")
            continue
        if item.max_per_purchase and qty > item.max_per_purchase:
            errors.append(f"「{item.label}」每次最多购买 {item.max_per_purchase}{item.unit}。")
            continue
        picked.append((item, qty))
    if not picked and not errors:
        errors.append("请至少选择一种材料并填写数量。")
    return picked, errors


def selection_total_cents(picked: list[tuple[MaterialItem, Fraction]]) -> int:
    return sum(amount_cents(item.unit_price_cents, qty) for item, qty in picked)


# ---------------------------------------------------------------- verification code

PURPOSE = "material-purchase"


def code_matches(purchase: MaterialPurchase, code: str) -> bool:
    return vc.digest_matches(purchase.code_hash, PURPOSE, purchase.id, code)


def code_expired(purchase: MaterialPurchase) -> bool:
    return vc.is_expired(purchase.code_expires_at)


def code_seconds_left(purchase: MaterialPurchase) -> int:
    return vc.seconds_left(purchase.code_expires_at)


def resend_wait_seconds(purchase: MaterialPurchase) -> int:
    return vc.resend_wait(purchase.code_sent_at)


def issue_code(purchase: MaterialPurchase):
    """Create a new code, store only its digest, e-mail it. Returns the MailLog row."""
    from app.mailer import send_mail

    code = vc.generate_code()
    now = local_now()
    purchase.code_hash = vc.code_digest(PURPOSE, purchase.id, code)
    purchase.code_expires_at = vc.expires_from_now()
    purchase.code_sent_at = now
    purchase.code_send_count = int(purchase.code_send_count or 0) + 1
    purchase.code_attempts = 0
    db.session.commit()
    lines = [f"  {line.label} × {line.quantity}{line.unit}  单价 {line.unit_price_label} 元  小计 {line.subtotal_label} 元" for line in purchase.items]
    body_template = (
        f"{purchase.user.name} 同学：\n\n"
        f"你正在社团实验室预约管理系统购买材料，验证码为：{{code}}\n"
        f"验证码 {CODE_TTL_SECONDS // 60} 分钟内有效，请勿告诉他人。\n\n"
        "购买明细：\n" + "\n".join(lines) + "\n"
        f"合计：{format_cents(purchase.total_cents)} 元（比赛后统一结算）\n\n"
        "如果不是你本人操作，请忽略本邮件并联系管理员。\n"
    )
    return send_mail(
        purchase.email_to or "",
        CODE_MAIL_SUBJECT,
        body_template.replace("{code}", code),
        log_body=body_template.replace("{code}", "******"),
    )


def cancel_other_pending(user_id: int, keep_id: int | None = None) -> int:
    query = MaterialPurchase.query.filter(MaterialPurchase.user_id == user_id, MaterialPurchase.status == "pending")
    if keep_id is not None:
        query = query.filter(MaterialPurchase.id != keep_id)
    count = 0
    for row in query.all():
        row.status = "cancelled"
        row.code_hash = None
        row.updated_at = local_now()
        row.note = (row.note or "") or "被新的购买申请替代"
        count += 1
    return count


def confirm_purchase(purchase: MaterialPurchase) -> list[str]:
    """Deduct stock for every line (inside the caller's serialized write). Errors -> nothing deducted."""
    errors = []
    # Roll back only the stock deductions on failure. Keep the outer writer lock
    # until the caller has marked this pending purchase cancelled, so another
    # verification cannot confirm it between rollback and cancellation.
    savepoint = db.session.begin_nested()
    for line in purchase.items:
        item = db.session.get(MaterialItem, line.item_id) if line.item_id else None
        if item is None or not item.enabled:
            errors.append(f"「{line.label}」已下架，请重新选择。")
            break
        if int(item.unit_price_cents) != int(line.unit_price_cents):
            errors.append(f"「{line.label}」的单价已调整为 {item.unit_price_label} 元，请重新提交。")
            break
        if not take_stock(item.id, line.quantity):
            db.session.refresh(item)
            errors.append(f"「{line.label}」库存不足（剩余 {item.stock}{line.unit}），请重新选择。")
            break
    if errors:
        savepoint.rollback()
        expire_session_items()
        return errors
    savepoint.commit()
    purchase.status = "confirmed"
    purchase.confirmed_at = local_now()
    purchase.code_hash = None
    purchase.code_expires_at = None
    expire_session_items()
    return []


# ---------------------------------------------------------------- statistics

def claim_statistics() -> dict:
    captains = (
        User.query.filter(User.role == "member", User.enabled.is_(True))
        .order_by(User.team_name.asc(), User.student_id.asc())
        .all()
    )
    active = {c.user_id: c for c in MaterialClaim.query.filter_by(status="active").all()}
    rows = [{"user": user, "claim": active.get(user.id)} for user in captains]
    claimed_ids = set(active)
    # Claims made by captains who are now disabled still count as claimed.
    extra = [c for uid, c in active.items() if uid not in {u.id for u in captains}]
    lines = (
        MaterialClaimItem.query
        .join(MaterialClaim, MaterialClaimItem.claim_id == MaterialClaim.id)
        .filter(MaterialClaim.status == "active")
        .order_by(MaterialClaimItem.name.asc())
        .all()
    )
    totals = defaultdict(Fraction)
    for line in lines:
        totals[(line.name, line.spec, line.unit)] += line.quantity
    return {
        "rows": rows,
        "extra": extra,
        "claimed": sum(1 for r in rows if r["claim"]) + len(extra),
        "unclaimed": sum(1 for r in rows if not r["claim"]),
        "team_count": len(rows),
        "totals": [
            {"label": f"{name}（{spec}）" if spec else name, "unit": unit, "quantity": qty}
            for (name, spec, unit), qty in totals.items()
        ],
        "claimed_ids": claimed_ids,
    }


def purchase_team_totals() -> list[dict]:
    """Per captain: confirmed purchases, total, unsettled amount (应付合计)."""
    rows = (
        db.session.query(
            MaterialPurchase.user_id,
            func.count(MaterialPurchase.id),
            func.coalesce(func.sum(MaterialPurchase.total_cents), 0),
        )
        .filter(MaterialPurchase.status == "confirmed")
        .group_by(MaterialPurchase.user_id)
        .all()
    )
    unsettled = dict(
        db.session.query(MaterialPurchase.user_id, func.coalesce(func.sum(MaterialPurchase.total_cents), 0))
        .filter(MaterialPurchase.status == "confirmed", MaterialPurchase.settled.is_(False))
        .group_by(MaterialPurchase.user_id)
        .all()
    )
    users = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in rows])).all()} if rows else {}
    result = []
    for user_id, count, total in rows:
        user = users.get(user_id)
        result.append({
            "user_id": user_id,
            "user": user,
            "team_name": (user.team_name if user else "") or "",
            "count": int(count),
            "total_cents": int(total or 0),
            "unsettled_cents": int(unsettled.get(user_id, 0) or 0),
        })
    result.sort(key=lambda r: (-r["unsettled_cents"], r["team_name"]))
    return result


def sold_totals() -> list[dict]:
    lines = (
        MaterialPurchaseItem.query
        .join(MaterialPurchase, MaterialPurchaseItem.purchase_id == MaterialPurchase.id)
        .filter(MaterialPurchase.status == "confirmed")
        .order_by(MaterialPurchaseItem.name.asc())
        .all()
    )
    rows = {}
    for line in lines:
        key = line.name, line.spec, line.unit
        row = rows.setdefault(key, {"label": line.label, "unit": line.unit, "quantity": Fraction(0), "amount_cents": 0})
        row["quantity"] += line.quantity
        row["amount_cents"] += line.subtotal_cents
    return list(rows.values())
