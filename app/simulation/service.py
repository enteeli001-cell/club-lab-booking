"""Resolve trusted library references and fingerprint saved model/results."""
import hashlib
import json

from app.models import SimulationMaterial, SimulationSection
from .schema import normalize, validate_libraries

MATERIAL_FIELDS = ("elastic_modulus", "shear_modulus", "density", "allowable_stress")
SECTION_FIELDS = ("area", "iy", "iz", "wy", "wz", "torsion_j")


def libraries():
    def rows(cls, fields):
        return [{"id": row.id, "name": row.name, "enabled": row.enabled,
                 **{key: getattr(row, key) for key in fields}} for row in cls.query.order_by(cls.id).all()]
    return rows(SimulationMaterial, MATERIAL_FIELDS), rows(SimulationSection, SECTION_FIELDS)


def prepare(data):
    materials, sections = libraries()
    validate_libraries(materials, sections)
    model = normalize(data, materials, sections)
    # Include only referenced presets in the job/snapshot; no client-supplied properties.
    material_ids = {m["material"] for m in model["members"]}
    section_ids = {m["section"] for m in model["members"]}
    return {"model": model,
            "materials": [m for m in materials if str(m["id"]) in material_ids],
            "sections": [s for s in sections if str(s["id"]) in section_ids]}


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
