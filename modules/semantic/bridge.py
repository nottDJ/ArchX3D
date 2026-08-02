"""
ArchX3D — CAD → Classifier Bridge
=================================
Turns a ``CadDocument`` and a set of segmented regions into the
``RoomEvidenceInput`` records the classifier consumes.

Why a separate module
---------------------
The classifier is deliberately ignorant of both CAD and vision types, so that
it can be tested with hand-built inputs and reused against a scene graph whose
CAD document is long gone. Something has to do the joining, and doing it here
keeps that knowledge in one place instead of spreading spatial queries through
the pipeline.

The interesting work is spatial. Evidence only counts for the room it is
physically inside, and "inside" is less obvious than it sounds:

* A room label is often placed slightly outside a small room's polygon, with
  a leader pointing in. Strict containment loses it, so containment is tried
  first and a bounded nearest-room fallback second.
* A fixture block's insertion point can sit on the wall face — a wall-hung
  basin's is *in* the wall. Points within a tolerance of a polygon are
  attributed to it.
* Doors belong to two rooms at once, which is the entire point of a door.

Every attribution is distance-attenuated rather than binary, so a label
2 metres outside a room contributes far less than one at its centre.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .classifier import RoomEvidenceInput

Point = Tuple[float, float]

#: How far outside a polygon an entity may sit and still be attributed to it.
#: Roughly one wall thickness plus drafting slop.
ATTRIBUTION_TOLERANCE_M = 0.45

#: A room label may sit this far outside a small room before it is ignored.
#: Generous because leader-pointed labels are common on bathrooms and stores.
LABEL_TOLERANCE_M = 1.50

#: An opening within this distance of a room's boundary serves that room.
OPENING_TOLERANCE_M = 0.60


def build_inputs(
    document,
    regions: Sequence[Any],
    *,
    vision_by_room: Optional[Dict[str, Tuple[str, float, List[str]]]] = None,
) -> List[RoomEvidenceInput]:
    """Assemble one ``RoomEvidenceInput`` per region.

    ``document`` is a ``cad.CadDocument`` (or ``None`` when no CAD evidence is
    available — the classifier then runs on geometry and vision alone).
    ``regions`` are ``vision.rooms.RoomRegion`` objects, or anything exposing
    ``id`` / ``polygon`` / ``area`` / ``bounds_min`` / ``bounds_max`` /
    ``centroid`` / ``connected_to``.
    """
    vision_by_room = vision_by_room or {}
    inputs: List[RoomEvidenceInput] = []

    doors, windows = _openings(document)

    # Labels and blocks are assigned to exactly one room each, globally,
    # before any per-room work. Doing it per-room with a distance tolerance
    # lets one label name two rooms: a bathroom label sitting near the party
    # wall reaches the bedroom next door and asserts itself there just as
    # strongly, which is how a master bedroom containing a bed gets classified
    # as a bathroom. A label names one room; so does a block sit in one room.
    labels_by_room: Dict[str, List[Tuple[str, str, float]]] = {}
    attributes_by_room: Dict[str, List[Tuple[str, str]]] = {}
    blocks_by_room: Dict[str, List[Tuple[str, str, float, float]]] = {}
    declared_area_by_room: Dict[str, float] = {}

    # Opening counts only count as evidence when the drawing actually carries
    # opening data. A plan with no door or glazing layer yields zero for every
    # room, which says nothing about any of them.
    openings_known = bool(doors or windows)

    if document is not None:
        labels_by_room, attributes_by_room = _assign_labels(document, regions)
        blocks_by_room = _assign_blocks(document, regions)
        declared_area_by_room = _assign_declared_areas(document, regions)

    for region in regions:
        region_id = str(region.id)
        polygon = list(getattr(region, "polygon", []) or [])
        centroid = tuple(getattr(region, "centroid", (0.0, 0.0)))
        bounds_min = tuple(getattr(region, "bounds_min", (0.0, 0.0)))
        bounds_max = tuple(getattr(region, "bounds_max", (0.0, 0.0)))

        room = RoomEvidenceInput(
            room_id=region_id,
            area=float(getattr(region, "area", 0.0) or 0.0),
            polygon=polygon,
            width=max(0.0, bounds_max[0] - bounds_min[0]),
            depth=max(0.0, bounds_max[1] - bounds_min[1]),
            centroid=centroid,
            neighbours=list(getattr(region, "connected_to", []) or []),
        )

        if document is not None:
            room.labels = labels_by_room.get(region_id, [])
            room.name_attributes = attributes_by_room.get(region_id, [])
            room.blocks = blocks_by_room.get(region_id, [])
            room.layer_roles = _layer_roles_for(document, polygon)
            room.door_count = _count_near(doors, polygon)
            room.window_count = _count_near(windows, polygon)
            room.openings_known = openings_known
            room.declared_area = declared_area_by_room.get(region_id, 0.0)

        vision = vision_by_room.get(region_id)
        if vision:
            room.vision_room_type, room.vision_confidence, room.vision_categories = vision

        inputs.append(room)

    return inputs


# ---------------------------------------------------------------------------
# Exclusive assignment
# ---------------------------------------------------------------------------


def _best_region(point: Point, regions: Sequence[Any], tolerance: float):
    """The one region an entity belongs to, and how strongly.

    Containment wins outright. Only when no region contains the point does the
    nearest region within ``tolerance`` claim it, at a strength that falls off
    with distance — which is what keeps a leader-pointed label on a small
    bathroom working without letting it also speak for the room next door.

    Returns ``(region, weight)``, or ``(None, 0.0)``.
    """
    containing = [
        region for region in regions
        if _point_in_polygon(point, list(getattr(region, "polygon", []) or []))
    ]
    if containing:
        # Overlapping polygons are possible after raster segmentation; the
        # smallest is the most specific claim.
        best = min(containing, key=lambda r: float(getattr(r, "area", 0.0) or 0.0))
        return best, 1.0

    nearest, nearest_distance = None, float("inf")
    for region in regions:
        polygon = list(getattr(region, "polygon", []) or [])
        if len(polygon) < 3:
            continue
        distance = _distance_to_polygon(point, polygon)
        if distance < nearest_distance:
            nearest, nearest_distance = region, distance

    if nearest is None or nearest_distance > tolerance:
        return None, 0.0

    return nearest, 1.0 - 0.5 * (nearest_distance / tolerance)


def _assign_labels(document, regions: Sequence[Any]):
    """Assign every room label and name attribute to exactly one region."""
    labels: Dict[str, List[Tuple[str, str, float]]] = {}
    attributes: Dict[str, List[Tuple[str, str]]] = {}

    for text in document.texts:
        if text.role != "room_label" or not text.room_type:
            continue

        region, weight = _best_region(text.insert, regions, LABEL_TOLERANCE_M)
        if region is None:
            continue

        region_id = str(region.id)
        if text.dxftype == "ATTRIB":
            attributes.setdefault(region_id, []).append((text.text, text.room_type))
        else:
            labels.setdefault(region_id, []).append(
                (text.text, text.room_type, round(text.confidence * weight, 3))
            )

    return labels, attributes


def _assign_declared_areas(document, regions: Sequence[Any]) -> Dict[str, float]:
    """The floor area each room's own label states, where it states one."""
    declared: Dict[str, float] = {}

    for text in document.texts:
        if text.role != "room_label" or not text.value or text.value <= 0:
            continue
        region, _ = _best_region(text.insert, regions, LABEL_TOLERANCE_M)
        if region is None:
            continue
        # Keep the largest claim when a room carries several annotations; a
        # label stating both an area and a dimension pair should not have the
        # smaller of the two silently win.
        region_id = str(region.id)
        declared[region_id] = max(declared.get(region_id, 0.0), float(text.value))

    return declared


