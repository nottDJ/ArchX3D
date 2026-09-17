"""
ArchX3D — Geometric quality metrics against ground truth
========================================================
Measures a reconstruction against what is actually in the drawing: where the
openings are and what they are, where the walls run and how thick they are,
how many rooms there are and how big, what the footprint covers, and whether
the scale is right.

Why measurements and not assertions
-----------------------------------
"The tests pass" was true of the engine that produced a 0.8 m building. A
metric is a number a wrong reconstruction cannot accidentally hit: a footprint
IoU of 0.97 cannot come from the wrong unit, and a door precision of 1.0 over
148 doors cannot come from guessing.

Truth documents
---------------
A truth document is JSON in **metres in the drawing's own frame** — the
coordinates the drafter used, converted to metres, before the reconstruction
moved the origin::

    {"openings": [{"kind": "door", "centre": [x, y], "width": w}, ...],
     "walls":    [{"start": [x, y], "end": [x, y], "thickness": t}, ...],
     "rooms":    [{"label": "KITCHEN", "area": 12.4, "point": [x, y]}, ...],
     "footprint": [[x, y], ...],
     "extent_m":  [w, d]}

Every key is optional; each metric is computed from whatever the document
provides. The fixture generators write one beside every generated plan.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .ir import Drawing

XY = Tuple[float, float]

#: An opening is found when its centre lands within this distance of the
#: true opening's centre, in metres.
OPENING_MATCH = 0.35

#: A wall is found when its centreline lies within this distance of the true
#: centreline over most of its length, in metres.
WALL_MATCH = 0.12

#: A reported window this close to truth glazing (a curtain wall) is on it,
#: in metres.
GLAZING_REACH = 0.3

DOOR_TRUTH = ("door", "double_door")
DOOR_FOUND = ("door",)


def load_truth(dxf_path: str) -> Optional[dict]:
    path = os.path.splitext(dxf_path)[0] + ".truth.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _to_drawing_frame(drawing: Drawing, p: XY) -> XY:
    """Local metres -> the drawing's own frame, in metres."""
    ox, oy = drawing.origin_offset
    return (p[0] + ox, p[1] + oy)


