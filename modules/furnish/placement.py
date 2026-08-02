"""
ArchX3D — Placement Solver
==========================
Decides where each planned item physically goes inside a room.

Approach: candidate generation and scoring, not constraint solving
------------------------------------------------------------------
A full constraint solver over furniture placement is elegant and, on real
plans, brittle: an over-constrained room yields no solution at all, and the
useful answer in that case is "the bed fits, nothing else does", not "no
layout exists". So each item generates a set of candidate poses, each pose is
scored, and the best feasible one is taken. Items are placed in importance
order and become obstacles for everything after them, which makes the result
deterministic and explicable — every placement can state why it went there.

What the scoring encodes
------------------------
* **Wall affinity.** ``catalog`` already knows a wardrobe belongs against a
  wall (1.0) and a dining table does not (0.0). Candidates along walls are
  generated for the former, in open floor for the latter.
* **Openings.** A door swing must stay clear and a window must not be blocked
  by anything tall. Both come from the scene graph's openings.
* **Circulation.** A corridor of walkable space has to survive. Enforced by
  rejecting a pose that would sever the room's free space.
* **Clearance.** Usable space in front of each item — you cannot open a
  wardrobe with a bed against it.

Coordinates match the scene graph: metres, +Y up in plan, ``rotation_z``
degrees CCW with 0 meaning the object's front faces +Y.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float]

#: Walkable gap that must remain between obstacles, metres. Below this a
#: person cannot pass, and the layout is unusable however tidy it looks.
MIN_CIRCULATION_M = 0.75

#: Clear floor kept in front of a door leaf so it can open.
DOOR_SWING_CLEARANCE_M = 0.85

#: Objects taller than this must not sit in front of a window.
WINDOW_BLOCKING_HEIGHT_M = 1.1

#: Gap left in front of an item for its own use (opening a wardrobe, getting
#: into a bed). Multiplied by the item's own depth for larger pieces.
DEFAULT_USE_CLEARANCE_M = 0.60

#: Items no taller than this are floor coverings, not obstructions. A rug is
#: *meant* to sit under a coffee table, so treating it as something to be
#: avoided both prevents the intended layout and wastes the floor it covers.
FLOOR_COVERING_HEIGHT_M = 0.10

#: How finely wall runs and open floor are sampled for candidate poses.
WALL_SAMPLE_M = 0.25
FLOOR_SAMPLE_M = 0.40


@dataclass
class Obstacle:
    """Anything already placed that a new item must not overlap."""

    corners: List[Point]
    height: float = 1.0
    #: Free-text, used when explaining a rejection.
    label: str = ""


@dataclass
class RoomSpace:
    """The geometry a room offers, in plan metres."""

    polygon: List[Point]
    bounds_min: Point
    bounds_max: Point
    ceiling_height: float = 3.0
    #: Wall runs bounding this room, as ``(start, end)`` pairs.
    walls: List[Tuple[Point, Point]] = field(default_factory=list)
    #: Door centres and widths.
    doors: List[Tuple[Point, float]] = field(default_factory=list)
    #: Window centres and widths.
    windows: List[Tuple[Point, float]] = field(default_factory=list)

    @property
    def centroid(self) -> Point:
        if not self.polygon:
            return (
                (self.bounds_min[0] + self.bounds_max[0]) / 2.0,
                (self.bounds_min[1] + self.bounds_max[1]) / 2.0,
            )
        return (
            sum(p[0] for p in self.polygon) / len(self.polygon),
            sum(p[1] for p in self.polygon) / len(self.polygon),
        )


@dataclass
class Placement:
    """One successfully placed item."""

    category: str
    position: Point
    rotation_z: float
    dimensions: Tuple[float, float, float]
    #: Why this pose was chosen, for diagnostics and the review UI.
    reason: str = ""
    score: float = 0.0
    against_wall: bool = False

    def corners(self) -> List[Point]:
        return rect_corners(
            self.position[0], self.position[1],
            self.dimensions[0], self.dimensions[1], self.rotation_z,
        )


@dataclass
class Rejection:
    """An item that could not be placed, and why. Never silent."""

    category: str
    reason: str


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


class Solver:
    """Places items into a room one at a time, in the order given."""

    def __init__(self, space: RoomSpace, log=None) -> None:
        self.space = space
        self.obstacles: List[Obstacle] = []
        self.placements: List[Placement] = []
        self.rejections: List[Rejection] = []
        self._log = log or (lambda *a, **k: None)

        # Door swings are obstacles from the outset: nothing may be placed in
        # the arc a door needs, and treating them as pre-existing obstacles is
        # simpler and safer than checking them per-candidate.
        for centre, width in space.doors:
            half = max(width, 0.8) / 2.0
            self.obstacles.append(Obstacle(
                corners=rect_corners(
                    centre[0], centre[1],
                    half * 2.0, DOOR_SWING_CLEARANCE_M * 2.0, 0.0,
                ),
                height=2.1,
                label="door swing",
            ))

    # -- public ------------------------------------------------------------

    def place(
        self,
        category: str,
        dimensions: Tuple[float, float, float],
        wall_affinity: float,
        *,
        wall_clearance: float = 0.05,
        prefer_near: Optional[Point] = None,
    ) -> Optional[Placement]:
        """Place one item, or record why it could not be placed."""
        width, depth, height = dimensions
        if width <= 0 or depth <= 0:
            self.rejections.append(Rejection(category, "degenerate dimensions"))
            return None

        candidates = self._candidates(dimensions, wall_affinity, wall_clearance)
        if not candidates:
            self.rejections.append(
                Rejection(category, "no candidate pose inside the room outline")
            )
            return None

        best: Optional[Placement] = None
        best_score = float("-inf")
        blocked = 0

        # A floor covering is laid under whatever is already there, so it is
        # neither blocked by furniture nor owed clearance. Only containment
        # applies to it.
        is_covering = height <= FLOOR_COVERING_HEIGHT_M

        for position, rotation, against_wall, note in candidates:
            corners = rect_corners(position[0], position[1], width, depth, rotation)

            if not self._inside(corners):
                continue
            if not is_covering:
                if self._collides(corners):
                    blocked += 1
                    continue
                if height > WINDOW_BLOCKING_HEIGHT_M and self._blocks_window(corners):
                    blocked += 1
                    continue
                if not self._clearance_free(position, rotation, dimensions):
                    blocked += 1
                    continue

            score = self._score(
                position, rotation, dimensions, against_wall, wall_affinity,
                prefer_near,
            )
            if score > best_score:
                best_score = score
                best = Placement(
                    category=category, position=position, rotation_z=rotation,
                    dimensions=dimensions, reason=note, score=score,
                    against_wall=against_wall,
                )

        if best is None:
            self.rejections.append(Rejection(
                category,
                f"no free pose: {blocked} candidate(s) blocked by existing "
                f"furniture, a door swing or a window" if blocked
                else "no candidate pose fits inside the room",
            ))
            return None

        self.placements.append(best)

        # Floor coverings are laid *under* furniture and never obstruct it.
        if dimensions[2] > FLOOR_COVERING_HEIGHT_M:
            self.obstacles.append(Obstacle(
                corners=best.corners(), height=dimensions[2], label=category
            ))
        return best

    def place_on_surface(
        self,
        category: str,
        dimensions: Tuple[float, float, float],
        carrier: Placement,
    ) -> Optional[Placement]:
        """Place a small item on top of an already-placed one.

        Not routed through the floor solver: a lamp on a bedside table is
        constrained by the table, not by the room, and running it through
        collision detection against the table it is meant to sit on would
        reject every pose.
        """
        placement = Placement(
            category=category,
            position=carrier.position,
            rotation_z=carrier.rotation_z,
            dimensions=dimensions,
            reason=f"on top of {carrier.category}",
            against_wall=False,
        )
        self.placements.append(placement)
        return placement

    # -- candidate generation ----------------------------------------------

    def _candidates(
        self,
        dimensions: Tuple[float, float, float],
        wall_affinity: float,
        wall_clearance: float,
    ) -> List[Tuple[Point, float, bool, str]]:
        """Poses worth trying, best-intentioned first."""
        candidates: List[Tuple[Point, float, bool, str]] = []

        if wall_affinity >= 0.4:
            candidates.extend(self._wall_candidates(dimensions, wall_clearance))

        # Open-floor candidates are generated for everything: a wardrobe in a
        # room whose walls are all occupied is better standing free than not
        # existing, and the score still prefers a wall when one is available.
        candidates.extend(self._floor_candidates(dimensions))
        return candidates

    def _wall_candidates(
        self, dimensions: Tuple[float, float, float], wall_clearance: float
    ) -> List[Tuple[Point, float, bool, str]]:
        """Poses with the item's back against a wall run."""
        width, depth, _ = dimensions
        out: List[Tuple[Point, float, bool, str]] = []
        interior = self.space.centroid

        for start, end in self.space.walls:
            run = math.dist(start, end)
            if run < width * 0.8:
                continue  # Wall too short to take this item.

            # Inward normal, so the item's back is to the wall.
            wall_angle = math.atan2(end[1] - start[1], end[0] - start[0])
            nx, ny = -math.sin(wall_angle), math.cos(wall_angle)
            mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            if (interior[0] - mid[0]) * nx + (interior[1] - mid[1]) * ny < 0:
                nx, ny = -nx, -ny

            # `rotation_z` is measured from +Y, and the object's front points
            # along the inward normal.
            rotation = math.degrees(math.atan2(-nx, ny)) % 360.0

            # Offset from the wall face by half the depth plus its clearance.
            offset = depth / 2.0 + wall_clearance

            steps = max(1, int(run / WALL_SAMPLE_M))
            for i in range(steps + 1):
                t = i / steps if steps else 0.5
                # Keep the item's full width on the wall run.
                margin = (width / 2.0) / run if run > 0 else 0.5
                if t < margin or t > 1.0 - margin:
                    continue
                px = start[0] + (end[0] - start[0]) * t + nx * offset
                py = start[1] + (end[1] - start[1]) * t + ny * offset
                out.append(((px, py), rotation, True, "against a wall"))

        return out

    def _floor_candidates(
        self, dimensions: Tuple[float, float, float]
    ) -> List[Tuple[Point, float, bool, str]]:
        """Poses on open floor, on a grid across the room's bounding box."""
        width, depth, _ = dimensions
        out: List[Tuple[Point, float, bool, str]] = []

        x0, y0 = self.space.bounds_min
        x1, y1 = self.space.bounds_max
        if x1 <= x0 or y1 <= y0:
            return out

        cols = max(1, int((x1 - x0) / FLOOR_SAMPLE_M))
        rows = max(1, int((y1 - y0) / FLOOR_SAMPLE_M))

        # Cap the grid so a large open-plan space cannot explode the search.
        cols, rows = min(cols, 40), min(rows, 40)

        for i in range(cols + 1):
            for j in range(rows + 1):
                px = x0 + (x1 - x0) * (i / cols if cols else 0.5)
                py = y0 + (y1 - y0) * (j / rows if rows else 0.5)
                for rotation in (0.0, 90.0):
                    out.append(((px, py), rotation, False, "in open floor"))
        return out

    # -- feasibility -------------------------------------------------------

    def _inside(self, corners: Sequence[Point]) -> bool:
        """Every corner must lie within the room outline."""
        return all(point_in_polygon(c, self.space.polygon) for c in corners)

    def _collides(self, corners: Sequence[Point]) -> bool:
        return any(
            rects_overlap(corners, obstacle.corners)
            for obstacle in self.obstacles
        )

    def _blocks_window(self, corners: Sequence[Point]) -> bool:
        """A tall item must not stand in front of a window."""
        for centre, width in self.space.windows:
            reach = max(width, 0.9) / 2.0 + 0.35
            for corner in corners:
                if math.dist(corner, centre) <= reach:
                    return True
        return False

    def _clearance_free(
        self,
        position: Point,
        rotation: float,
        dimensions: Tuple[float, float, float],
    ) -> bool:
        """The usable space in front of the item must be free.

        Without this a wardrobe can be placed with a bed hard against its
        doors: geometrically valid, physically useless. This is the check that
        separates "no overlaps" from "actually habitable".
        """
        width, depth, _ = dimensions
        gap = max(DEFAULT_USE_CLEARANCE_M, depth * 0.5)

        theta = math.radians(rotation)
        # Front direction: rotation 0 faces +Y.
        fx, fy = -math.sin(theta), math.cos(theta)
        front = (
            position[0] + fx * (depth / 2.0 + gap / 2.0),
            position[1] + fy * (depth / 2.0 + gap / 2.0),
        )
        zone = rect_corners(front[0], front[1], width * 0.8, gap, rotation)

        # The clearance zone may leave the room (an item against an external
        # wall faces inward, so this rarely triggers) but must not be occupied.
        return not any(
            rects_overlap(zone, obstacle.corners) for obstacle in self.obstacles
        )

    # -- scoring -----------------------------------------------------------

    def _score(
        self,
        position: Point,
        rotation: float,
        dimensions: Tuple[float, float, float],
        against_wall: bool,
        wall_affinity: float,
        prefer_near: Optional[Point],
    ) -> float:
        """How good this pose is. Higher is better."""
        score = 0.0

        # Wall affinity is the dominant term for items that want a wall.
        if against_wall:
            score += 4.0 * wall_affinity
        else:
            score += 2.0 * (1.0 - wall_affinity)

        centroid = self.space.centroid
        distance_to_centre = math.dist(position, centroid)

        # Free-standing items belong near the middle; wall items do not.
        if wall_affinity < 0.4:
            score -= 0.8 * distance_to_centre
        else:
            score += 0.15 * distance_to_centre

        # Prefer to keep related items together.
        if prefer_near is not None:
            score -= 0.5 * math.dist(position, prefer_near)

        # Prefer poses that leave the room's circulation intact.
        score += 1.5 * self._circulation_margin(position, rotation, dimensions)

        # Mild preference for tucking into a corner, which is where furniture
        # actually ends up and which keeps the middle of the room walkable.
        if against_wall:
            score += 0.4 * self._corner_affinity(position)

        return score

    def _circulation_margin(
        self,
        position: Point,
        rotation: float,
        dimensions: Tuple[float, float, float],
    ) -> float:
        """How much walkable clearance this pose leaves, 0 to 1.

        Approximated as the distance from the item to the nearest existing
        obstacle, saturating at the minimum circulation width. Cheap, and it
        captures the failure that matters: furniture bunching into a wall of
        stuff with no way through.
        """
        if not self.obstacles:
            return 1.0

        corners = rect_corners(
            position[0], position[1], dimensions[0], dimensions[1], rotation
        )
        nearest = min(
            _rect_distance(corners, obstacle.corners)
            for obstacle in self.obstacles
        )
        return min(1.0, nearest / MIN_CIRCULATION_M)

    def _corner_affinity(self, position: Point) -> float:
        """1 near a corner of the room's bounding box, 0 at its centre."""
        x0, y0 = self.space.bounds_min
        x1, y1 = self.space.bounds_max
        if x1 <= x0 or y1 <= y0:
            return 0.0
        u = abs((position[0] - x0) / (x1 - x0) - 0.5) * 2.0
        v = abs((position[1] - y0) / (y1 - y0) - 0.5) * 2.0
        return min(1.0, (u + v) / 2.0)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
