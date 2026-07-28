"""
ArchX3D — Relationship inference and constraint solving
=======================================================
Objects are not placed independently. This module turns the observed (and
inferred) semantic relationships into actual transforms.

Two jobs:

1. **Inference.** Relationships the model stated are taken at face value.
   Where it stayed silent, `catalog.IMPLIED_RELATIONSHIPS` supplies the
   arrangements that are near-universal in real interiors — chairs surround the
   dining table, bedside tables flank the bed. Inferred relationships are
   recorded at reduced confidence and clearly marked, so they never masquerade
   as observations.

2. **Solving.** Constraints are applied in dependency order: anchors (large
   floor furniture) keep their grounded positions, and dependants are moved
   onto them. Applying them in arbitrary order lets a later constraint undo an
   earlier one, which is what produces chairs standing inside tables.

Each relationship records whether it was actually satisfied, so the diagnostics
can report what the solver could not honour rather than quietly dropping it.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog, geometry2d as g2
from .grounding import RoomFrame
from .observe import RelationObservation
from .schema import Relationship, SceneObject, Vec3

#: Confidence ceiling for a relationship nobody observed, only inferred.
IMPLIED_CONFIDENCE = 0.45

#: Gap left between an object and the thing it sits beside, metres.
BESIDE_GAP = 0.06

#: How far dining chairs tuck under the table edge, metres.
CHAIR_TUCK = 0.10

#: Predicates that fully determine a subject's position. An object can only
#: rest on one surface or sit under one thing, so at most one of these may
#: apply per subject — otherwise the last one solved silently wins and the
#: earlier (often more confident) constraint is lost.
EXCLUSIVE_PREDICATES = frozenset(
    {"on_top_of", "centered_under", "under", "above", "beside", "surrounds"}
)

#: Order in which predicates are solved. Positional constraints must settle
#: before orientation ones, or objects end up facing where they no longer are.
PREDICATE_ORDER = (
    "against_wall",
    "mounted_on",
    "centered_under",
    "surrounds",
    "beside",
    "on_top_of",
    "under",
    "above",
    "faces",
)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def infer_relationships(
    observed: Sequence[RelationObservation], objects: Sequence[SceneObject]
) -> List[Relationship]:
    """Combine observed relationships with catalog-implied ones."""
    by_category: Dict[str, List[SceneObject]] = {}
    for obj in objects:
        by_category.setdefault(obj.category, []).append(obj)

    known_ids = {obj.id for obj in objects}
    relationships: List[Relationship] = []
    seen: set = set()

    # Observed relationships take precedence.
    for observation in observed:
        if observation.subject not in known_ids or observation.object not in known_ids:
            continue
        key = (observation.subject, observation.predicate, observation.object)
        if key in seen:
            continue
        seen.add(key)
        relationships.append(
            Relationship(
                subject=observation.subject,
                predicate=observation.predicate,
                object=observation.object,
                confidence=observation.confidence,
            )
        )

    # Fill the gaps with catalog priors.
    for subject_category, predicate, object_category in catalog.IMPLIED_RELATIONSHIPS:
        subjects = by_category.get(subject_category, [])
        targets = by_category.get(object_category, [])
        if not subjects or not targets:
            continue

        for subject in subjects:
            # Never override an explicit statement about this subject.
            if any(r.subject == subject.id and r.predicate == predicate for r in relationships):
                continue
            target = _nearest(subject, targets)
            if target is None or target.id == subject.id:
                continue
            key = (subject.id, predicate, target.id)
            if key in seen:
                continue
            seen.add(key)
            relationships.append(
                Relationship(
                    subject=subject.id,
                    predicate=predicate,
                    object=target.id,
                    confidence=IMPLIED_CONFIDENCE,
                )
            )

    return _drop_conflicting(relationships)


def _drop_conflicting(relationships: List[Relationship]) -> List[Relationship]:
    """Keep at most one position-defining constraint per subject.

    The model happily reports both "bowl on the coffee table" and "bowl on the
    side table" when it sees similar bowls, and a rug can pick up ``under``,
    ``centered_under`` and a second ``under`` for good measure. Solving all of
    them just applies whichever runs last. The most confident claim wins; the
    rest are discarded rather than left to fight.
    """
    best: Dict[str, Relationship] = {}
    kept: List[Relationship] = []

    for relation in relationships:
        if relation.predicate not in EXCLUSIVE_PREDICATES:
            kept.append(relation)
            continue
        incumbent = best.get(relation.subject)
        if incumbent is None or relation.confidence > incumbent.confidence:
            best[relation.subject] = relation

    kept.extend(best.values())
    # Stable ordering keeps runs reproducible.
    kept.sort(key=lambda r: (r.subject, r.predicate, r.object))
    return kept


def _nearest(subject: SceneObject, candidates: Sequence[SceneObject]) -> Optional[SceneObject]:
    best, best_distance = None, float("inf")
    for candidate in candidates:
        if candidate.id == subject.id:
            continue
        distance = subject.position.planar_distance_to(candidate.position)
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


def apply_relationships(
    objects: List[SceneObject], relationships: List[Relationship], room: RoomFrame
) -> Dict[str, int]:
    """Move and rotate objects so the relationships hold.

    Returns a count of applied constraints by predicate, for diagnostics.
    """
    index = {obj.id: obj for obj in objects}
    applied: Dict[str, int] = {}

    # Group "surrounds" so all chairs around one table are solved together.
    surrounds: Dict[str, List[Relationship]] = {}
    for relation in relationships:
        if relation.predicate == "surrounds":
            surrounds.setdefault(relation.object, []).append(relation)

    for predicate in PREDICATE_ORDER:
        for relation in relationships:
            if relation.predicate != predicate:
                continue

            subject = index.get(relation.subject)
            target = index.get(relation.object)
            if subject is None:
                continue

            if predicate == "surrounds":
                continue  # handled below, as a group

            if target is None:
                continue

            if _apply_one(predicate, subject, target, room):
                relation.satisfied = True
                applied[predicate] = applied.get(predicate, 0) + 1

        if predicate == "surrounds":
            for target_id, group in surrounds.items():
                target = index.get(target_id)
                if target is None:
                    continue
                members = [index[r.subject] for r in group if r.subject in index]
                if not members:
                    continue
                _arrange_around(members, target, room)
                for relation in group:
                    relation.satisfied = True
                applied["surrounds"] = applied.get("surrounds", 0) + len(members)

    _place_orphan_supported(objects, room)
    return applied


def reapply_orientation(
    objects: List[SceneObject], relationships: List[Relationship]
) -> int:
    """Re-solve ``faces`` after positions have settled.

    Validation moves objects to resolve collisions, which invalidates any
    rotation computed beforehand — a sofa can end up rotated toward where the
    TV *used* to be. Re-running orientation last is cheap and keeps the two
    consistent. Positions are untouched, so this cannot reintroduce overlaps.
    """
    index = {obj.id: obj for obj in objects}
    fixed = 0

    for relation in relationships:
        if relation.predicate != "faces":
            continue
        subject = index.get(relation.subject)
        target = index.get(relation.object)
        if subject is None or target is None:
            continue
        if _face(subject, target):
            relation.satisfied = True
            fixed += 1

    return fixed


def _apply_one(
    predicate: str, subject: SceneObject, target: SceneObject, room: RoomFrame
) -> bool:
    if predicate == "faces":
        return _face(subject, target)
    if predicate == "on_top_of":
        return _put_on(subject, target, room)
    if predicate == "beside":
        return _put_beside(subject, target, room)
    if predicate == "centered_under":
        return _center_under(subject, target, room)
    if predicate == "under":
        return _center_under(subject, target, room)
    if predicate == "above":
        return _put_above(subject, target, room)
    # against_wall / mounted_on are satisfied during grounding.
    return predicate in ("against_wall", "mounted_on")


def _face(subject: SceneObject, target: SceneObject) -> bool:
    """Rotate the subject to look at the target."""
    if subject.position.planar_distance_to(target.position) < 1e-3:
        return False

    prior = catalog.get_prior(subject.category)
    if prior is not None and prior.orientation == "free":
        return False

    heading = g2.heading_toward(
        (subject.position.x, subject.position.y), (target.position.x, target.position.y)
    )

    # A wall-backed object may only rotate a little — a sofa against a wall
    # cannot swivel 180° to face a TV behind it. Snap to the nearer of the
    # wall-constrained options instead of tearing it off the wall.
    if subject.wall_id and prior is not None and prior.wall_affinity >= 0.6:
        if g2.angle_between_deg(heading, subject.rotation_z) > 75.0:
            subject.flags.append(f"faces_{target.id}_limited_by_wall")
            return False

    subject.rotation_z = heading
    return True


def _put_on(subject: SceneObject, target: SceneObject, room: RoomFrame) -> bool:
    """Rest the subject on the target's top surface."""
    prior = catalog.get_prior(subject.category)
    if prior is not None and prior.support in ("wall", "ceiling"):
        # A split AC unit or wall TV is fixed to the structure; the model
        # sometimes reports it as sitting on whatever is beneath it. Refuse
        # to re-parent it onto furniture.
        subject.flags.append(f"kept_{prior.support}_mounted_not_on_{target.id}")
        return False

    surface_z = _surface_height(target)
    if surface_z <= 0.0:
        return False

    # Keep the subject within the target's footprint.
    max_offset_x = max(0.0, (target.dimensions.width - subject.dimensions.width) / 2.0)
    max_offset_y = max(0.0, (target.dimensions.depth - subject.dimensions.depth) / 2.0)

    # Deterministic spread so several items on one surface do not stack.
    seed = sum(ord(c) for c in subject.id)
    offset_x = ((seed % 7) / 6.0 - 0.5) * 2.0 * max_offset_x * 0.7
    offset_y = (((seed // 7) % 5) / 4.0 - 0.5) * 2.0 * max_offset_y * 0.7

    theta = math.radians(target.rotation_z)
    subject.position = Vec3(
        target.position.x + offset_x * math.cos(theta) - offset_y * math.sin(theta),
        target.position.y + offset_x * math.sin(theta) + offset_y * math.cos(theta),
        surface_z,
    )
    subject.support = "on_object"
    subject.support_id = target.id
    if "awaiting_support_placement" in subject.flags:
        subject.flags.remove("awaiting_support_placement")
    return True


def _put_beside(subject: SceneObject, target: SceneObject, room: RoomFrame) -> bool:
    """Place the subject alongside the target, sharing its orientation."""
    theta = math.radians(target.rotation_z)
    # Local +X runs along the target's width.
    axis = (math.cos(theta), math.sin(theta))
    offset = target.dimensions.width / 2.0 + subject.dimensions.width / 2.0 + BESIDE_GAP

    candidates = [
        (target.position.x + axis[0] * offset, target.position.y + axis[1] * offset),
        (target.position.x - axis[0] * offset, target.position.y - axis[1] * offset),
    ]
    # Prefer the side that stays inside the room; fall back to whichever is
    # closer to the subject's grounded guess.
    inside = [c for c in candidates if room.contains(c)]
    pool = inside or candidates
    chosen = min(pool, key=lambda c: math.dist(c, (subject.position.x, subject.position.y)))

    chosen = room.clamp_inside(chosen, margin=max(subject.dimensions.width, subject.dimensions.depth) / 2.0)
    subject.position = Vec3(chosen[0], chosen[1], subject.position.z)
    subject.rotation_z = target.rotation_z
    return True


def _center_under(subject: SceneObject, target: SceneObject, room: RoomFrame) -> bool:
    """Centre a floor-level item (rug) beneath another object."""
    subject.position = Vec3(target.position.x, target.position.y, 0.0)
    prior = catalog.get_prior(subject.category)
    if prior is not None and prior.orientation != "free":
        subject.rotation_z = target.rotation_z
    else:
        subject.rotation_z = target.rotation_z
    return True


def _put_above(subject: SceneObject, target: SceneObject, room: RoomFrame) -> bool:
    """Hang the subject directly over the target."""
    z = _surface_height(target) + 0.9
    if z >= room.ceiling_height:
        z = room.ceiling_height - subject.dimensions.height - 0.05
    subject.position = Vec3(target.position.x, target.position.y, max(0.0, z))
    return True


def _arrange_around(
    members: List[SceneObject], target: SceneObject, room: RoomFrame
) -> None:
    """Distribute chairs evenly around a table, each facing inward.

    Seats are laid along the table's long sides first (which is how people
    actually sit), then the ends, rather than on a circle — a circular
    arrangement around a rectangular table reads as obviously synthetic.
    """
    if not members:
        return

    count = len(members)
    half_w = target.dimensions.width / 2.0
    half_d = target.dimensions.depth / 2.0
    theta = math.radians(target.rotation_z)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    offset = _seat_offset(members[0])

    def to_world(local_x: float, local_y: float) -> Tuple[float, float]:
        return (
            target.position.x + local_x * cos_t - local_y * sin_t,
            target.position.y + local_x * sin_t + local_y * cos_t,
        )

    # Build every plausible seat, then choose among them. Generating more
    # candidates than needed lets an unusable seat (outside the room, or up
    # against a wall) be skipped rather than clamped — clamping a seat is what
    # drags a chair back inside the table it is supposed to sit at.
    per_side = max(1, int(target.dimensions.width // 0.62))
    candidates: List[Tuple[float, float, float]] = []  # local x, local y, heading offset

    for sign in (1.0, -1.0):
        for seat in range(per_side):
            fraction = (seat + 0.5) / per_side
            local_x = (fraction - 0.5) * target.dimensions.width
            candidates.append((local_x, sign * (half_d + offset), 180.0 if sign > 0 else 0.0))

    for sign in (1.0, -1.0):
        candidates.append((sign * (half_w + offset), 0.0, -90.0 if sign > 0 else 90.0))

    # Interleave the two long sides so chairs fill opposite sides evenly rather
    # than crowding one edge.
    long_side = [c for c in candidates if abs(c[1]) > abs(c[0])]
    ends = [c for c in candidates if abs(c[1]) <= abs(c[0])]
    front = [c for c in long_side if c[1] > 0]
    back = [c for c in long_side if c[1] < 0]

    ordered: List[Tuple[float, float, float]] = []
    for index in range(max(len(front), len(back))):
        if index < len(front):
            ordered.append(front[index])
        if index < len(back):
            ordered.append(back[index])
    ordered.extend(ends)

    margin = members[0].dimensions.depth / 2.0
    usable = [slot for slot in ordered if room.contains(to_world(slot[0], slot[1]))]
    if len(usable) < count:
        # Not enough seats fit inside the room; fall back to the full set so
        # every chair still gets a distinct place at the table.
        usable = ordered

    for member, (local_x, local_y, heading_offset) in zip(members, usable):
        world = to_world(local_x, local_y)
        if not room.contains(world):
            world = room.clamp_inside(world, margin=margin)
        member.position = Vec3(world[0], world[1], 0.0)
        member.rotation_z = (target.rotation_z + heading_offset) % 360.0
        member.flags.append(f"arranged_around_{target.id}")


def _seat_offset(chair: SceneObject) -> float:
    """How far a chair's centre sits from the table edge."""
    return max(0.18, chair.dimensions.depth / 2.0 - CHAIR_TUCK)


def _surface_height(target: SceneObject) -> float:
    """Top surface height of an object, in metres above the floor."""
    prior = catalog.get_prior(target.category)
    if prior is not None and prior.surface_height > 0:
        # Scale the prior's surface height if the instance was resized.
        if prior.typical[2] > 0:
            ratio = target.dimensions.height / prior.typical[2]
            return target.position.z + prior.surface_height * ratio
        return target.position.z + prior.surface_height
    return target.position.z + target.dimensions.height


def _place_orphan_supported(objects: List[SceneObject], room: RoomFrame) -> None:
    """Rescue ``on_object`` items that no relationship ever placed.

    Rather than leaving them hovering at the room centre, attach them to the
    nearest plausible surface. If there is none, demote them to the floor and
    flag it — visible and wrong is worse than visible and modest.
    """
    surfaces = [
        obj
        for obj in objects
        if (prior := catalog.get_prior(obj.category)) is not None and prior.surface_height > 0
    ]

    for obj in objects:
        if "awaiting_support_placement" not in obj.flags:
            continue

        candidates = [s for s in surfaces if s.id != obj.id]
        target = _nearest(obj, candidates) if candidates else None

        # `_put_on` refuses wall/ceiling-fixed categories, so its result must be
        # checked — otherwise the object keeps its placeholder flag and later
        # gets dropped to the floor by validation.
        if target is not None and _put_on(obj, target, room):
            obj.flags.append(f"auto_supported_by_{target.id}")
            continue

        obj.flags.remove("awaiting_support_placement")
        prior = catalog.get_prior(obj.category)

        if prior is not None and prior.support in ("wall", "ceiling"):
            # Structurally fixed: mount it rather than dropping it on the floor.
            obj.support = prior.support
            z = (
                prior.mount_height
                if prior.support == "wall"
                else room.ceiling_height - obj.dimensions.height
            )
            obj.position = Vec3(obj.position.x, obj.position.y, max(0.0, z))
            obj.flags.append(f"mounted_to_{prior.support}_no_surface_available")
            continue

        obj.support = "floor"
        obj.position = Vec3(obj.position.x, obj.position.y, 0.0)
        obj.flags.append("no_support_found_placed_on_floor")
        obj.confidence = round(obj.confidence * 0.8, 4)
