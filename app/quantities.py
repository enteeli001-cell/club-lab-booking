"""Exact material quantities and non-negative, half-up currency rounding.

UI grammar and bounds are mirrored by static/materials.js (BigInt rationals).
Persistence uses canonical rational TEXT; no SQLite REAL arithmetic is involved.
"""
from fractions import Fraction
import re

MAX_QUANTITY = 1_000_000
MAX_INPUT_LENGTH = 64
MAX_DENOMINATOR = 1_000_000_000
MAX_DECIMALS = 9
QUANTITY_HELP = "数量支持小数、分数或带分数，如 0.5、1/3、1 1/2；小数最多 9 位，分母不超过 1000000000。"
QUANTITY_ERROR = "数量格式无效，请填写大于 0 的小数或分数，如 0.5、1/3、1 1/2。"


def parse_quantity(raw, *, maximum=MAX_QUANTITY, allow_zero=False) -> Fraction | None:
    value = str(raw if raw is not None else "").strip()
    if not value or len(value) > MAX_INPUT_LENGTH or not value.isascii():
        return None
    try:
        mixed = re.fullmatch(r"([0-9]+)[ \t]+([0-9]+)[ \t]*/[ \t]*([0-9]+)", value)
        simple = re.fullmatch(r"([0-9]+)[ \t]*/[ \t]*([0-9]+)", value)
        if mixed:
            whole, numerator, denominator = map(int, mixed.groups())
            if not 0 < numerator < denominator or denominator > MAX_DENOMINATOR:
                return None
            result = Fraction(whole) + Fraction(numerator, denominator)
        elif simple:
            numerator, denominator = map(int, simple.groups())
            if not 0 < denominator <= MAX_DENOMINATOR:
                return None
            result = Fraction(numerator, denominator)
        elif re.fullmatch(r"(?:[0-9]+(?:\.[0-9]{1,9})?|\.[0-9]{1,9})", value):
            result = Fraction(value)
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    if result < 0 or (not allow_zero and result == 0) or result > maximum:
        return None
    return result


def as_quantity(value) -> Fraction:
    """Internal exact conversion; floats, when supplied by callers, mean decimal text."""
    return value if isinstance(value, Fraction) else Fraction(str(value))


def amount_cents(unit_price_cents: int, quantity) -> int:
    amount = as_quantity(quantity) * int(unit_price_cents)
    if amount < 0:
        raise ValueError("Negative material amount")
    return (2 * amount.numerator + amount.denominator) // (2 * amount.denominator)


class ExactQuantity:
    """Public Fraction attribute backed by a new TEXT field and an old integer mirror.

    Migration preserves original integer columns. New writes maintain their whole-part
    mirror for schema compatibility; only *_exact is authoritative for new code.
    """
    def __init__(self, name, *, nullable=False):
        self.name = name
        self.nullable = nullable

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        exact = getattr(instance, self.name + "_exact")
        if exact is not None:
            return Fraction(exact)
        legacy = getattr(instance, "_" + self.name + "_legacy")
        if legacy is None and self.nullable:
            return None
        return Fraction(legacy or 0)

    def __set__(self, instance, value):
        if value is None and self.nullable:
            exact, whole = None, None
        else:
            quantity = as_quantity(value)
            if quantity < 0:
                raise ValueError("Negative material quantity")
            exact, whole = str(quantity), quantity.numerator // quantity.denominator
        setattr(instance, self.name + "_exact", exact)
        setattr(instance, "_" + self.name + "_legacy", whole)


# table -> (new exact column, original integer column), shared by backup and migration.
EXACT_COLUMNS = {
    "material_items": (("stock_exact", "stock"), ("per_team_qty_exact", "per_team_qty"),
                       ("max_per_purchase_exact", "max_per_purchase")),
    "material_claim_items": (("quantity_exact", "quantity"),),
    "material_purchase_items": (("quantity_exact", "quantity"),),
}
