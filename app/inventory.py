"""Major-tool inventory: borrowed counts come from in-progress (including overdue) loans."""

from __future__ import annotations

from sqlalchemy import func

from app import db
from app.models import ToolCatalog, ToolLoan, ToolLoanItem


def borrowed_quantity(catalog_id: int) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(ToolLoanItem.quantity), 0))
        .join(ToolLoan, ToolLoanItem.loan_id == ToolLoan.id)
        .filter(
            ToolLoanItem.catalog_id == catalog_id,
            ToolLoanItem.is_other.is_(False),
            ToolLoan.status == "borrowing",
        )
        .scalar()
    )
    return int(total or 0)


def remaining_quantity(item: ToolCatalog) -> int:
    return max(0, int(item.total_quantity) - borrowed_quantity(item.id))


def inventory_rows(*, major_only: bool = True) -> list[dict]:
    borrowed = (
        db.session.query(ToolLoanItem.catalog_id.label("catalog_id"),
                         func.sum(ToolLoanItem.quantity).label("quantity"))
        .join(ToolLoan, ToolLoanItem.loan_id == ToolLoan.id)
        .filter(ToolLoanItem.is_other.is_(False), ToolLoan.status == "borrowing")
        .group_by(ToolLoanItem.catalog_id).subquery()
    )
    query = (db.session.query(ToolCatalog, func.coalesce(borrowed.c.quantity, 0))
             .outerjoin(borrowed, ToolCatalog.id == borrowed.c.catalog_id))
    if major_only:
        query = query.filter(ToolCatalog.is_major.is_(True))
    return [{"id": item.id, "name": item.name, "total": item.total_quantity,
             "borrowed": int(quantity), "remaining": max(0, item.total_quantity - int(quantity)),
             "is_major": item.is_major}
            for item, quantity in query.order_by(ToolCatalog.id.asc()).all()]


def catalog_choices() -> list[dict]:
    return sorted(inventory_rows(major_only=False), key=lambda row: row["name"])


def check_loan_quantities(items: list[dict]) -> list[str]:
    needed: dict[int, int] = {}
    names: dict[int, str] = {}
    for item in items:
        if item.get("is_other") or not item.get("catalog_id"):
            continue
        cid = int(item["catalog_id"])
        needed[cid] = needed.get(cid, 0) + int(item["quantity"])
        names[cid] = item.get("name") or names.get(cid, "")
    errors = []
    for cid, qty in needed.items():
        catalog = db.session.get(ToolCatalog, cid)
        if catalog is None:
            errors.append("所选工具无效。")
            continue
        remaining = remaining_quantity(catalog)
        if qty > remaining:
            errors.append(
                f"“{catalog.name}”剩余可借 {remaining} 件，申请数量 {qty} 超出库存，请调整后再提交。"
            )
    return errors
