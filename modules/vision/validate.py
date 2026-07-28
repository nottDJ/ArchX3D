"""
ArchX3D — Scene validation
==========================
Physical plausibility checks run immediately before export.

The vision and placement stages each make reasonable local decisions that can
still combine into something impossible: two chairs in the same place, a lamp
hovering 40 cm above the table it belongs on, a wardrobe half inside a wall.
This module finds those and, where the fix is unambiguous, applies it.

Correction policy
-----------------
Only *minor* problems are auto-corrected — nudging objects apart, dropping a
floating object onto its support, pulling something back inside the room. Where
a fix would require inventing information (an object far larger than the room
that contains it), the object is flagged and withheld from the build instead of
being silently reshaped into something the reference image never showed.

Overlap is judged in 3D, not plan: a rug under a coffee table and a lamp on a
side table both overlap in plan and both are correct. Two objects only conflict
when their footprints *and* their height ranges intersect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog, geometry2d as g2
from .grounding import RoomFrame
from .schema import SceneGraph, SceneObject, Vec3

#: Footprint penetration below this is treated as touching, not colliding.
OVERLAP_TOLERANCE = 0.02

#: Height above the floor at which a "floor" object counts as floating.
FLOAT_TOLERANCE = 0.02

#: How many relaxation passes to run when separating collisions.
MAX_RESOLUTION_PASSES = 6

#: An object whose footprint exceeds this share of the room is implausible.
MAX_FOOTPRINT_SHARE = 0.55


@dataclass
class Issue:
    """One validation finding."""

    kind: str
    severity: str  # error | warning
    subject: str
    detail: str
    corrected: bool = False
    target: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "subject": self.subject,
            "target": self.target,
            "detail": self.detail,
            "corrected": self.corrected,
        }


@dataclass
class ValidationReport:
    issues: List[Issue] = field(default_factory=list)
    #: Ids withheld from the build because they could not be made plausible.
    withheld: List[str] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def to_dict(self) -> Dict[str, object]:
        by_kind: Dict[str, int] = {}
        for issue in self.issues:
            by_kind[issue.kind] = by_kind.get(issue.kind, 0) + 1
        return {
            "total_issues": len(self.issues),
            "corrected": sum(1 for i in self.issues if i.corrected),
            "uncorrected": sum(1 for i in self.issues if not i.corrected),
            "by_kind": by_kind,
            "withheld_objects": list(self.withheld),
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_and_correct(graph: SceneGraph, room: RoomFrame) -> ValidationReport:
    """Run every check, applying safe corrections in place."""
    report = ValidationReport()
    objects = graph.objects

    _check_scale(objects, room, report)
    _check_containment(objects, room, report)
    _check_support_heights(objects, report)
    _check_wall_intersections(objects, room, report)
    _resolve_collisions(objects, room, report)
    _check_ceiling_clearance(objects, room, report)
    _check_lights(graph, room, report)

    # Anything still implausible is withheld rather than built wrong.
    for obj in objects:
        if any(
            issue.subject == obj.id and issue.severity == "error" and not issue.corrected
            for issue in report.issues
        ):
            obj.uncertain = True
            obj.flags.append("withheld_failed_validation")
            report.withheld.append(obj.id)

    return report


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_scale(objects: Sequence[SceneObject], room: RoomFrame, report: ValidationReport) -> None:
    """Reject objects that cannot physically fit the room."""
    room_area = max(1e-6, room.width * room.depth)

    for obj in objects:
        if obj.dimensions.is_degenerate():
            report.add(Issue("degenerate_dimensions", "error", obj.id,
                             f"dimensions {obj.dimensions.to_dict()} are zero or negative"))
            continue

        share = obj.dimensions.footprint_area / room_area
        if share > MAX_FOOTPRINT_SHARE:
            report.add(Issue(
                "implausible_scale", "error", obj.id,
                f"footprint {obj.dimensions.footprint_area:.2f} m2 is "
                f"{share:.0%} of the {room_area:.1f} m2 room",
            ))

        if obj.dimensions.height > room.ceiling_height:
            # This one is safely correctable: clip to the ceiling.
            obj.dimensions.height = room.ceiling_height - 0.05
            report.add(Issue("taller_than_room", "warning", obj.id,
                             "height clipped to ceiling", corrected=True))


def _check_containment(objects: Sequence[SceneObject], room: RoomFrame, report: ValidationReport) -> None:
    """Pull objects whose centre escaped the room back inside."""
    for obj in objects:
        if obj.support in ("wall", "ceiling", "on_object"):
            continue

        point = (obj.position.x, obj.position.y)
        if room.contains(point):
            continue

        margin = max(obj.dimensions.width, obj.dimensions.depth) / 2.0
        fixed = room.clamp_inside(point, margin=margin)
        obj.position = Vec3(fixed[0], fixed[1], obj.position.z)
        obj.flags.append("clamped_into_room")
        report.add(Issue("outside_room", "warning", obj.id,
                         f"centre moved {math.dist(point, fixed):.2f} m to re-enter the room",
                         corrected=True))


def _check_support_heights(objects: Sequence[SceneObject], report: ValidationReport) -> None:
    """Detect and fix floating or sunken objects."""
    index = {obj.id: obj for obj in objects}

    for obj in objects:
        if obj.support == "floor":
            if abs(obj.position.z) > FLOAT_TOLERANCE:
                detail = (
                    f"floor object sat at z={obj.position.z:.3f} m"
                    if obj.position.z > 0
                    else f"floor object sank to z={obj.position.z:.3f} m"
                )
                obj.position = Vec3(obj.position.x, obj.position.y, 0.0)
                obj.flags.append("dropped_to_floor")
                report.add(Issue("floating_object", "warning", obj.id, detail, corrected=True))

        elif obj.support == "on_object":
            support = index.get(obj.support_id)
            if support is None:
                report.add(Issue("missing_support", "warning", obj.id,
                                 f"support {obj.support_id!r} not in scene; dropped to floor"))
                obj.support = "floor"
                obj.position = Vec3(obj.position.x, obj.position.y, 0.0)
                continue

            expected = _surface_height(support)
            if abs(obj.position.z - expected) > 0.03:
                report.add(Issue(
                    "misaligned_support", "warning", obj.id,
                    f"z={obj.position.z:.3f} m did not match {support.id} surface "
                    f"at {expected:.3f} m",
                    corrected=True, target=support.id,
                ))
                obj.position = Vec3(obj.position.x, obj.position.y, expected)

        elif obj.support in ("wall", "ceiling"):
            if obj.position.z < 0:
                obj.position = Vec3(obj.position.x, obj.position.y, 0.0)
                report.add(Issue("below_floor", "warning", obj.id,
                                 "mounted object was below floor level", corrected=True))


def _check_wall_intersections(
    objects: Sequence[SceneObject], room: RoomFrame, report: ValidationReport
) -> None:
    """Push floor objects out of the walls they intersect."""
    if not room.walls:
        return

    for obj in objects:
        if obj.support != "floor" or obj.dimensions.is_degenerate():
            continue
        # Rugs and other near-flat items may legitimately run under a wall line.
        if obj.dimensions.height < 0.06:
            continue

        corners = obj.footprint_corners()
        offending = _first_wall_crossing(corners, room)
        if offending is None:
            continue

        wall, _ = offending
        heading = math.radians(wall.inward_normal_deg(room.center))
        contact, _ = g2.closest_point_on_segment(
            (obj.position.x, obj.position.y), wall.start, wall.end
        )
        clearance = obj.dimensions.depth / 2.0 + wall.thickness / 2.0 + 0.02

        obj.position = Vec3(
            contact[0] - math.sin(heading) * clearance,
            contact[1] + math.cos(heading) * clearance,
            obj.position.z,
        )
        obj.flags.append(f"pushed_off_{wall.id}")
        report.add(Issue("wall_intersection", "warning", obj.id,
                         f"footprint crossed {wall.id}; pushed clear",
                         corrected=True, target=wall.id))


def _first_wall_crossing(corners: Sequence[Tuple[float, float]], room: RoomFrame):
    """Return the first wall an object's footprint edge actually crosses."""
    for wall in room.walls:
        for i in range(len(corners)):
            a = corners[i]
            b = corners[(i + 1) % len(corners)]
            if g2.segments_intersect(a, b, wall.start, wall.end):
                return wall, (a, b)
    return None


