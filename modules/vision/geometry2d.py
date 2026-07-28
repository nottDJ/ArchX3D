"""
ArchX3D — 2D geometry helpers
=============================
Plan-space primitives shared by grounding, relationship solving and validation.

Stdlib only, so Blender can import it alongside ``schema`` and ``catalog``.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# Polygons
# ---------------------------------------------------------------------------


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray-casting containment test. Points exactly on an edge may go either way."""
    if len(polygon) < 3:
        return False

    x, y = point
    inside = False
    j = len(polygon) - 1

    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
            if x < x_cross:
                inside = not inside
        j = i

    return inside


def polygon_area(polygon: Sequence[Point]) -> float:
    """Absolute shoelace area."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for i in range(len(polygon)):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % len(polygon)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    area = polygon_area(polygon)
    if area < 1e-9:
        if not polygon:
            return (0.0, 0.0)
        return (
            sum(p[0] for p in polygon) / len(polygon),
            sum(p[1] for p in polygon) / len(polygon),
        )

    cx = cy = 0.0
    for i in range(len(polygon)):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % len(polygon)]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    # Re-derive the signed area; the sign must match the cross products above.
    signed = 0.0
    for i in range(len(polygon)):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % len(polygon)]
        signed += x0 * y1 - x1 * y0
    signed /= 2.0

    return (cx / (6.0 * signed), cy / (6.0 * signed))


def shrink_polygon_to_bounds(point: Point, polygon: Sequence[Point], margin: float) -> Point:
    """Pull ``point`` inside ``polygon``, leaving at least ``margin`` clearance.

    Used to keep an object that back-projected outside the room from being
    built through a wall. Returns the original point when already comfortably
    inside.
    """
    if point_in_polygon(point, polygon):
        nearest, distance = nearest_point_on_polygon(point, polygon)
        if distance >= margin:
            return point
        # Inside but too close to an edge: push inward along the edge normal.
        centroid = polygon_centroid(polygon)
        return _step_toward(nearest, centroid, margin)

    nearest, _ = nearest_point_on_polygon(point, polygon)
    centroid = polygon_centroid(polygon)
    return _step_toward(nearest, centroid, margin)


def _step_toward(origin: Point, target: Point, distance: float) -> Point:
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return origin
    return (origin[0] + dx / length * distance, origin[1] + dy / length * distance)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def closest_point_on_segment(point: Point, a: Point, b: Point) -> Tuple[Point, float]:
    """Return the closest point on segment ``ab`` and the distance to it."""
    ax, ay = a
    bx, by = b
    px, py = point

    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy

    if length_sq < 1e-12:
        return a, math.dist(point, a)

    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    closest = (ax + t * dx, ay + t * dy)
    return closest, math.dist(point, closest)


def nearest_point_on_polygon(point: Point, polygon: Sequence[Point]) -> Tuple[Point, float]:
    """Closest point on the polygon boundary, and its distance."""
    if len(polygon) < 2:
        return point, 0.0

    best_point, best_distance = polygon[0], float("inf")
    for i in range(len(polygon)):
        a = polygon[i]
        b = polygon[(i + 1) % len(polygon)]
        candidate, distance = closest_point_on_segment(point, a, b)
        if distance < best_distance:
            best_point, best_distance = candidate, distance

    return best_point, best_distance


def segments_intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """Proper segment intersection test (collinear touching counts as False)."""

    def orientation(p: Point, q: Point, r: Point) -> int:
        value = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        if abs(value) < 1e-12:
            return 0
        return 1 if value > 0 else 2

    o1, o2 = orientation(a1, a2, b1), orientation(a1, a2, b2)
    o3, o4 = orientation(b1, b2, a1), orientation(b1, b2, a2)
    return o1 != o2 and o3 != o4


# ---------------------------------------------------------------------------
# Oriented rectangles (object footprints)
# ---------------------------------------------------------------------------


def rect_corners(cx: float, cy: float, width: float, depth: float, rotation_deg: float) -> List[Point]:
    """Corners of an oriented rectangle, counter-clockwise from back-left."""
    half_w, half_d = width / 2.0, depth / 2.0
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return [
        (cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)
        for dx, dy in ((-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d))
    ]


def _project(corners: Sequence[Point], axis: Point) -> Tuple[float, float]:
    values = [c[0] * axis[0] + c[1] * axis[1] for c in corners]
    return min(values), max(values)


def rect_overlap(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Penetration depth between two convex rectangles via the SAT.

    Returns 0.0 when they are disjoint, otherwise the smallest overlap across
    all separating-axis candidates — which is exactly how far one must move to
    separate them.
    """
    smallest = float("inf")

    for corners in (a, b):
        for i in range(len(corners)):
            x0, y0 = corners[i]
            x1, y1 = corners[(i + 1) % len(corners)]
            edge = (x1 - x0, y1 - y0)
            length = math.hypot(*edge)
            if length < 1e-12:
                continue
            axis = (-edge[1] / length, edge[0] / length)

            min_a, max_a = _project(a, axis)
            min_b, max_b = _project(b, axis)
            overlap = min(max_a, max_b) - max(min_a, min_b)
            if overlap <= 0:
                return 0.0
            smallest = min(smallest, overlap)

    return 0.0 if smallest == float("inf") else smallest


def separation_axis(a: Sequence[Point], b: Sequence[Point]) -> Optional[Point]:
    """Unit axis along which ``b`` should move to escape ``a`` fastest."""
    smallest, best_axis = float("inf"), None

    for corners in (a, b):
        for i in range(len(corners)):
            x0, y0 = corners[i]
            x1, y1 = corners[(i + 1) % len(corners)]
            edge = (x1 - x0, y1 - y0)
            length = math.hypot(*edge)
            if length < 1e-12:
                continue
            axis = (-edge[1] / length, edge[0] / length)

            min_a, max_a = _project(a, axis)
            min_b, max_b = _project(b, axis)
            overlap = min(max_a, max_b) - max(min_a, min_b)
            if overlap <= 0:
                return None
            if overlap < smallest:
                smallest = overlap
                # Orient the axis so it points from a's centre toward b's.
                centre_a = (sum(p[0] for p in a) / len(a), sum(p[1] for p in a) / len(a))
                centre_b = (sum(p[0] for p in b) / len(b), sum(p[1] for p in b) / len(b))
                delta = (centre_b[0] - centre_a[0], centre_b[1] - centre_a[1])
                if delta[0] * axis[0] + delta[1] * axis[1] < 0:
                    axis = (-axis[0], -axis[1])
                best_axis = axis

    return best_axis


def angle_between_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings, in ``[0, 180]``."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def heading_toward(origin: Point, target: Point) -> float:
    """Heading in degrees such that 0 faces +Y, matching ``SceneObject.rotation_z``."""
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(-dx, dy)) % 360.0
