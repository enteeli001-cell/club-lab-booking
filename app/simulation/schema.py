"""Bounded, standard-library-only model validation; SI units throughout."""
import math

MAX_NODES = 180
MAX_MEMBERS = 600
MAX_INPUT_BYTES = 512_000
DOFS = ("ux", "uy", "uz", "rx", "ry", "rz")
FORCES = ("fx", "fy", "fz", "mx", "my", "mz")


class SimulationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def invalid(message="模型格式无效，请检查表格数据。", code="invalid_model"):
    raise SimulationError(code, message)


def number(value, *, positive=False, maximum=1e12):
    if isinstance(value, bool):
        invalid()
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        invalid()
    if not math.isfinite(result) or abs(result) > maximum or (positive and result <= 0):
        invalid("数值超出允许范围或不是有限数值。")
    return result


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        invalid()
    value = str(value).strip()
    if not value or len(value) > 32 or any(c in value for c in "\r\n\t"):
        invalid()
    return value


def normalize(model, materials, sections):
    if not isinstance(model, dict):
        invalid()
    nodes, members = model.get("nodes"), model.get("members")
    if not isinstance(nodes, list) or not isinstance(members, list):
        invalid()
    if not 2 <= len(nodes) <= MAX_NODES or not 1 <= len(members) <= MAX_MEMBERS:
        invalid("模型超过规模限制：最多 180 个节点、600 根杆件。", "size_limit")
    clean = {"nodes": [], "members": [], "supports": [], "loads": []}
    node_ids, member_ids, positions = set(), set(), {}
    for row in nodes:
        if not isinstance(row, dict): invalid()
        nid = identifier(row.get("id"))
        if nid in node_ids: invalid("节点或杆件编号重复。")
        node_ids.add(nid)
        xyz = [number(row.get(key), maximum=1000) for key in ("x", "y", "z")]
        positions[nid] = xyz
        clean["nodes"].append(dict(id=nid, **dict(zip(("x", "y", "z"), xyz))))
    material_map = {str(row["id"]): row for row in materials}
    section_map = {str(row["id"]): row for row in sections}
    connected = set()
    pairs = set()
    for row in members:
        if not isinstance(row, dict): invalid()
        mid = identifier(row.get("id"))
        a, b = identifier(row.get("start")), identifier(row.get("end"))
        if mid in member_ids: invalid("节点或杆件编号重复。")
        member_ids.add(mid)
        if a not in node_ids or b not in node_ids or a == b:
            invalid("杆件端点不存在或长度为零。")
        length = math.dist(positions[a], positions[b])
        if length < 1e-6 or length > 1000: invalid("杆件端点不存在或长度为零。")
        pair = tuple(sorted((a, b)))
        if pair in pairs: invalid("同一节点对存在重复杆件，请合并或调整截面。")
        pairs.add(pair)
        material, section = str(row.get("material")), str(row.get("section"))
        if material not in material_map or section not in section_map:
            invalid("模型只能引用已启用的材料和截面预设。")
        if not material_map[material].get("enabled", True) or not section_map[section].get("enabled", True):
            invalid("模型只能引用已启用的材料和截面预设。")
        kind = row.get("type", "frame")
        if kind not in ("frame", "truss"): invalid()
        clean["members"].append(dict(id=mid, start=a, end=b, material=material, section=section,
                                     type=kind, roll=number(row.get("roll", 0), maximum=360)))
        connected.update((a, b))
    if connected != node_ids: invalid("存在未连接杆件的孤立节点。")
    for key in ("supports", "loads"):
        rows = model.get(key, [])
        if not isinstance(rows, list) or len(rows) > MAX_NODES: invalid()
        seen = set()
        for row in rows:
            if not isinstance(row, dict): invalid()
            nid = identifier(row.get("node"))
            if nid not in node_ids or nid in seen: invalid("支座或荷载节点不存在或重复。")
            seen.add(nid)
            entry = {"node": nid}
            if key == "supports":
                for dof in DOFS:
                    value = row.get(dof, 0)
                    if value not in (0, 1, False, True, "0", "1"): invalid()
                    entry[dof] = int(value)
            else:
                entry.update({force: number(row.get(force, 0), maximum=1e7) for force in FORCES})
            clean[key].append(entry)
    clean["displacement_limit"] = number(model.get("displacement_limit", .005), positive=True, maximum=1)
    return clean


def validate_libraries(materials, sections):
    for row in materials:
        for key in ("elastic_modulus", "shear_modulus", "density", "allowable_stress"):
            row[key] = number(row.get(key), positive=True, maximum=1e13)
    for row in sections:
        for key in ("area", "iy", "iz", "wy", "wz", "torsion_j"):
            row[key] = number(row.get(key), positive=True, maximum=10)
            if row[key] < 1e-18: invalid("截面参数过小，请检查 SI 单位。")

