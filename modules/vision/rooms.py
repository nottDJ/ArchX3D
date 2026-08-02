"""
ArchX3D — Room segmentation
===========================
Splits a DXF floor plan into discrete enclosed rooms.

Why this exists
---------------
Until now the pipeline treated an entire plan as one room: `build_room_frame`
took the bounding box of every wall segment. That is fine for a single-room
test file and wrong for any real plan — furniture from a bedroom render could
land in the kitchen, because there was no notion of "kitchen".

Approach: raster flood-fill, not planar graph faces
---------------------------------------------------
The textbook method is to build a planar graph from the wall segments and
enumerate its minimal cycles. That is exact on clean input and fragile on real
CAD, where walls overshoot, stop short, or are drawn as two offset lines with
a gap at every doorway. A single unclosed corner silently merges two rooms.

Rasterising is far more robust to that. Walls are drawn onto a grid, dilated
enough to bridge doorways and small drafting gaps, and the remaining free space
is connected-component labelled. Each interior component is a room. The cost is
resolution-limited boundaries, which is irrelevant here: rooms are used to
*assign* and *contain* furniture, not to build geometry — geometry still comes
from the exact wall segments.

Doorway handling is the one tuning knob. ``gap_closing_m`` must exceed the
widest door opening (so adjacent rooms stay separate) but stay below the
narrowest real passage the plan intends to be open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

from . import geometry2d as g2

Point = Tuple[float, float]

#: Grid resolution in metres. 5 cm keeps a 20 x 20 m plan at 400 x 400 cells —
#: fast, and finer than any placement decision that depends on it.
DEFAULT_RESOLUTION = 0.05

#: Openings up to this width are sealed so adjacent rooms separate cleanly.
#:
#: Raising this is *safer* than lowering it, which is the opposite of the
#: intuition. An unsealed doorway does not merely merge two rooms: the merged
#: region usually reaches an external door or a gap in the wall run, touches
#: the grid border, and is discarded wholesale as exterior. Under-sealing
#: therefore deletes floor area rather than mislabelling it.
#:
#: Measured on the reference plan (``final_plan_19th_may.dxf``), total
#: recovered area against this value:
#:
#:     0.60 m ->   2.9 m2 in  1 region   (the building is gone)
#:     0.90 m ->  41.5 m2 in  9 regions
#:     1.10 m ->  92.1 m2 in 11 regions  (previous default; two bedrooms
#:                                        merged into one 47.7 m2 region)
#:     1.40 m -> 153.6 m2 in 13 regions  (bedrooms separate at 22.8 / 22.6 m2)
#:     2.00 m -> 233.6 m2 in 13 regions  (over-sealed: a 70 m2 region appears
#:                                        where two rooms joined)
#:
#: 1.40 m is chosen because it is where the recovered areas agree with the
#: dimensions the drawing itself states — ``BED ROOM 16'0" X 15'9"`` is 23.4 m2
#: and segments at 22.8, ``16'0" X 10'0"`` is 14.9 m2 and segments at 14.2.
#: That agreement is the check worth trusting, not the region count.
#:
#: The constraint from both sides is unchanged: this must exceed the widest
#: door on the plan (the reference set has a 1.25 m double door) and stay below
#: the narrowest opening that is genuinely meant to read as one space.
DEFAULT_GAP_CLOSING = 1.40

#: Regions smaller than this are cupboards, wall cavities or raster noise.
MIN_ROOM_AREA = 2.0

#: Typical floor areas in m², used to guess what a region is for when the plan
#: carries no room labels. Ranges overlap deliberately; the matcher scores
#: rather than hard-classifies.
ROOM_AREA_PRIORS: Dict[str, Tuple[float, float]] = {
    "living_room": (14.0, 60.0),
    "bedroom": (9.0, 30.0),
    "kitchen": (5.0, 18.0),
    "dining_room": (8.0, 25.0),
    "bathroom": (2.5, 8.0),
    "office": (6.0, 16.0),
    "hallway": (2.0, 12.0),
    "balcony": (2.0, 12.0),
}


@dataclass
class RoomRegion:
    """One enclosed space found in the plan."""

    id: str
    #: Simplified boundary polygon in plan metres, counter-clockwise.
    polygon: List[Point]
    bounds_min: Point
    bounds_max: Point
    area: float
    centroid: Point
    #: Assigned later, from the imagery that was matched to this region.
    room_type: str = "unknown"
    room_type_confidence: float = 0.0
    #: Ids of wall segments that bound this region.
    wall_ids: List[str] = field(default_factory=list)
    #: Regions reachable through a doorway.
    connected_to: List[str] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    @property
    def aspect(self) -> float:
        short = min(self.width, self.depth)
        return (max(self.width, self.depth) / short) if short > 1e-6 else 1.0

    def contains(self, point: Point) -> bool:
        return g2.point_in_polygon(point, self.polygon)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "room_type": self.room_type,
            "room_type_confidence": round(self.room_type_confidence, 3),
            "polygon": [[round(p[0], 3), round(p[1], 3)] for p in self.polygon],
            "bounds_min": [round(self.bounds_min[0], 3), round(self.bounds_min[1], 3)],
            "bounds_max": [round(self.bounds_max[0], 3), round(self.bounds_max[1], 3)],
            "area": round(self.area, 2),
            "centroid": [round(self.centroid[0], 3), round(self.centroid[1], 3)],
            "wall_ids": list(self.wall_ids),
            "connected_to": list(self.connected_to),
        }


@dataclass
class SegmentationResult:
    regions: List[RoomRegion]
    #: Diagnostics: what the segmentation did and why.
    stats: Dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.regions)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def segment_rooms(
    wall_segments: Sequence[Dict],
    wall_thickness: float = 0.15,
    resolution: float = DEFAULT_RESOLUTION,
    gap_closing_m: float = DEFAULT_GAP_CLOSING,
    min_area: float = MIN_ROOM_AREA,
) -> SegmentationResult:
    """Find enclosed rooms in a set of DXF wall segments.

    ``wall_segments`` are ``{"start": [x, y], "end": [x, y]}`` dicts as emitted
    by ``dxf_extractor``. Returns regions ordered largest-first.
    """
    points = _collect_points(wall_segments)
    if len(points) < 3:
        return SegmentationResult([], {"reason": "not enough wall geometry"})

    min_x = min(p[0] for p in points)
    max_x = max(p[0] for p in points)
    min_y = min(p[1] for p in points)
    max_y = max(p[1] for p in points)

    # One cell of padding all round guarantees the exterior is connected to the
    # border, which is how the outside is identified.
    pad = max(gap_closing_m, wall_thickness) * 2.0 + resolution * 4
    origin = (min_x - pad, min_y - pad)
    cols = int(math.ceil((max_x - min_x + 2 * pad) / resolution)) + 1
    rows = int(math.ceil((max_y - min_y + 2 * pad) / resolution)) + 1

    # Guard against a pathological plan rasterising into gigabytes.
    if cols * rows > 40_000_000:
        return SegmentationResult(
            [], {"reason": f"plan too large to rasterise ({cols}x{rows} cells)"}
        )

    walls = _rasterise_walls(wall_segments, origin, resolution, rows, cols, wall_thickness)

    # Seal doorways so neighbouring rooms do not flood into one another.
    closing_cells = max(1, int(round(gap_closing_m / resolution / 2.0)))
    sealed = ndimage.binary_dilation(
        walls, structure=_disk(closing_cells), iterations=1
    )

    free = ~sealed
    labels, count = ndimage.label(free, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    if count == 0:
        return SegmentationResult([], {"reason": "no free space after wall dilation"})

    exterior_labels = _labels_touching_border(labels)

    regions: List[RoomRegion] = []
    rejected_small = 0
    cell_area = resolution * resolution

    for label_id in range(1, count + 1):
        if label_id in exterior_labels:
            continue

        mask = labels == label_id

        # Undo the sealing dilation so the room reaches its true wall faces.
        grown = ndimage.binary_dilation(mask, structure=_disk(closing_cells), iterations=1)
        grown &= ~walls
        if not grown.any():
            grown = mask

        area = float(grown.sum()) * cell_area
        if area < min_area:
            rejected_small += 1
            continue

        polygon = _mask_to_polygon(grown, origin, resolution)
        if len(polygon) < 3:
            rejected_small += 1
            continue

        ys, xs = np.nonzero(grown)
        bounds_min = (origin[0] + xs.min() * resolution, origin[1] + ys.min() * resolution)
        bounds_max = (origin[0] + xs.max() * resolution, origin[1] + ys.max() * resolution)
        centroid = (
            origin[0] + float(xs.mean()) * resolution,
            origin[1] + float(ys.mean()) * resolution,
        )

        regions.append(
            RoomRegion(
                id=f"room_{len(regions)}",
                polygon=polygon,
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                area=area,
                centroid=centroid,
            )
        )

    # Largest first: the biggest space is usually the most important, and a
    # stable order keeps ids reproducible across runs.
    regions.sort(key=lambda r: -r.area)
    for index, region in enumerate(regions):
        region.id = f"room_{index}"

    _attach_walls(regions, wall_segments)
    _find_connections(regions, gap_closing_m)

    return SegmentationResult(
        regions,
        {
            "grid": f"{cols}x{rows} @ {resolution}m",
            "components_found": int(count),
            "exterior_components": len(exterior_labels),
            "rooms_kept": len(regions),
            "rooms_rejected_small": rejected_small,
            "gap_closing_m": gap_closing_m,
            "total_area_m2": round(sum(r.area for r in regions), 2),
        },
    )


# ---------------------------------------------------------------------------
# Rasterisation helpers
# ---------------------------------------------------------------------------


def _collect_points(wall_segments: Sequence[Dict]) -> List[Point]:
    points: List[Point] = []
    for segment in wall_segments:
        try:
            points.append((float(segment["start"][0]), float(segment["start"][1])))
            points.append((float(segment["end"][0]), float(segment["end"][1])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return points


def _disk(radius: int) -> np.ndarray:
    """Circular structuring element; avoids the axis bias of a square."""
    size = radius * 2 + 1
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    return (xx * xx + yy * yy) <= radius * radius + 1e-9


def _rasterise_walls(
    wall_segments: Sequence[Dict],
    origin: Point,
    resolution: float,
    rows: int,
    cols: int,
    thickness: float,
) -> np.ndarray:
    """Draw every wall segment onto a boolean grid."""
    grid = np.zeros((rows, cols), dtype=bool)

    for segment in wall_segments:
        try:
            x0, y0 = float(segment["start"][0]), float(segment["start"][1])
            x1, y1 = float(segment["end"][0]), float(segment["end"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-9:
            continue

        # Sample along the segment at sub-cell spacing so the line is unbroken.
        steps = max(2, int(length / (resolution * 0.5)) + 1)
        ts = np.linspace(0.0, 1.0, steps)
        xs = ((x0 + (x1 - x0) * ts - origin[0]) / resolution).astype(np.int32)
        ys = ((y0 + (y1 - y0) * ts - origin[1]) / resolution).astype(np.int32)

        valid = (xs >= 0) & (xs < cols) & (ys >= 0) & (ys < rows)
        grid[ys[valid], xs[valid]] = True

    # Give the drawn centrelines their real thickness.
    thickness_cells = max(1, int(round(thickness / resolution / 2.0)))
    return ndimage.binary_dilation(grid, structure=_disk(thickness_cells), iterations=1)


def _labels_touching_border(labels: np.ndarray) -> set:
    """Component ids that reach the grid edge, i.e. the outside world."""
    border = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    border.discard(0)
    return {int(v) for v in border}


# ---------------------------------------------------------------------------
# Mask → polygon
# ---------------------------------------------------------------------------


def _mask_to_polygon(mask: np.ndarray, origin: Point, resolution: float) -> List[Point]:
    """Trace a mask's outer boundary and simplify it to a polygon.

    Moore-neighbour tracing followed by Douglas-Peucker. Keeps L-shaped and
    T-shaped rooms as real polygons instead of collapsing them to bounding
    boxes, which matters when deciding whether a sofa is inside a room.
    """
    boundary = _trace_boundary(mask)
    if len(boundary) < 4:
        return []

    world = [
        (origin[0] + (c + 0.5) * resolution, origin[1] + (r + 0.5) * resolution)
        for r, c in boundary
    ]

    # Tolerance of one cell: removes staircase artefacts, keeps real corners.
    simplified = _douglas_peucker(world, epsilon=resolution * 1.5)

    if len(simplified) > 2 and simplified[0] == simplified[-1]:
        simplified.pop()

    return simplified if len(simplified) >= 3 else []


#: Clockwise Moore neighbourhood offsets.
_NEIGHBOURS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


def _trace_boundary(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Ordered outer-boundary cells of the largest blob in ``mask``."""
    filled = ndimage.binary_fill_holes(mask)
    coords = np.argwhere(filled)
    if coords.size == 0:
        return []

    # Start at the topmost-then-leftmost cell, which is guaranteed on the hull.
    start = tuple(coords[np.lexsort((coords[:, 1], coords[:, 0]))][0])
    rows, cols = filled.shape

    def solid(cell: Tuple[int, int]) -> bool:
        r, c = cell
        return 0 <= r < rows and 0 <= c < cols and bool(filled[r, c])

    boundary = [start]
    current = start
    backtrack = 6  # arrived from the left

    # Bounded so a pathological mask cannot spin forever.
    for _ in range(filled.size * 4):
        found = False
        for step in range(8):
            direction = (backtrack + 1 + step) % 8
            dr, dc = _NEIGHBOURS[direction]
            candidate = (current[0] + dr, current[1] + dc)
            if solid(candidate):
                backtrack = (direction + 4) % 8  # face back where we came from
                current = candidate
                boundary.append(current)
                found = True
                break
        if not found:
            break
        if current == start and len(boundary) > 2:
            break

    return boundary


