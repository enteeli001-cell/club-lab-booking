"""Shared eager-loading options and batched lookups for list pages (avoid N+1 queries).

Each list page used to lazy-load the lab, captain, teammates, companions and loan items
row by row, which issued hundreds or thousands of SQL statements on realistic data.
These helpers load the same objects in a fixed, small number of queries.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, and_
from sqlalchemy.orm import selectinload

from app.models import Booking, DutyShift, ToolLoan, User


def booking_list_options(*, user: bool = False, teammates: bool = False, reviewer: bool = False):
    options = [selectinload(Booking.lab), selectinload(Booking.companions)]
    if user:
        loader = selectinload(Booking.user)
        options.append(loader.selectinload(User.teammates) if teammates else loader)
    if reviewer:
        options.append(selectinload(Booking.reviewer))
    return options


def loan_list_options():
    return [
        selectinload(ToolLoan.items),
        selectinload(ToolLoan.lab),
        selectinload(ToolLoan.user).selectinload(User.teammates),
    ]


def pending_conflict_map(bookings) -> dict[int, list]:
    """Same rule as utils.find_conflicts(..., exclude_id=b.id) for every pending booking, in one query.

    Returns {booking_id: [overlapping pending bookings]} only for bookings with such overlaps.
    """
    from app.utils import is_significant_overlap

    pending = [b for b in bookings if b.status == "pending"]
    if not pending:
        return {}
    keys = {(b.lab_id, b.date) for b in pending}
    candidates = Booking.query.filter(
        Booking.status.in_(("pending", "approved")),
        or_(*[and_(Booking.lab_id == lab_id, Booking.date == day) for lab_id, day in keys]),
    ).all()
    by_slot = defaultdict(list)
    for other in candidates:
        by_slot[(other.lab_id, other.date)].append(other)
    hints = {}
    for booking in pending:
        overlaps = [
            other for other in by_slot[(booking.lab_id, booking.date)]
            if other.id != booking.id and other.status == "pending"
            and is_significant_overlap(booking.start_time, booking.end_time, other.start_time, other.end_time)
        ]
        if overlaps:
            hints[booking.id] = overlaps
    return hints


def booking_contacts_map(bookings) -> dict[int, list[dict]]:
    """utils.find_booking_contacts for several bookings with one duty-shift query."""
    from app.utils import find_booking_contacts

    bookings = list(bookings)
    if not bookings:
        return {}
    lab_ids = {b.lab_id for b in bookings}
    shifts = (
        DutyShift.query.options(selectinload(DutyShift.admin))
        .filter(DutyShift.lab_id.in_(lab_ids))
        .all()
    )
    by_lab = defaultdict(list)
    for shift in shifts:
        by_lab[shift.lab_id].append(shift)
    return {b.id: find_booking_contacts(b, shifts=by_lab[b.lab_id]) for b in bookings}