#
# Local copies rather than imports from ``vision.geometry2d``: this package is
# imported by the Blender-side generator, which may only load stdlib-only
# modules, and keeping the dependency surface at zero is what guarantees that.


def rect_corners(
    cx: float, cy: float, width: float, depth: float, rotation_deg: float
) -> List[Point]:
    """The four corners of an oriented rectangle."""
    hw, hd = width / 2.0, depth / 2.0
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return [
        (cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)
        for dx, dy in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd))
    ]


def rects_overlap(a: Sequence[Point], b: Sequence[Point]) -> bool:
    """Separating-axis test for two convex quads."""
    for polygon in (a, b):
        for i in range(len(polygon)):
            x0, y0 = polygon[i]
            x1, y1 = polygon[(i + 1) % len(polygon)]
            axis = (-(y1 - y0), x1 - x0)
            length = math.hypot(*axis)
            if length < 1e-12:
                continue
            axis = (axis[0] / length, axis[1] / length)

            a_min, a_max = _project(a, axis)
            b_min, b_max = _project(b, axis)
            if a_max <= b_min + 1e-9 or b_max <= a_min + 1e-9:
                return False
    return True


def _project(polygon: Sequence[Point], axis: Point) -> Tuple[float, float]:
    values = [p[0] * axis[0] + p[1] * axis[1] for p in polygon]
    return min(values), max(values)


