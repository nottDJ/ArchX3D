"""
ArchX3D — Incremental validation
================================
Deterministic re-validation of an edited scene graph, between review and
generation.

Why this stage exists
---------------------
``validate.py`` runs once, during analysis, and never again. That was fine when
the review step could only delete and relabel, but a user who can drag, rotate
and resize can produce a scene that no longer satisfies the checks the pipeline
enforced — and nothing would notice before the render. This module closes that
gap without re-running any model:

    analysis → validation → review → *incremental validation* → generation

Nothing here calls a VLM or touches the network. Every check is a geometric
predicate over the graph, so a re-check is cheap enough to run on every edit.

Reporting, not overruling
-------------------------
The default is **report-only**: the graph is not mutated at all. A user who
deliberately pushed two chairs together does not want that silently undone on
the way to the renderer, and the previous stage already told them what they
did. Correction is opt-in via ``apply_corrections``, and even then a locked
object is never moved and a hand-edited one is only moved when the caller
explicitly asks with ``respect_user_edits=False``.

The existing checks are reused rather than reimplemented: they run against a
deep copy, and the difference between the copy and the original *is* the set of
proposed corrections. That keeps one implementation of "what is a legal
placement" instead of two that can drift apart.

Three checks live here rather than in ``validate.py`` because they concern
*habitability* rather than physical possibility — a scene can be perfectly
well-formed and still have a wardrobe blocking the only door.

Stdlib only.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import catalog, geometry2d as g2, validate
from .grounding import RoomFrame
from .schema import Opening, Room, SceneGraph, SceneObject

#: Gap below which two pieces of furniture cannot be walked between, in metres.
#: Roughly a person's shoulder width plus clothing — narrower than this and the
#: arrangement reads as a barrier rather than a route.
MIN_CIRCULATION = 0.60

#: Depth of the keep-clear zone projected into the room from a door, in metres.
DOOR_CLEARANCE = 0.90

#: Cell size for the reachability raster, in metres.
ACCESS_CELL = 0.10

#: Share of a room's free floor that must be reachable from its doors.
MIN_REACHABLE_SHARE = 0.60

#: Footprint below which an object is not treated as an obstacle for spacing —
#: a vase does not divide a room.
OBSTACLE_FOOTPRINT = 0.15

#: Categories a person walks straight over.
WALKABLE = frozenset({"rug", "carpet"})

#: Height below which an object is stepped over rather than walked around.
STEP_OVER_HEIGHT = 0.12


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class RecheckIssue:
    """One finding, optionally carrying the correction that would fix it."""

    kind: str
    severity: str  # error | warning
    subject: str
    detail: str
    room_id: str = ""
    target: str = ""
    #: Present when automatic correction could resolve this, and describes what
    #: ``apply_corrections`` would do. Absent means the user must decide.
    suggestion: Optional[Dict[str, Any]] = None
    applied: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
            "room_id": self.room_id,
            "applied": self.applied,
        }
        if self.target:
            out["target"] = self.target
        if self.suggestion is not None:
            out["suggestion"] = self.suggestion
        return out


@dataclass
class RecheckReport:
    issues: List[RecheckIssue] = field(default_factory=list)
    #: Ids whose proposed correction was withheld because the user owns them.
    protected: List[str] = field(default_factory=list)

    def add(self, issue: RecheckIssue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> List[RecheckIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> Dict[str, Any]:
        by_kind: Dict[str, int] = {}
        for issue in self.issues:
            by_kind[issue.kind] = by_kind.get(issue.kind, 0) + 1
        return {
            "total_issues": len(self.issues),
            "errors": len(self.errors),
            "warnings": sum(1 for i in self.issues if i.severity == "warning"),
            "applied": sum(1 for i in self.issues if i.applied),
            "correctable": sum(1 for i in self.issues if i.suggestion is not None),
            "by_kind": by_kind,
            "protected_objects": list(self.protected),
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        if not self.issues:
            return "no problems found"
        return (
            f"{len(self.errors)} error(s), "
            f"{len(self.issues) - len(self.errors)} warning(s)"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def recheck(
    graph: SceneGraph,
    *,
    apply_corrections: bool = False,
    respect_user_edits: bool = True,
) -> RecheckReport:
    """Re-validate an edited graph.

    With ``apply_corrections`` false (the default) the graph is not modified;
    the report describes what is wrong and, where a fix is unambiguous, what
    would be done about it.

    With it true, corrections are written back except to objects the user owns:
    anything locked, and — unless ``respect_user_edits`` is false — anything
    carrying a ``*_set_by_user`` flag. Those are listed in ``protected``.
    """
    report = RecheckReport()
    protected = _protected_ids(graph, respect_user_edits)

    for room in graph.rooms:
        objects = [o for o in graph.objects if o.room_id == room.id]
        lights = [x for x in graph.lights if x.room_id == room.id]
        if not objects and not lights:
            continue

        frame = _frame_for(room, graph)

        _replay_core_checks(graph, room, objects, lights, frame, protected, report,
                            apply_corrections)
        _check_circulation(room, objects, report)
        _check_door_clearance(room, objects, _openings_for(room, graph), report)
        _check_reachability(room, objects, _openings_for(room, graph), report)

    report.protected = sorted(set(report.protected))
    return report


def _protected_ids(graph: SceneGraph, respect_user_edits: bool) -> Set[str]:
    """Objects automatic correction must not move."""
    protected = {o.id for o in graph.objects if o.locked}
    if respect_user_edits:
        protected |= {
            o.id for o in graph.objects
            if any(flag.endswith("_set_by_user") for flag in o.flags)
        }
    return protected


def _frame_for(room: Room, graph: SceneGraph) -> RoomFrame:
    """Rebuild the room frame from the graph itself.

    After the review step the DXF regions are long gone, but everything the
    frame needs was carried into the scene graph, so it can be reconstructed
    rather than recomputed from the drawing.
    """
    wall_ids = set(room.wall_ids)
    walls = [w for w in graph.walls if w.id in wall_ids] or list(graph.walls)
    return RoomFrame(
        polygon=list(room.polygon),
        bounds_min=room.bounds_min,
        bounds_max=room.bounds_max,
        ceiling_height=room.ceiling_height,
        walls=walls,
    )


# ---------------------------------------------------------------------------
# Replaying the analysis-time checks
# ---------------------------------------------------------------------------


def _replay_core_checks(
    graph: SceneGraph,
    room: Room,
    objects: List[SceneObject],
    lights: List,
    frame: RoomFrame,
    protected: Set[str],
    report: RecheckReport,
    apply_corrections: bool,
) -> None:
    """Run ``validate_and_correct`` on a copy and report the difference.

    Running the real checks against a throwaway graph is what lets this module
    stay in step with ``validate.py`` for free: whatever that module considers
    a problem, this one reports, including checks added to it later.
    """
    scoped = SceneGraph(
        rooms=[room],
        walls=frame.walls,
        objects=copy.deepcopy(objects),
        lights=copy.deepcopy(lights),
    )
    result = validate.validate_and_correct(scoped, frame)

    before = {o.id: o for o in objects}

    # Diff every object, not only the ones named as an issue subject. Collision
    # resolution moves *both* halves of a colliding pair while reporting a
    # single issue against one of them, so subject-only diffing would silently
    # drop the partner's correction — and with it, the partner's protection.
    corrections: Dict[str, Dict[str, Any]] = {}
    for original in objects:
        corrected = scoped.object_by_id(original.id)
        if corrected is None:
            continue
        patch = _difference(original, corrected)
        if patch is not None:
            corrections[original.id] = patch

    applied: Set[str] = set()
    if apply_corrections:
        for object_id, patch in corrections.items():
            if object_id in protected:
                continue
            _apply(before[object_id], patch)
            applied.add(object_id)

    explained: Set[str] = set()
    for issue in result.issues:
        explained.add(issue.subject)
        report.add(RecheckIssue(
            kind=issue.kind,
            severity=issue.severity,
            subject=issue.subject,
            detail=issue.detail,
            room_id=room.id,
            target=issue.target,
            suggestion=corrections.get(issue.subject),
            applied=issue.subject in applied,
        ))

    # An object moved as the far side of someone else's collision has no issue
    # of its own. Reporting it keeps the correction visible instead of it
    # appearing as an unexplained shift between review and render.
    for object_id, patch in corrections.items():
        if object_id in explained:
            continue
        obj = before[object_id]
        report.add(RecheckIssue(
            kind="displaced",
            severity="warning",
            subject=object_id,
            room_id=room.id,
            detail=(
                f"{obj.category} would move to make room for a neighbour it "
                f"conflicts with"
            ),
            suggestion=patch,
            applied=object_id in applied,
        ))

    for object_id in corrections:
        if object_id in protected:
            report.protected.append(object_id)


def _difference(original: SceneObject, corrected: SceneObject) -> Optional[Dict[str, Any]]:
    """What would have to change on ``original`` to match ``corrected``."""
    patch: Dict[str, Any] = {}

    if (
        abs(original.position.x - corrected.position.x) > 1e-6
        or abs(original.position.y - corrected.position.y) > 1e-6
        or abs(original.position.z - corrected.position.z) > 1e-6
    ):
        patch["position"] = {
            "x": round(corrected.position.x, 4),
            "y": round(corrected.position.y, 4),
            "z": round(corrected.position.z, 4),
        }

    if abs(original.rotation_z - corrected.rotation_z) > 1e-6:
        patch["rotation_z"] = round(corrected.rotation_z, 3)

    for axis in ("width", "depth", "height"):
        if abs(getattr(original.dimensions, axis) - getattr(corrected.dimensions, axis)) > 1e-6:
            patch.setdefault("dimensions", {})[axis] = round(
                getattr(corrected.dimensions, axis), 4
            )

    return patch or None


def _apply(obj: SceneObject, patch: Dict[str, Any]) -> None:
    from .schema import Vec3

    if "position" in patch:
        p = patch["position"]
        obj.position = Vec3(p["x"], p["y"], p.get("z", obj.position.z))
    if "rotation_z" in patch:
        obj.rotation_z = patch["rotation_z"]
    for axis, value in (patch.get("dimensions") or {}).items():
        setattr(obj.dimensions, axis, value)
    obj.flags.append("corrected_by_recheck")


# ---------------------------------------------------------------------------
# Habitability checks
# ---------------------------------------------------------------------------


def _obstacles(objects: Iterable[SceneObject]) -> List[SceneObject]:
    """Floor-standing objects a person has to walk around."""
    return [
        o for o in objects
        if o.support == "floor"
        and o.category not in WALKABLE
        and o.dimensions.height > STEP_OVER_HEIGHT
        and not o.dimensions.is_degenerate()
    ]


def _check_circulation(
    room: Room, objects: Sequence[SceneObject], report: RecheckReport
) -> None:
    """Flag gaps too narrow to walk through.

    Only genuine gaps count: objects that touch are a deliberate arrangement
    (a nightstand against a bed), and objects far apart are fine. The failure
    being caught is the in-between case that looks passable in plan and is not.
    """
    obstacles = [
        o for o in _obstacles(objects)
        if o.dimensions.footprint_area >= OBSTACLE_FOOTPRINT
    ]

    for i, a in enumerate(obstacles):
        for b in obstacles[i + 1:]:
            corners_a = a.footprint_corners()
            corners_b = b.footprint_corners()
            if g2.rect_overlap(corners_a, corners_b) > 0:
                continue  # touching or overlapping; not a route

            gap = _rect_gap(corners_a, corners_b)
            if 1e-6 < gap < MIN_CIRCULATION:
                report.add(RecheckIssue(
                    kind="tight_circulation",
                    severity="warning",
                    subject=a.id,
                    target=b.id,
                    room_id=room.id,
                    detail=(
                        f"{gap:.2f} m between {a.category} and {b.category}; "
                        f"under the {MIN_CIRCULATION:.2f} m needed to walk through"
                    ),
                ))


def _rect_gap(a: Sequence[Tuple[float, float]], b: Sequence[Tuple[float, float]]) -> float:
    """Shortest distance between two convex footprints."""
    best = float("inf")
    for corners, other in ((a, b), (b, a)):
        for point in corners:
            for i in range(len(other)):
                _, distance = g2.closest_point_on_segment(
                    point, other[i], other[(i + 1) % len(other)]
                )
                best = min(best, distance)
    return best


def _check_door_clearance(
    room: Room,
    objects: Sequence[SceneObject],
    openings: Sequence[Opening],
    report: RecheckReport,
) -> None:
    """Flag anything parked in a doorway.

    The keep-clear zone is projected from the opening toward the room's
    interior, which avoids needing to know which way the door swings — the
    approach side is the side inside this room either way.
    """
    doors = [o for o in openings if o.kind in ("door", "archway")]
    if not doors:
        return

    interior = g2.polygon_centroid(room.polygon) if room.polygon else None
    if interior is None:
        return

    for door in doors:
        origin = (door.position.x, door.position.y)
        heading = math.degrees(
            math.atan2(interior[1] - origin[1], interior[0] - origin[0])
        )
        # A rectangle standing on the opening, running into the room.
        centre = (
            origin[0] + math.cos(math.radians(heading)) * DOOR_CLEARANCE / 2.0,
            origin[1] + math.sin(math.radians(heading)) * DOOR_CLEARANCE / 2.0,
        )
        zone = g2.rect_corners(
            centre[0], centre[1], max(door.width, 0.6), DOOR_CLEARANCE, heading - 90.0
        )

        for obj in _obstacles(objects):
            penetration = g2.rect_overlap(zone, obj.footprint_corners())
            if penetration > validate.OVERLAP_TOLERANCE:
                report.add(RecheckIssue(
                    kind="blocked_door",
                    severity="error",
                    subject=obj.id,
                    target=door.id,
                    room_id=room.id,
                    detail=(
                        f"{obj.category} intrudes {penetration:.2f} m into the "
                        f"clearance in front of {door.kind} {door.id}"
                    ),
                ))


def _check_reachability(
    room: Room,
    objects: Sequence[SceneObject],
    openings: Sequence[Opening],
    report: RecheckReport,
    ) -> None:
    """Flag furniture arrangements that wall off part of the room.

    Rasterises the free floor and flood-fills from the doorways, which is the
    same technique room segmentation uses on walls. A grid is used rather than
    a visibility graph because it degrades gracefully: a slightly wrong cell
    costs 10 cm of accuracy instead of a wrong answer.
    """
    if not room.polygon:
        return

    min_x, min_y = room.bounds_min
    max_x, max_y = room.bounds_max
    columns = int((max_x - min_x) / ACCESS_CELL) + 1
    rows = int((max_y - min_y) / ACCESS_CELL) + 1
    if columns < 3 or rows < 3 or columns * rows > 500_000:
        return  # degenerate or implausibly large; not worth a verdict

    def centre(col: int, row: int) -> Tuple[float, float]:
        return (min_x + (col + 0.5) * ACCESS_CELL, min_y + (row + 0.5) * ACCESS_CELL)

    free = [[False] * columns for _ in range(rows)]
    for row in range(rows):
        for col in range(columns):
            free[row][col] = g2.point_in_polygon(centre(col, row), room.polygon)

    blocked_footprints = [o.footprint_corners() for o in _obstacles(objects)]
    for corners in blocked_footprints:
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        for row in range(
            max(0, int((min(ys) - min_y) / ACCESS_CELL)),
            min(rows, int((max(ys) - min_y) / ACCESS_CELL) + 2),
        ):
            for col in range(
                max(0, int((min(xs) - min_x) / ACCESS_CELL)),
                min(columns, int((max(xs) - min_x) / ACCESS_CELL) + 2),
            ):
                if free[row][col] and g2.point_in_polygon(centre(col, row), corners):
                    free[row][col] = False

    total_free = sum(sum(1 for cell in line if cell) for line in free)
    if total_free == 0:
        report.add(RecheckIssue(
            kind="room_full", severity="error", subject=room.id, room_id=room.id,
            detail="furniture covers the entire floor; nothing can move through the room",
        ))
        return

    seeds = _seed_cells(room, openings, free, min_x, min_y, columns, rows, centre)
    if not seeds:
        return  # no door recorded for this room; nothing to be reachable from

    reached = _flood(free, seeds, columns, rows)
    share = reached / total_free
    if share < MIN_REACHABLE_SHARE:
        report.add(RecheckIssue(
            kind="unreachable_floor",
            severity="warning",
            subject=room.id,
            room_id=room.id,
            detail=(
                f"only {share:.0%} of the free floor can be reached from the "
                f"doorway; furniture is dividing the room"
            ),
        ))


def _seed_cells(room, openings, free, min_x, min_y, columns, rows, centre) -> List[Tuple[int, int]]:
    """Free cells just inside each doorway."""
    doors = [o for o in openings if o.kind in ("door", "archway")]
    if not doors:
        return []

    interior = g2.polygon_centroid(room.polygon)
    seeds: List[Tuple[int, int]] = []
    for door in doors:
        # Step inward from the opening until a free cell is found.
        dx = interior[0] - door.position.x
        dy = interior[1] - door.position.y
        length = math.hypot(dx, dy) or 1.0
        for step in range(1, 12):
            probe = (
                door.position.x + dx / length * ACCESS_CELL * step,
                door.position.y + dy / length * ACCESS_CELL * step,
            )
            col = int((probe[0] - min_x) / ACCESS_CELL)
            row = int((probe[1] - min_y) / ACCESS_CELL)
            if 0 <= col < columns and 0 <= row < rows and free[row][col]:
                seeds.append((col, row))
                break
    return seeds


def _flood(free, seeds: List[Tuple[int, int]], columns: int, rows: int) -> int:
    """Four-connected flood fill; returns how many free cells were reached."""
    seen = [[False] * columns for _ in range(rows)]
    stack = list(seeds)
    count = 0

    while stack:
        col, row = stack.pop()
        if not (0 <= col < columns and 0 <= row < rows):
            continue
        if seen[row][col] or not free[row][col]:
            continue
        seen[row][col] = True
        count += 1
        stack.extend(((col + 1, row), (col - 1, row), (col, row + 1), (col, row - 1)))

    return count


def _openings_for(room: Room, graph: SceneGraph) -> List[Opening]:
    return [o for o in graph.openings if o.room_id == room.id]
