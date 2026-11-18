"""Upgrade an existing SQLite database without losing production-shaped rows."""
import sqlite3
from fractions import Fraction
from pathlib import Path
import pytest

from app import create_app, db
from app.models import AppSetting, User
from app.quantities import EXACT_COLUMNS


def test_incremental_upgrade_backup_and_idempotent_restart(app, seed, tmp_path):
    path = Path(app.config["DB_PATH"])
    backup_dir = tmp_path / "backups"
    config = {"TESTING": True, "DATA_DIR": app.config["DATA_DIR"], "LOG_DIR": app.config["LOG_DIR"],
              "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"], "DB_PATH": str(path),
              "SECRET_KEY": app.config["SECRET_KEY"], "API_TOKEN": app.config["API_TOKEN"], "BACKUP_DIR": str(backup_dir)}
    with app.app_context():
        row = db.session.get(AppSetting, "feature.material_claim")
        row.value = "0"
        db.session.commit()
        before = [(u.id, u.student_id, u.name, u.password_hash, u.role) for u in User.query.order_by(User.id).all()]
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(path) as connection:
        for table in ("material_claim_items", "material_purchase_items", "material_claims", "material_purchases", "material_items", "email_verifications"):
            connection.execute(f"DROP TABLE {table}")
        for column in ("verified_email", "email_verified_at", "profile_completed_at"):
            connection.execute(f"ALTER TABLE users DROP COLUMN {column}")
    upgraded = create_app(config)
    with upgraded.app_context():
        assert [(u.id, u.student_id, u.name, u.password_hash, u.role) for u in User.query.order_by(User.id).all()] == before
        assert db.session.get(AppSetting, "feature.material_claim").value == "0"
        assert db.session.get(User, seed["member_id"]).email_verified_at is None
    backups = list(backup_dir.glob("app-before-materials-migration-*.db"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o077 == 0
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == len(before)
        assert "verified_email" not in {r[1] for r in connection.execute("PRAGMA table_info(users)")}
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    create_app(config)
    assert list(backup_dir.glob("*.db")) == backups


@pytest.mark.parametrize("partial", [False, True])
def test_fraction_upgrade_preserves_integer_records_and_existing_exact_values(app, seed, tmp_path, partial):
    from app.models import MaterialItem, MaterialClaim, MaterialClaimItem, MaterialPurchase, MaterialPurchaseItem
    path = Path(app.config["DB_PATH"])
    backup_dir = tmp_path / "fraction-backups"
    config = {"TESTING": True, "DATA_DIR": app.config["DATA_DIR"], "LOG_DIR": app.config["LOG_DIR"],
              "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"], "DB_PATH": str(path),
              "SECRET_KEY": app.config["SECRET_KEY"], "API_TOKEN": app.config["API_TOKEN"], "BACKUP_DIR": str(backup_dir)}
    with app.app_context():
        item = MaterialItem(kind="sale", name="旧材料", stock=Fraction(2, 3) if partial else 9,
                            unit="张", per_team_qty=2, max_per_purchase=3, unit_price_cents=235)
        db.session.add(item); db.session.flush()
        claim = MaterialClaim(user_id=seed["member_id"], photo="retained.png", ip="192.0.2.1")
        claim.items.append(MaterialClaimItem(item_id=item.id, name=item.name, unit=item.unit, quantity=2))
        purchase = MaterialPurchase(user_id=seed["member_id"], status="confirmed", total_cents=705, settled=True,
                                    photo="retained-sale.png", ip="192.0.2.2")
        purchase.items.append(MaterialPurchaseItem(item_id=item.id, name=item.name, unit=item.unit, quantity=3, unit_price_cents=235))
        db.session.add_all([claim, purchase]); db.session.commit()
        mid, pid = item.id, purchase.id
        db.session.remove(); db.engine.dispose()
    before = {}
    with sqlite3.connect(path) as conn:
        for table, fields in EXACT_COLUMNS.items():
            for exact, _ in fields:
                if not (partial and exact == "stock_exact"):
                    conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{exact}"')
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            before[table] = columns, conn.execute(f'SELECT * FROM "{table}"').fetchall()
    upgraded = create_app(config)
    with upgraded.app_context():
        item = db.session.get(MaterialItem, mid)
        assert item.stock == (Fraction(2, 3) if partial else 9)
        assert item.per_team_qty == 2 and item.max_per_purchase == 3
        assert MaterialClaim.query.one().items[0].quantity == 2
        record = db.session.get(MaterialPurchase, pid)
        assert record.total_cents == 705 and record.settled is True
        assert record.items[0].quantity == 3 and record.items[0].subtotal_cents == 705
        item.stock = Fraction(1, 3)
        db.session.commit()
    backups = list(backup_dir.glob("app-before-materials-migration-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        for table, (columns, rows) in before.items():
            assert conn.execute(f'SELECT * FROM "{table}"').fetchall() == rows
            assert [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')] == columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restarted = create_app(config)
    with restarted.app_context():
        assert db.session.get(MaterialItem, mid).stock == Fraction(1, 3)
        assert db.session.get(MaterialPurchase, pid).total_cents == 705
        assert db.session.get(MaterialPurchase, pid).settled
    assert list(backup_dir.glob("*.db")) == backups
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
