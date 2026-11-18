"""Linear elastic 3D Euler-Bernoulli frames and optional axial-only trusses.

Element order: [ux,uy,uz,rx,ry,rz] at i and j. Member local x follows i -> j;
local y = reference cross x, local z = x cross y; optional roll about x.
No distributed loads, shear deformation, geometric stiffness or plasticity.
"""
import math
import time
import warnings

import numpy as np
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import splu

if __package__:
    from .schema import normalize, validate_libraries, SimulationError, DOFS, FORCES
else:  # standalone worker deliberately does not import Flask or the application
    from schema import normalize, validate_libraries, SimulationError, DOFS, FORCES


def element(a, b, member, material, section):
    delta = np.asarray(b, dtype=float) - a
    length = float(np.linalg.norm(delta))
    ex = delta / length
    reference = np.array([0., 0., 1.]) if abs(ex[2]) < .9 else np.array([0., 1., 0.])
    ey = np.cross(reference, ex); ey /= np.linalg.norm(ey)
    ez = np.cross(ex, ey)
    angle = math.radians(member.get("roll", 0))
    ey, ez = math.cos(angle) * ey + math.sin(angle) * ez, -math.sin(angle) * ey + math.cos(angle) * ez
    rotation = np.vstack((ex, ey, ez))
    transform = np.zeros((12, 12))
    for offset in (0, 3, 6, 9): transform[offset:offset+3, offset:offset+3] = rotation
    k = np.zeros((12, 12))
    axial = material["elastic_modulus"] * section["area"] / length
    k[np.ix_([0, 6], [0, 6])] = axial * np.array([[1., -1.], [-1., 1.]])
    if member["type"] == "frame":
        torsion = material["shear_modulus"] * section["torsion_j"] / length
        k[np.ix_([3, 9], [3, 9])] = torsion * np.array([[1., -1.], [-1., 1.]])
        l = length
        bending = np.array([[12, 6*l, -12, 6*l], [6*l, 4*l*l, -6*l, 2*l*l],
                            [-12, -6*l, 12, -6*l], [6*l, 2*l*l, -6*l, 4*l*l]])
        k[np.ix_([1, 5, 7, 11], [1, 5, 7, 11])] = material["elastic_modulus"] * section["iz"] / l**3 * bending
        signs = np.array([1, -1, 1, -1])
        k[np.ix_([2, 4, 8, 10], [2, 4, 8, 10])] = material["elastic_modulus"] * section["iy"] / l**3 * bending * np.outer(signs, signs)
    return k, transform, length


def unstable():
    raise SimulationError("mechanism", "模型几何可变或约束不足（刚度矩阵奇异或接近奇异）。请检查支座、断开的结构及桁架面外约束。")