def _resolve_collisions(
    objects: List[SceneObject], room: RoomFrame, report: ValidationReport
) -> None:
    """Separate objects that occupy the same space.

    Runs a few relaxation passes. The less confident object of a colliding pair
    absorbs most of the movement, so a firmly-observed sofa stays put and an
    uncertain side table shifts around it.
    """
    candidates = [
        obj for obj in objects
        if obj.support in ("floor", "on_object") and not obj.dimensions.is_degenerate()
    ]
    index = {obj.id: obj for obj in objects}
    reported: set = set()

    for _ in range(MAX_RESOLUTION_PASSES):
        moved = False

        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]

                if _is_support_pair(a, b) or not _vertical_overlap(a, b):
                    continue

                corners_a = a.footprint_corners()
                corners_b = b.footprint_corners()
                penetration = g2.rect_overlap(corners_a, corners_b)
                if penetration <= OVERLAP_TOLERANCE:
                    continue

                axis = g2.separation_axis(corners_a, corners_b)
                if axis is None:
                    continue

                # Share the correction inversely to confidence, except that a
                # locked object never moves — the user placed it deliberately,
                # so its partner absorbs the whole correction.
                if a.locked and b.locked:
                    continue
                if a.locked:
                    share_a, share_b = 0.0, 1.0
                elif b.locked:
                    share_a, share_b = 1.0, 0.0
                else:
                    total = max(1e-3, a.confidence + b.confidence)
                    share_b = a.confidence / total
                    share_a = 1.0 - share_b
                push = penetration + 0.01

                _shift(a, (-axis[0] * push * share_a, -axis[1] * push * share_a), room)
                _shift(b, (axis[0] * push * share_b, axis[1] * push * share_b), room)
                moved = True

                key = tuple(sorted((a.id, b.id)))
                if key not in reported:
                    reported.add(key)
                    report.add(Issue(
                        "overlap", "warning", a.id,
                        f"overlapped {b.id} by {penetration:.3f} m; separated",
                        corrected=True, target=b.id,
                    ))

        if not moved:
            break

    # Anything still interpenetrating after relaxation is a real problem.
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            if _is_support_pair(a, b) or not _vertical_overlap(a, b):
                continue
            penetration = g2.rect_overlap(a.footprint_corners(), b.footprint_corners())
            if penetration > OVERLAP_TOLERANCE * 4:
                report.add(Issue(
                    "unresolved_overlap", "warning", a.id,
                    f"still overlaps {b.id} by {penetration:.3f} m after "
                    f"{MAX_RESOLUTION_PASSES} passes",
                    target=b.id,
                ))

    # Children must follow the surfaces they sit on.
    for obj in objects:
        if obj.support == "on_object" and obj.support_id in index:
            support = index[obj.support_id]
            obj.position = Vec3(obj.position.x, obj.position.y, _surface_height(support))


