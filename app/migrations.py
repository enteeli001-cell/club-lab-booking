"""Serialize SQLite startup migrations and back up existing feature data first."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import os
import sqlite3
from urllib.parse import quote
import uuid

from flask import current_app

from app import db
from app.quantities import EXACT_COLUMNS


@contextmanager
def migration_guard():
    """One migration process per database, including multi-worker startup.

    Do not use an ORM connection here: the backup must precede create_all/ALTER.
    Failed backups abort startup. Existing databases are never recreated.
    """
    if db.engine.dialect.name != "sqlite" or db.engine.url.database in (None, "", ":memory:"):
        yield
        return
    path = Path(db.engine.url.database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".migration.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size:
            with closing(sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=30)) as source:
                tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = {row[1] for row in source.execute("PRAGMA table_info(users)")}
                required = {"material_items", "material_claims", "material_claim_items", "material_purchases",
                            "material_purchase_items", "email_verifications", "simulation_materials",
                            "simulation_sections", "simulation_models"}
                fractional_upgrade = any(
                    any(exact not in {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
                        for exact, _ in fields)
                    for table, fields in EXACT_COLUMNS.items() if table in tables
                )
                if "users" in tables and (not required <= tables or not {
                    "verified_email", "email_verified_at", "profile_completed_at"
                } <= columns or fractional_upgrade):
                    directory = Path(current_app.config.get("BACKUP_DIR") or path.parent.parent / "backups")
                    directory.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    target = directory / f"app-before-materials-migration-{stamp}-{uuid.uuid4().hex[:8]}.db"
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                    with closing(sqlite3.connect(target)) as backup:
                        source.backup(backup)
                    current_app.logger.info("材料模块迁移前数据库备份已完成：%s", target.name)
        yield


def ensure_simulation_schema():
    """Incremental create_all tables plus insert-only seeds; never reset edited data."""
    import json
    from app.models import SimulationMaterial, SimulationSection, SimulationModel
    from app.simulation.presets import examples

    material = SimulationMaterial.query.filter_by(seed_key="demo_bamboo_v1").first()
    if material is None:
        material = SimulationMaterial(seed_key="demo_bamboo_v1", name="演示等效竹材",
            elastic_modulus=12e9, shear_modulus=.8e9, density=650, allowable_stress=25e6)
        db.session.add(material)
    section = SimulationSection.query.filter_by(seed_key="square_10mm_v1").first()
    if section is None:
        section = SimulationSection(seed_key="square_10mm_v1", name="实心方条 10×10 mm",
            area=.0001, iy=8.333333333333333e-10, iz=8.333333333333333e-10,
            wy=1.6666666666666667e-7, wz=1.6666666666666667e-7, torsion_j=1.406e-9)
        db.session.add(section)
    db.session.flush()
    for key, name, description, model in examples(material.id, section.id):
        if SimulationModel.query.filter_by(seed_key="simulation_v1_"+key).first() is None:
            db.session.add(SimulationModel(seed_key="simulation_v1_"+key, name=name,
                description=description, data_json=json.dumps(model, ensure_ascii=False)))
    db.session.commit()