def solve(model, materials, sections):
    started = time.perf_counter()
    validate_libraries(materials, sections)
    model = normalize(model, materials, sections)
    mats, secs = {str(r["id"]): r for r in materials}, {str(r["id"]): r for r in sections}
    ids = {n["id"]: i for i, n in enumerate(model["nodes"])}
    xyz = np.array([[n[k] for k in ("x", "y", "z")] for n in model["nodes"]])
    ndof = len(ids) * 6
    count = len(model["members"]) * 144
    rows, columns, values = np.empty(count, np.int32), np.empty(count, np.int32), np.empty(count)
    offset = 0
    weight_kg = 0.
    for member in model["members"]:
        i, j = ids[member["start"]], ids[member["end"]]
        material, section = mats[member["material"]], secs[member["section"]]
        local, transform, length = element(xyz[i], xyz[j], member, material, section)
        global_k = transform.T @ local @ transform
        dofs = np.r_[np.arange(6*i, 6*i+6), np.arange(6*j, 6*j+6)]
        rows[offset:offset+144] = np.repeat(dofs, 12)
        columns[offset:offset+144] = np.tile(dofs, 12)
        values[offset:offset+144] = global_k.ravel()
        offset += 144
        weight_kg += section["area"] * length * material["density"]
    k = coo_matrix((values, (rows, columns)), shape=(ndof, ndof)).tocsc()
    k.eliminate_zeros()
    force = np.zeros(ndof)
    for row in model["loads"]:
        start = ids[row["node"]] * 6
        force[start:start+6] += [row[key] for key in FORCES]
    fixed = np.zeros(ndof, bool)
    for row in model["supports"]:
        start = ids[row["node"]] * 6
        fixed[start:start+6] |= [bool(row[key]) for key in DOFS]
    diagonal = k.diagonal()
    inactive = diagonal == 0
    # Axial-only trusses have no rotational stiffness. Remove only unloaded,
    # unused rotations; never hide a free translational mechanism.
    if np.any(inactive & ~fixed & (np.arange(ndof) % 6 < 3)): unstable()
    if np.any(inactive & ~fixed & (np.abs(force) > 0)):
        raise SimulationError("inactive_load", "荷载作用于无刚度自由度，桁架节点不能承受节点力矩。")
    free = np.flatnonzero(~fixed & ~inactive)
    displacement = np.zeros(ndof)
    if len(free):
        reduced = k[free][:, free]
        scale = 1. / np.sqrt(reduced.diagonal())
        scaled = (diags(scale) @ reduced @ diags(scale)).tocsc()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                factor = splu(scaled, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.)
            pivots = np.abs(factor.U.diagonal())
            if not np.all(np.isfinite(pivots)) or pivots.min() < 1e-11 * max(1., pivots.max()): unstable()
            displacement[free] = scale * factor.solve(force[free] * scale)
        except (RuntimeError, Warning, np.linalg.LinAlgError):
            unstable()
        if not np.all(np.isfinite(displacement)): unstable()
        residual = np.linalg.norm((k @ displacement - force)[free]) / max(1., np.linalg.norm(force))
        if residual > 1e-6: unstable()
    else:
        residual = 0.
    reaction = k @ displacement - force
    nodes = []
    for row in model["nodes"]:
        start = ids[row["id"]] * 6
        nodes.append({"id": row["id"], "displacement": displacement[start:start+6].tolist(),
                      "reaction": np.where(fixed[start:start+6], reaction[start:start+6], 0.).tolist(),
                      "magnitude": float(np.linalg.norm(displacement[start:start+3]))})
    members = []
    for member in model["members"]:
        i, j = ids[member["start"]], ids[member["end"]]
        material, section = mats[member["material"]], secs[member["section"]]
        local, transform, length = element(xyz[i], xyz[j], member, material, section)
        dofs = np.r_[np.arange(6*i, 6*i+6), np.arange(6*j, 6*j+6)]
        local_u = transform @ displacement[dofs]
        action = local @ local_u
        axial = float(action[6])
        my, mz = [float(action[x]) for x in (4, 10)], [float(action[x]) for x in (5, 11)]
        bending = max(abs(my[e]) / section["wy"] + abs(mz[e]) / section["wz"] for e in (0, 1))
        stress = abs(axial) / section["area"] + bending
        euler = math.pi**2 * material["elastic_modulus"] * min(section["iy"], section["iz"]) / length**2
        members.append({"id": member["id"], "axial_force": axial, "moment_y": my, "moment_z": mz,
                        "torsion": float(max(abs(action[3]), abs(action[9]))), "end_actions": action.tolist(),
                        "local_displacement": local_u.tolist(), "rotation": transform[:3,:3].tolist(),
                        "length": length, "combined_stress": stress, "stress_ratio": stress / material["allowable_stress"],
                        "weight_kg": length * section["area"] * material["density"]})
        members[-1]["buckling_ratio"] = max(0., -axial) / euler
    worst = max(members, key=lambda r: r["stress_ratio"])
    max_displacement = max(nodes, key=lambda r: r["magnitude"])
    vertical_load = sum(abs(r["fz"]) for r in model["loads"])
    return {"nodes": nodes, "members": members, "summary": {
        "max_stress_ratio": worst["stress_ratio"], "worst_member": worst["id"],
        "max_displacement": max_displacement["magnitude"], "max_displacement_node": max_displacement["id"],
        "weight_kg": weight_kg, "vertical_load_n": vertical_load,
        "load_weight_ratio": vertical_load / (weight_kg * 9.80665) if weight_kg else 0.,
        "overstressed": sum(m["stress_ratio"] > 1 for m in members),
        "low_utilization": sum(m["stress_ratio"] < .1 for m in members),
        "buckling_candidates": sum(m["buckling_ratio"] > 1 for m in members),
        "residual": float(residual), "dofs": ndof, "free_dofs": int(len(free)), "nnz": int(k.nnz),
        "solve_seconds": time.perf_counter() - started,
    }}
