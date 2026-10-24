"""Mesh analysis for print preparation (numpy only; scipy is used when available).

Works on vertices in printer coordinates (Z up, mm) and triangle indices:
 * mesh sanity (open / non-manifold edges),
 * orientation candidates scored by overhang area, base contact and height,
 * "body of revolution" test around principal axes -> suitability for 5D
   (cylindrical) printing, where the part turns around a vertical axis in the
   centre of the build plate.
"""
import math

import numpy

_DOWN = numpy.array([0.0, 0.0, -1.0])


def _dedupe(verts, faces, tolerance=1e-4):
    quantized = numpy.ascontiguousarray(numpy.round(verts / tolerance).astype(numpy.int64))
    view = quantized.view(numpy.dtype((numpy.void, quantized.dtype.itemsize * 3))).ravel()
    _, first, inverse = numpy.unique(view, return_index=True, return_inverse=True)
    return verts[first], inverse[faces]


def _triangles(verts, faces):
    tri = verts[faces]
    cross = numpy.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = numpy.linalg.norm(cross, axis=1)
    valid = norm > 1e-12
    tri, cross, norm = tri[valid], cross[valid], norm[valid]
    return tri, cross / norm[:, None], norm / 2.0


def _edge_check(faces, vertex_count):
    edges = numpy.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges.sort(axis=1)
    keys = edges[:, 0].astype(numpy.int64) * int(vertex_count) + edges[:, 1]
    _, counts = numpy.unique(keys, return_counts=True)
    open_edges = int((counts == 1).sum())
    non_manifold = int((counts > 2).sum())
    return {"open_edges": open_edges, "non_manifold_edges": non_manifold,
            "watertight": open_edges == 0 and non_manifold == 0}


def rotation_to_down(direction):
    """Rotation matrix that turns the unit vector `direction` onto -Z."""
    d = numpy.asarray(direction, dtype=numpy.float64)
    d = d / numpy.linalg.norm(d)
    v = numpy.cross(d, _DOWN)
    c = float(numpy.dot(d, _DOWN))
    if numpy.linalg.norm(v) < 1e-9:
        return numpy.eye(3) if c > 0 else numpy.diag([1.0, -1.0, -1.0])
    vx = numpy.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return numpy.eye(3) + vx + vx.dot(vx) * (1.0 / (1.0 + c))


def _evaluate(rotation, tri, normals, areas, support_angle):
    n = normals.dot(rotation.T)
    z = tri.reshape(-1, 3).dot(rotation.T)[:, 2].reshape(-1, 3)
    z_min, z_max = float(z.min()), float(z.max())
    near_bottom = z.max(axis=1) < z_min + 0.3
    down = -n[:, 2]
    threshold = math.sin(math.radians(support_angle))
    base = areas[(down > 0.995) & near_bottom].sum()
    overhang = areas[(down > threshold) & ~near_bottom].sum()
    return float(overhang), float(base), z_max - z_min


def _hull_directions(verts, limit=6):
    try:
        from scipy.spatial import ConvexHull
    except Exception:
        return []
    points = verts
    if len(points) > 20000:
        points = points[numpy.random.RandomState(3).choice(len(points), 20000, replace=False)]
    try:
        hull = ConvexHull(points)
    except Exception:
        return []
    simplices = points[hull.simplices]
    areas = numpy.linalg.norm(numpy.cross(simplices[:, 1] - simplices[:, 0], simplices[:, 2] - simplices[:, 0]), axis=1) / 2.0
    normals = hull.equations[:, :3]
    groups = {}
    for normal, area in zip(normals, areas):
        key = tuple(numpy.round(normal, 2))
        total, vector = groups.get(key, (0.0, numpy.zeros(3)))
        groups[key] = (total + area, vector + normal * area)
    ranked = sorted(groups.values(), key=lambda item: -item[0])
    directions = []
    for total, vector in ranked[:limit]:
        length = numpy.linalg.norm(vector)
        if length > 0:
            directions.append(vector / length)
    return directions


def _sample_surface(tri, areas, count, seed=7):
    rng = numpy.random.RandomState(seed)
    index = rng.choice(len(areas), size=count, p=areas / areas.sum())
    r1 = numpy.sqrt(rng.rand(count))
    r2 = rng.rand(count)
    a, b, c = tri[index, 0], tri[index, 1], tri[index, 2]
    return (1 - r1)[:, None] * a + (r1 * (1 - r2))[:, None] * b + (r1 * r2)[:, None] * c