def _ratio(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


# ---------------------------------------------------------------------------
# Openings
# ---------------------------------------------------------------------------

def opening_metrics(drawing: Drawing, truth: dict) -> dict:
    """Precision and recall by opening class, and position/width error.

    A reported door that is not a real door is a false positive whatever
    else is near it; a probable door and an unknown opening are not doors and
    cannot be false doors — they are tallied separately so the price of
    precision (doors reported only as probable) stays visible.
    """
    real = [(o["kind"], tuple(o["centre"]), float(o.get("width", 0.0)))
            for o in truth.get("openings", [])]
    found = [(o.classification, _to_drawing_frame(drawing, o.position), o.width, o)
             for o in drawing.openings]

    def nearest(point: XY, pool) -> Optional[int]:
        best = None
        for i, cand in enumerate(pool):
            d = math.dist(point, cand[1])
            if d <= OPENING_MATCH and (best is None or d < best[0]):
                best = (d, i)
        return None if best is None else best[1]

    # Glazing that is not a window element — a curtain wall's panels. A window
    # reported along it is neither found nor false; it is counted apart.
    glazing = None
    if truth.get("glazing"):
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        glazing = unary_union([Polygon(g["polygon"]).buffer(0) for g in truth["glazing"]
                               if len(g["polygon"]) >= 3]).buffer(GLAZING_REACH)

    out: Dict[str, object] = {}
    for label, truth_kinds, found_classes in (
            ("door", DOOR_TRUTH, DOOR_FOUND),
            ("window", ("window",), ("window",))):
        t_pool = [r for r in real if r[0] in truth_kinds]
        f_pool = [f for f in found if f[0] in found_classes]
        neutral = 0
        if label == "window" and glazing is not None:
            from shapely.geometry import Point
            kept = []
            for f in f_pool:
                if nearest(f[1], t_pool) is None and glazing.contains(Point(*f[1])):
                    neutral += 1
                else:
                    kept.append(f)
            f_pool = kept
        matched_t = set()
        tp = 0
        pos_err: List[float] = []
        width_err: List[float] = []
        for f in f_pool:
            k = nearest(f[1], [t for i, t in enumerate(t_pool) if i not in matched_t])
            if k is None:
                continue
            free = [i for i in range(len(t_pool)) if i not in matched_t]
            i = free[k]
            matched_t.add(i)
            tp += 1
            pos_err.append(math.dist(f[1], t_pool[i][1]))
            width_err.append(abs(f[2] - t_pool[i][2]))
        out[label] = {
            "truth": len(t_pool), "reported": len(f_pool), "true_positive": tp,
            "false_positive": len(f_pool) - tp, "missed": len(t_pool) - tp,
            "precision": _ratio(tp, len(f_pool)), "recall": _ratio(tp, len(t_pool)),
            "mean_position_error_m": round(sum(pos_err) / len(pos_err), 4) if pos_err else None,
            "mean_width_error_m": round(sum(width_err) / len(width_err), 4) if width_err else None,
        }
        if label == "window" and glazing is not None:
            out[label]["on_curtain_wall"] = neutral

    # What became of the real doors that were not reported as doors.
    doors = [r for r in real if r[0] in DOOR_TRUTH]
    fates: Dict[str, int] = {}
    for kind, centre, _w in doors:
        k = nearest(centre, found)
        fate = "not found" if k is None else found[k][0]
        fates[fate] = fates.get(fate, 0) + 1
    out["door_outcomes"] = dict(sorted(fates.items()))
    # Anything reported that matches no real opening at all (a window or a
    # hole in a curtain wall is on real glazing, and is not spurious).
    spurious = [f for f in found if nearest(f[1], real) is None]
    if glazing is not None:
        from shapely.geometry import Point
        spurious = [f for f in spurious if f[0] not in ("window", "unknown_opening")
                    or not glazing.contains(Point(*f[1]))]
    out["spurious_openings"] = len(spurious)
    out["spurious_by_class"] = _count(f[0] for f in spurious)
    return out


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Footprint, rooms, walls, scale
# ---------------------------------------------------------------------------

def _model_footprint(drawing: Drawing):
    """Every storey's envelope as drawn, in the drawing's own frame."""
    from shapely.affinity import translate
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    ox, oy = drawing.origin_offset
    parts = []
    for level in drawing.levels():
        for ring in level.footprint_parts or ([level.footprint] if level.footprint else []):
            if len(ring) >= 3:
                parts.append(translate(Polygon(ring).buffer(0), ox, oy))
    return unary_union(parts) if parts else Polygon()


def footprint_metrics(drawing: Drawing, truth: dict) -> Optional[dict]:
    """IoU of the reconstructed envelope with the true one, and the area ratio."""
    rings = truth.get("footprint")
    if not rings:
        return None
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    if rings and isinstance(rings[0][0], (int, float)):
        rings = [rings]
    real = unary_union([Polygon(r).buffer(0) for r in rings if len(r) >= 3])
    found = _model_footprint(drawing)
    if real.is_empty:
        return None
    inter = real.intersection(found).area
    union = real.union(found).area
    return {"iou": round(inter / union, 4) if union else 0.0,
            "area_ratio": round(found.area / real.area, 4),
            "truth_area_m2": round(real.area, 2), "found_area_m2": round(found.area, 2)}


def room_metrics(drawing: Drawing, truth: dict) -> Optional[dict]:
    """Room count accuracy and area error, matched by containment.

    A true room is found when a reconstructed room contains the point that
    lies inside it; one reconstructed room may be credited once. Rooms the
    truth marks as open (outside the walls, a balcony) are counted separately:
    a wall model is not expected to enclose them.
    """
    rooms = truth.get("rooms")
    if rooms is None:
        return None
    from shapely import prepare
    from shapely.geometry import Point, Polygon
    from shapely.strtree import STRtree
    ox, oy = drawing.origin_offset
    found = []
    for r in drawing.rooms:
        try:
            found.append((r, Polygon([(x + ox, y + oy) for x, y in r.polygon]).buffer(0)))
        except Exception:
            continue
    grown = [p.buffer(0.05) for _r, p in found]
    for g in grown:
        prepare(g)
    tree = STRtree(grown) if grown else None
    reps = [p.representative_point() for _r, p in found]
    enclosed = [t for t in rooms if t.get("enclosed", True) and not t.get("exterior", False)]
    used = set()
    errors: List[float] = []
    matched = 0
    for t in enclosed:
        p = Point(*t["point"])
        region = Polygon(t["polygon"]).buffer(0) if t.get("polygon") else None
        candidates = sorted(int(k) for k in tree.query(p)) if tree is not None else []
        for i in candidates:
            r, poly = found[i]
            if i in used or not grown[i].contains(p):
                continue
            used.add(i)
            matched += 1
            if t.get("area_m2") and region is not None and t.get("spaces", 1) > 1:
                # An open-plan region holding several named spaces may be
                # reported whole or divided along its labels; either way the
                # rooms inside it should add up to it.
                inside = sum(found[j][0].area for j in range(len(found))
                             if region.contains(reps[j]))
                errors.append(abs(inside - t["area_m2"]) / t["area_m2"])
            elif t.get("area_m2"):
                errors.append(abs(r.area - t["area_m2"]) / t["area_m2"])
            break
    interior_found = [r for r, _ in found if not r.is_exterior]
    # How many rooms a correct reading may report: one per walled region, or
    # up to one per named space where a region is open plan.
    fewest = len(enclosed)
    most = sum(max(1, int(t.get("spaces", 1) or 1)) for t in enclosed)
    n = len(interior_found)
    miss = fewest - n if n < fewest else (n - most if n > most else 0)
    return {
        "truth_enclosed": fewest, "truth_rooms_most": most, "reported": n,
        "matched": matched,
        "recall": _ratio(matched, fewest),
        "count_accuracy": round(1.0 - miss / max(fewest, 1), 4),
        "mean_area_error": round(sum(errors) / len(errors), 4) if errors else None,
        "median_area_error": round(sorted(errors)[len(errors) // 2], 4) if errors else None,
    }


def _weighted_median(values: Sequence[Tuple[float, float]]) -> float:
    """Median of ``(value, weight)`` pairs."""
    items = sorted(values)
    total = sum(w for _v, w in items)
    acc = 0.0
    for v, w in items:
        acc += w
        if acc >= total / 2.0:
            return v
    return items[-1][0] if items else 0.0


def wall_metrics(drawing: Drawing, truth: dict) -> Optional[dict]:
    """Where the walls are, how thick and how long, against the truth.

    Truth walls come either as centreline segments with a thickness (the
    generated fixtures) or as solid polygons (converted plans). Position error
    is the distance from sampled points on each reconstructed centreline to
    the truth; recall is the share of true wall that some reconstructed wall
    lies along.
    """
    walls_t = truth.get("walls")
    if not walls_t:
        return None
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    ox, oy = drawing.origin_offset
    found = [((w.start[0] + ox, w.start[1] + oy), (w.end[0] + ox, w.end[1] + oy), w)
             for w in drawing.walls]
    if not found:
        return {"reported": 0}

    if "polygon" in walls_t[0]:
        solid = unary_union([Polygon(w["polygon"]).buffer(0) for w in walls_t])
        thickness = truth.get("wall_thickness_m")
        # A centreline of the solid is not available, so position error is
        # distance *outside* the solid, and recall compares covered area.
        import numpy as np
        import shapely
        grown = solid.buffer(0.01)
        shapely.prepare(grown)
        shapely.prepare(solid)
        ks = np.linspace(0.0, 1.0, 11)
        xs = np.array([a[0] + (b[0] - a[0]) * k for a, b, _w in found for k in ks])
        ys = np.array([a[1] + (b[1] - a[1]) * k for a, b, _w in found for k in ks])
        inside = shapely.contains_xy(grown, xs, ys)
        pts = shapely.points(xs, ys)
        dists = [0.0 if inside[i] else float(shapely.distance(solid, pts[i]))
                 for i in range(len(xs))]
        cover = unary_union([LineString([a, b]).buffer(max(w.thickness, 0.05) / 2 + 0.05)
                             for a, b, w in found])
        recall = solid.intersection(cover).area / solid.area if solid.area else None
        true_length = solid.area / thickness if thickness else None
        thick_err = None
        if thickness:
            # Length-weighted, as the truth's own figure is: a plan's many
            # short partitions otherwise outvote its long exterior walls.
            thick_err = abs(_weighted_median([(w.thickness, w.length)
                                              for _a, _b, w in found]) - thickness)
    else:
        segs = [LineString([tuple(w["start"]), tuple(w["end"])]) for w in walls_t]
        truth_lines = unary_union(segs)
        dists = []
        for a, b, _w in found:
            line = LineString([a, b])
            for k in range(11):
                dists.append(truth_lines.distance(line.interpolate(k / 10.0, normalized=True)))
        cover = unary_union([LineString([a, b]).buffer(0.15) for a, b, _w in found])
        total = sum(s.length for s in segs)
        recall = sum(s.intersection(cover).length for s in segs) / total if total else None
        true_length = total
        errs = []
        for a, b, w in found:
            mid = Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            k = min(range(len(segs)), key=lambda i: segs[i].distance(mid))
            if segs[k].distance(mid) < 0.2:
                errs.append(abs(w.thickness - float(walls_t[k].get("thickness", w.thickness))))
        thick_err = sorted(errs)[len(errs) // 2] if errs else None
    found_length = sum(w.length for _a, _b, w in found)
    return {
        "reported": len(found),
        "mean_position_error_m": round(sum(dists) / len(dists), 4) if dists else None,
        "within_0_1m": round(sum(1 for d in dists if d <= 0.1) / len(dists), 4) if dists else None,
        "recall": round(recall, 4) if recall is not None else None,
        "thickness_error_m": round(thick_err, 4) if thick_err is not None else None,
        "length_error": round(abs(found_length - true_length) / true_length, 4)
        if true_length else None,
    }


def scale_metrics(drawing: Drawing, truth: dict) -> Optional[dict]:
    """Whether the model is the right size: the unit, and area agreement.

    Area, not extent: the extent of a rotated plan depends on the frame it is
    measured in, the area of its envelope does not.
    """
    fp = footprint_metrics(drawing, truth)
    out: Dict[str, object] = {}
    want = truth.get("units")
    if want:
        names = {"mm": "millimetres", "cm": "centimetres", "m": "metres",
                 "in": "inches", "ft": "feet"}
        got = drawing.units.unit_name if drawing.units else None
        out["unit_expected"] = names.get(want, want)
        out["unit_found"] = got
        out["unit_correct"] = got == names.get(want, want)
    if fp:
        out["linear_scale_error"] = round(abs(math.sqrt(max(fp["area_ratio"], 1e-9)) - 1.0), 4)
    return out or None


def evaluate(drawing: Drawing, truth: dict) -> dict:
    """Every metric the truth document supports, in one record."""
    expect = truth.get("expect") or {}
    record = {
        "openings": opening_metrics(drawing, truth) if "openings" in truth else None,
        "footprint": footprint_metrics(drawing, truth),
        "rooms": room_metrics(drawing, truth),
        "walls": wall_metrics(drawing, truth),
        "scale": scale_metrics(drawing, truth),
    }
    if expect:
        record["structure"] = {
            "buildings_expected": expect.get("buildings"),
            "buildings_found": len(drawing.buildings),
            "levels_expected": expect.get("levels"),
            "levels_found": sum(1 for _ in drawing.levels()),
        }
    return record