def _assign_blocks(document, regions: Sequence[Any]):
    """Assign every meaningful block to exactly one region."""
    ignored = {"annotation", "north_arrow", "grid_bubble", "title_block", "unknown"}
    assigned: Dict[str, List[Tuple[str, str, float, float]]] = {}

    for block in document.blocks:
        if not block.category or block.kind in ignored:
            continue
        # Doors and windows are counted as openings, not as room contents:
        # a door belongs to two rooms at once, which exclusive assignment
        # cannot express and `_count_near` handles correctly.
        if block.kind in ("door", "window"):
            continue

        region, weight = _best_region(
            block.position, regions, ATTRIBUTION_TOLERANCE_M
        )
        if region is None:
            continue

        centroid = tuple(getattr(region, "centroid", (0.0, 0.0)))
        distance = math.dist(block.position, centroid) if centroid else 0.0
        assigned.setdefault(str(region.id), []).append(
            (block.category, block.kind, block.confidence * weight, round(distance, 3))
        )

    return assigned


# ---------------------------------------------------------------------------
# Spatial attribution
# ---------------------------------------------------------------------------


def _layer_roles_for(document, polygon: Sequence[Point]) -> Dict[str, int]:
    """Histogram of layer roles for geometry inside the polygon."""
    counts: Dict[str, int] = {}
    if len(polygon) < 3:
        return counts

    interesting = set(_LAYER_ROLES_WORTH_COUNTING)

    for segment in document.segments:
        if segment.role not in interesting:
            continue
        if _point_in_polygon(segment.midpoint, polygon):
            counts[segment.role] = counts.get(segment.role, 0) + 1

    return counts