def _revolution(points, axis):
    axis = axis / numpy.linalg.norm(axis)
    center = points.mean(axis=0)
    rel = points - center
    h = rel.dot(axis)
    radial = rel - h[:, None] * axis
    r = numpy.linalg.norm(radial, axis=1)
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(axis, helper)
    u /= numpy.linalg.norm(u)
    w = numpy.cross(axis, u)
    phi = numpy.arctan2(radial.dot(w), radial.dot(u))
    h_bins, phi_bins = 16, 24
    h_index = numpy.clip(((h - h.min()) / max(h.max() - h.min(), 1e-9) * h_bins).astype(int), 0, h_bins - 1)
    phi_index = numpy.clip(((phi + math.pi) / (2 * math.pi) * phi_bins).astype(int), 0, phi_bins - 1)
    variations, outer, inner = [], [], []
    for hb in range(h_bins):
        mask = h_index == hb
        if mask.sum() < 60:
            continue
        r_max = numpy.full(phi_bins, numpy.nan)
        for pb in range(phi_bins):
            sel = r[mask & (phi_index == pb)]
            if len(sel):
                r_max[pb] = sel.max()
        filled = ~numpy.isnan(r_max)
        if filled.sum() < phi_bins * 0.75:
            variations.append(1.0)
            continue
        mean = numpy.nanmean(r_max)
        variations.append(float(numpy.nanstd(r_max) / mean) if mean > 0 else 1.0)
        outer.append(mean)
        # An inner wall (axial hole) shows up as a cluster of points well inside the outer radius.
        r_slice = r[mask]
        inside = r_slice[r_slice < 0.85 * mean]
        inner.append(float(numpy.median(inside)) if len(inside) >= 0.1 * len(r_slice) else 0.0)
    if not variations:
        return None
    score = max(0.0, 1.0 - float(numpy.median(variations)) * 4.0)
    inner_radius = float(numpy.median(inner)) if inner else 0.0
    return {
        "axis": [round(float(v), 3) for v in axis],
        "center": [round(float(v), 2) for v in center],
        "symmetry_score": round(score, 3),
        "outer_diameter_mm": round(2 * float(numpy.median(outer)), 2) if outer else None,
        "inner_diameter_mm": round(2 * inner_radius, 2) if inner_radius > 0 else None,
        "length_mm": round(float(h.max() - h.min()), 2),
    }


def _direction_label(d):
    names = {(0, 0, -1): "текущая ориентация", (0, 0, 1): "перевернуть (верх вниз)",
             (1, 0, 0): "на бок (+X вниз)", (-1, 0, 0): "на бок (−X вниз)",
             (0, 1, 0): "на бок (+Y вниз)", (0, -1, 0): "на бок (−Y вниз)"}
    key = tuple(int(round(v)) for v in d)
    if numpy.allclose(d, key, atol=1e-3) and key in names:
        return names[key]
    return "грань выпуклой оболочки вниз"