def _douglas_peucker(points: List[Point], epsilon: float) -> List[Point]:
    """Iterative Douglas-Peucker; iterative to avoid recursion limits."""
    if len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue

        max_distance, index = 0.0, first
        for i in range(first + 1, last):
            distance = _point_line_distance(points[i], points[first], points[last])
            if distance > max_distance:
                max_distance, index = distance, i

        if max_distance > epsilon:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))

    return [p for p, k in zip(points, keep) if k]


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    if start == end:
        return math.dist(point, start)
    x0, y0 = start
    x1, y1 = end
    px, py = point
    numerator = abs((y1 - y0) * px - (x1 - x0) * py + x1 * y0 - y1 * x0)
    return numerator / math.dist(start, end)


# ---------------------------------------------------------------------------
# Relationships between rooms
# ---------------------------------------------------------------------------


def _attach_walls(regions: Sequence[RoomRegion], wall_segments: Sequence[Dict]) -> None:
    """Record which wall segments bound each region."""
    for index, segment in enumerate(wall_segments):
        try:
            start = (float(segment["start"][0]), float(segment["start"][1]))
            end = (float(segment["end"][0]), float(segment["end"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        for region in regions:
            _, distance = g2.nearest_point_on_polygon(midpoint, region.polygon)
            # A wall bounding this room lies close to its traced boundary.
            if distance < 0.45:
                region.wall_ids.append(f"wall_{index}")


def _find_connections(regions: Sequence[RoomRegion], gap_closing_m: float) -> None:
    """Link regions whose boundaries nearly touch — i.e. share a doorway."""
    threshold = gap_closing_m * 1.6

    for i, a in enumerate(regions):
        for b in regions[i + 1 :]:
            if _min_polygon_distance(a.polygon, b.polygon) <= threshold:
                a.connected_to.append(b.id)
                b.connected_to.append(a.id)


def _min_polygon_distance(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Smallest distance between two polygon boundaries (vertex sampled)."""
    best = float("inf")
    for point in a:
        _, distance = g2.nearest_point_on_polygon(point, b)
        best = min(best, distance)
        if best < 1e-6:
            break
    return best


# ---------------------------------------------------------------------------
# Room type priors
# ---------------------------------------------------------------------------


def area_plausibility(room_type: str, area: float) -> float:
    """How plausible is ``area`` for ``room_type``? Returns ``[0, 1]``.

    Used to match an image's declared room type onto a physical region when
    the DXF carries no room labels.
    """
    span = ROOM_AREA_PRIORS.get(room_type)
    if span is None:
        return 0.5

    low, high = span
    if low <= area <= high:
        return 1.0

    # Decay smoothly outside the band rather than dropping to zero, so an
    # unusually large bedroom is still preferred over no match at all.
    if area < low:
        return max(0.0, 1.0 - (low - area) / max(low, 1e-6))
    return max(0.0, 1.0 - (area - high) / max(high, 1e-6))


def fallback_region(wall_segments: Sequence[Dict], padding: float = 0.0) -> RoomRegion:
    """A single region covering the whole plan.

    Used when segmentation finds nothing — an open-plan space, or a plan whose
    walls do not enclose anything. Preserves the previous single-room
    behaviour rather than dropping every object.
    """
    points = _collect_points(wall_segments)
    if not points:
        bounds_min, bounds_max = (0.0, 0.0), (5.0, 4.0)
    else:
        bounds_min = (min(p[0] for p in points) + padding, min(p[1] for p in points) + padding)
        bounds_max = (max(p[0] for p in points) - padding, max(p[1] for p in points) - padding)

    polygon = [
        (bounds_min[0], bounds_min[1]),
        (bounds_max[0], bounds_min[1]),
        (bounds_max[0], bounds_max[1]),
        (bounds_min[0], bounds_max[1]),
    ]
    width = bounds_max[0] - bounds_min[0]
    depth = bounds_max[1] - bounds_min[1]

    return RoomRegion(
        id="room_0",
        polygon=polygon,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        area=width * depth,
        centroid=((bounds_min[0] + bounds_max[0]) / 2.0, (bounds_min[1] + bounds_max[1]) / 2.0),
        wall_ids=[f"wall_{i}" for i in range(len(wall_segments))],
    )