#: Only roles that carry room-type evidence are counted, so the histogram
#: stays small and the classifier is not handed thousands of wall segments.
_LAYER_ROLES_WORTH_COUNTING = (
    "plumbing_fixture", "casework", "appliance", "furniture", "stair", "plumbing",
)


def _openings(document) -> Tuple[List[Point], List[Point]]:
    """Door and window positions, from blocks and from door/window layers."""
    doors: List[Point] = []
    windows: List[Point] = []

    if document is None:
        return doors, windows

    for block in document.blocks:
        if block.kind == "door":
            doors.append(block.position)
        elif block.kind == "window":
            windows.append(block.position)

    # Drawings that draw openings as plain geometry rather than as blocks are
    # common. Each contiguous run on a door/window layer is collapsed to one
    # opening by clustering, so a door drawn as an arc plus two lines counts
    # once rather than three times.
    doors.extend(_cluster([s.midpoint for s in document.segments_with_role("door")]))
    windows.extend(_cluster([s.midpoint for s in document.segments_with_role("window")]))

    return doors, windows


def _cluster(points: Sequence[Point], radius: float = 0.9) -> List[Point]:
    """Collapse nearby points to their centroids.

    Single-link agglomeration with a fixed radius. A door leaf, its swing arc
    and its frame are all within a door's width of each other, so one pass at
    ~0.9 m merges them without merging two genuinely separate doors, which are
    almost never that close in a residential plan.
    """
    remaining = list(points)
    clusters: List[List[Point]] = []

    while remaining:
        seed = remaining.pop()
        group = [seed]
        changed = True
        while changed:
            changed = False
            for point in list(remaining):
                if any(math.dist(point, member) <= radius for member in group):
                    group.append(point)
                    remaining.remove(point)
                    changed = True
        clusters.append(group)

    return [
        (sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g))
        for g in clusters
    ]


def _count_near(points: Sequence[Point], polygon: Sequence[Point]) -> int:
    """How many points lie on or just inside this polygon's boundary.

    Openings sit *in* the wall, so they are on the boundary rather than
    inside it — containment alone finds almost none of them.
    """
    if len(polygon) < 3:
        return 0
    return sum(
        1 for point in points
        if _distance_to_polygon(point, polygon) <= OPENING_TOLERANCE_M
        or _point_in_polygon(point, polygon)
    )


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------
#
# Local copies rather than imports from ``vision.geometry2d``: this package
# must not depend on the vision package, so that CAD-only classification works
# without numpy, scipy or an API key present.


def _point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            denominator = yj - yi
            if abs(denominator) > 1e-12 and x < (xj - xi) * (y - yi) / denominator + xi:
                inside = not inside
        j = i
    return inside


def _distance_to_polygon(point: Point, polygon: Sequence[Point]) -> float:
    """Shortest distance from ``point`` to the polygon boundary."""
    if len(polygon) < 2:
        return float("inf")
    best = float("inf")
    for i in range(len(polygon)):
        a = polygon[i]
        b = polygon[(i + 1) % len(polygon)]
        best = min(best, _distance_to_segment(point, a, b))
    return best


def _distance_to_segment(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-12:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def _within(point: Point, polygon: Sequence[Point], tolerance: float) -> bool:
    return _point_in_polygon(point, polygon) or (
        _distance_to_polygon(point, polygon) <= tolerance
    )
