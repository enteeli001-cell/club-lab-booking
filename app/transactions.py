"""SQLite write transactions for decisions that must survive concurrent requests."""
from sqlalchemy import text
from app import db


def begin_serialized_write() -> None:
    # SQLite SELECTs do not begin a driver transaction in legacy transaction mode.
    # Reserve the writer before reading stock, booking status, or audit state.
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