def analyze(verts, faces, support_angle=50.0, five_axis=False, base_diameter=6.0):
    verts = numpy.asarray(verts, dtype=numpy.float64)
    faces = numpy.asarray(faces, dtype=numpy.int64)
    verts, faces = _dedupe(verts, faces)
    tri, normals, areas = _triangles(verts, faces)
    if len(areas) == 0:
        raise ValueError("пустая геометрия")
    total_area = float(areas.sum())
    size = verts.max(axis=0) - verts.min(axis=0)
    cross = numpy.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    volume = abs(float(numpy.einsum("ij,ij->i", tri[:, 0], cross).sum()) / 6.0)
    diagonal = float(numpy.linalg.norm(size)) or 1.0

    # -- orientation candidates ------------------------------------------------
    directions = [numpy.array(d, dtype=float) for d in
                  ((0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]
    directions += _hull_directions(verts)

    points = _sample_surface(tri, areas, 20000)
    covariance = numpy.cov((points - points.mean(axis=0)).T)
    _, eigenvectors = numpy.linalg.eigh(covariance)
    axes = [eigenvectors[:, i] for i in range(3)] + [numpy.array([0.0, 0.0, 1.0])]
    revolutions = []
    for axis in axes:
        result = _revolution(points, axis)
        if result is not None:
            revolutions.append(result)
    revolution = max(revolutions, key=lambda item: item["symmetry_score"]) if revolutions else None

    axis_direction = None
    if revolution is not None and revolution["symmetry_score"] >= 0.6:
        axis = numpy.array(revolution["axis"])
        best_end = None
        for candidate in (axis, -axis):
            overhang, base, height = _evaluate(rotation_to_down(candidate), tri, normals, areas, support_angle)
            if best_end is None or base > best_end[1]:
                best_end = (candidate, base)
        axis_direction = best_end[0]
        directions.append(axis_direction)

    unique = []
    for d in directions:
        d = d / numpy.linalg.norm(d)
        if not any(float(numpy.dot(d, u)) > 0.995 for u in unique):
            unique.append(d)

    candidates = []
    for d in unique:
        overhang, base, height = _evaluate(rotation_to_down(d), tri, normals, areas, support_angle)
        overhang_ratio = overhang / total_area
        score = overhang_ratio * 100.0 + (height / diagonal) * 15.0 - min(base / (0.05 * total_area), 1.0) * 10.0
        is_axis = axis_direction is not None and float(numpy.dot(d, axis_direction / numpy.linalg.norm(axis_direction))) > 0.995
        candidates.append({
            "down_direction": [round(float(v), 4) for v in d],
            "label": "ось вращения вертикально (для 5D)" if is_axis else _direction_label(d),
            "overhang_area_mm2": round(overhang, 1),
            "overhang_percent": round(overhang_ratio * 100.0, 2),
            "base_contact_mm2": round(base, 1),
            "height_mm": round(height, 2),
            "score": round(score, 2),
            "revolution_axis_vertical": bool(is_axis),
        })
    candidates.sort(key=lambda c: c["score"])
    for index, candidate in enumerate(candidates):
        candidate["index"] = index
    best = candidates[0]

    # -- recommendation --------------------------------------------------------
    reasons = []
    mode = "3D"
    orientation_index = best["index"]
    if revolution is not None:
        vertical = abs(revolution["axis"][2]) > 0.98
        revolution["axis_is_vertical_now"] = bool(vertical)
    if five_axis and revolution is not None and revolution["symmetry_score"] >= 0.8:
        mode = "5D"
        reasons.append("Деталь близка к телу вращения (симметрия %.2f): в 5D слои укладываются вокруг оси, "
                       "без ступенчатости и с прочностью поперёк оси." % revolution["symmetry_score"])
        axis_candidates = [c for c in candidates if c["revolution_axis_vertical"]]
        if axis_candidates:
            orientation_index = axis_candidates[0]["index"]
        reasons.append("Для 5D ось вращения должна стоять вертикально и проходить через центр стола (X=0, Y=0).")
        if revolution.get("inner_diameter_mm") and revolution["inner_diameter_mm"] > base_diameter * 0.9:
            reasons.append("Вдоль оси есть отверстие ≈ Ø%.1f мм (база 5D-ядра Ø%.1f мм)." %
                           (revolution["inner_diameter_mm"], base_diameter))
    else:
        if five_axis and revolution is not None and revolution["symmetry_score"] >= 0.6:
            reasons.append("Деталь частично осесимметрична (%.2f) — 5D возможен, но выигрыш не очевиден; "
                           "по умолчанию 3D." % revolution["symmetry_score"])
        elif five_axis:
            reasons.append("Геометрия не похожа на тело вращения — классическая 3D-печать надёжнее.")
        if best["overhang_percent"] > 3.0:
            reasons.append("Даже в лучшей ориентации свесы ~%.1f%% поверхности — нужны поддержки или 5D/разворот."
                           % best["overhang_percent"])
        else:
            reasons.append("В рекомендуемой ориентации свесов почти нет — поддержки не нужны.")
    chosen = candidates[orientation_index]
    return {
        "size_mm": {"x": round(float(size[0]), 2), "y": round(float(size[1]), 2), "z": round(float(size[2]), 2)},
        "volume_cm3": round(volume / 1000.0, 3),
        "surface_cm2": round(total_area / 100.0, 2),
        "triangles": int(len(areas)),
        "mesh_check": _edge_check(faces, len(verts)),
        "support_angle_deg": support_angle,
        "orientation_candidates": candidates,
        "revolution": revolution,
        "recommendation": {
            "printing_mode": mode,
            "orientation_index": orientation_index,
            "orientation": chosen["label"],
            "needs_support": mode == "3D" and chosen["overhang_percent"] > 3.0,
            "reasons": reasons,
        },
    }