def _shift(obj: SceneObject, delta: Tuple[float, float], room: RoomFrame) -> None:
    if obj.locked:
        return
    target = (obj.position.x + delta[0], obj.position.y + delta[1])
    if obj.support == "floor":
        margin = max(obj.dimensions.width, obj.dimensions.depth) / 2.0
        target = room.clamp_inside(target, margin=margin)
    obj.position = Vec3(target[0], target[1], obj.position.z)


def _is_support_pair(a: SceneObject, b: SceneObject) -> bool:
    """True when one object rests on the other, so overlap is expected."""
    return a.support_id == b.id or b.support_id == a.id


def _vertical_overlap(a: SceneObject, b: SceneObject) -> bool:
    """Do the two objects' height ranges intersect?

    This is what allows a rug under a table, or a vase on a shelf, without
    reporting a collision.
    """
    a_low, a_high = a.position.z, a.position.z + a.dimensions.height
    b_low, b_high = b.position.z, b.position.z + b.dimensions.height
    return min(a_high, b_high) - max(a_low, b_low) > 0.02


def _check_ceiling_clearance(
    objects: Sequence[SceneObject], room: RoomFrame, report: ValidationReport
) -> None:
    for obj in objects:
        top = obj.position.z + obj.dimensions.height
        if top > room.ceiling_height + 0.01:
            excess = top - room.ceiling_height
            if obj.support == "floor" and excess < obj.dimensions.height * 0.4:
                obj.dimensions.height -= excess + 0.02
                report.add(Issue("through_ceiling", "warning", obj.id,
                                 f"trimmed {excess:.2f} m to clear the ceiling", corrected=True))
            else:
                report.add(Issue("through_ceiling", "error", obj.id,
                                 f"extends {excess:.2f} m above the ceiling"))


def _check_lights(graph: SceneGraph, room: RoomFrame, report: ValidationReport) -> None:
    """Keep luminaires inside the room volume and physically sane."""
    for light in graph.lights:
        point = (light.position.x, light.position.y)
        if not room.contains(point):
            fixed = room.clamp_inside(point, margin=0.15)
            light.position = Vec3(fixed[0], fixed[1], light.position.z)
            report.add(Issue("light_outside_room", "warning", light.id,
                             "moved back inside the room", corrected=True))

        if light.position.z > room.ceiling_height:
            light.position = Vec3(light.position.x, light.position.y, room.ceiling_height - 0.05)
            report.add(Issue("light_above_ceiling", "warning", light.id,
                             "lowered below the ceiling", corrected=True))

        if light.power_w < 0:
            light.power_w = 0.0
            report.add(Issue("negative_power", "warning", light.id,
                             "clamped to zero", corrected=True))

    if not graph.buildable_lights():
        report.add(Issue("no_lighting", "warning", "scene",
                         "no confident light sources were detected; "
                         "the generator will fall back to default lighting"))


def _surface_height(target: SceneObject) -> float:
    prior = catalog.get_prior(target.category)
    if prior is not None and prior.surface_height > 0 and prior.typical[2] > 0:
        ratio = target.dimensions.height / prior.typical[2]
        return target.position.z + prior.surface_height * ratio
    return target.position.z + target.dimensions.height