def _rect_distance(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Approximate gap between two rectangles; 0 when they overlap."""
    if rects_overlap(a, b):
        return 0.0
    best = float("inf")
    for p in a:
        for i in range(len(b)):
            best = min(best, _point_segment_distance(p, b[i], b[(i + 1) % len(b)]))
    for p in b:
        for i in range(len(a)):
            best = min(best, _point_segment_distance(p, a[i], a[(i + 1) % len(a)]))
    return best


def _point_segment_distance(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-12:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
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


def shrink_polygon(polygon: Sequence[Point], margin: float) -> List[Point]:
    """Pull a polygon inward toward its centroid by roughly ``margin``.

    Used to keep furniture off the wall faces. A proper straight-skeleton
    offset is unnecessary here: the polygons come from raster segmentation and
    are already approximations, and a centroid-scaled inset is stable on the
    concave L-shapes that offsetting handles badly.
    """
    if len(polygon) < 3 or margin <= 0:
        return list(polygon)

    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)

    out: List[Point] = []
    for x, y in polygon:
        dx, dy = x - cx, y - cy
        distance = math.hypot(dx, dy)
        if distance <= 1e-9:
            out.append((x, y))
            continue
        scale = max(0.0, (distance - margin) / distance)
        out.append((cx + dx * scale, cy + dy * scale))
    return out
