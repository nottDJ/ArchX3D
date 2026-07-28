"""
ArchX3D — Image grouping and room assignment
============================================
Decides which physical room each reference image shows, and therefore which
room each detected object belongs to.

The problem
-----------
A DXF plan gives geometry but almost never gives room *labels*. Reference
images give room labels ("this is a bedroom") but no position. Neither alone
says which bedroom render belongs to which rectangle on the plan.

The approach
------------
1. **Group images by the room they depict.** Images the model labels
   `bedroom` form one group; `kitchen` another. That grouping is what makes
   two views of the same sofa fuse into one sofa rather than two.
2. **Match each group to a segmented region** by area plausibility — a
   14 m² space is a plausible bedroom and an implausible bathroom — resolved
   as a global assignment rather than greedily, so one bad early pick cannot
   cascade.
3. **Ground each group inside its own room frame.** This is what actually
   enforces "do not place objects in the wrong room": an object detected in
   the bedroom render is back-projected into the bedroom's polygon, so it
   physically cannot land in the kitchen.

Plan-view images are handled separately: they show every room at once, so
their objects are mapped into plan space directly and assigned to whichever
region contains them.

Regions that no image covers stay empty. Furnishing them would mean inventing
objects nobody observed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import permutations
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog, geometry2d as g2
from .classify import ImageProfile
from .observe import ImageObservation
from .rooms import RoomRegion, area_plausibility

#: Assignment below this score is treated as "no plausible room".
MIN_ASSIGNMENT_SCORE = 0.15

#: Exhaustive matching is only tractable for small plans; above this we fall
#: back to a greedy pass. 6! = 720 permutations is instant; 10! is not.
EXHAUSTIVE_LIMIT = 6


@dataclass
class RoomGroup:
    """A physical room, the images that show it, and what they observed."""

    region: RoomRegion
    room_type: str = "unknown"
    room_type_confidence: float = 0.0
    style: str = "unknown"
    image_ids: List[str] = field(default_factory=list)
    observations: List[ImageObservation] = field(default_factory=list)
    #: Why this group landed on this region, for the review UI.
    rationale: str = ""

    @property
    def has_imagery(self) -> bool:
        return bool(self.observations)


@dataclass
class AssignmentResult:
    groups: List[RoomGroup]
    #: Observations from plan views, which span every room.
    plan_observations: List[ImageObservation] = field(default_factory=list)
    #: Observations that contribute geometry only (CAD drawings).
    geometry_observations: List[ImageObservation] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def assign(
    observations: Sequence[ImageObservation],
    profiles: Dict[str, ImageProfile],
    regions: Sequence[RoomRegion],
) -> AssignmentResult:
    """Group images by room and match each group to a segmented region."""
    if not regions:
        return AssignmentResult([], stats={"reason": "no regions to assign to"})

    full_views: List[ImageObservation] = []
    plan_views: List[ImageObservation] = []
    geometry_views: List[ImageObservation] = []

    for observation in observations:
        profile = profiles.get(observation.image_id)
        mode = profile.analysis_mode if profile else "full"
        if mode == "geometry":
            geometry_views.append(observation)
        elif mode == "layout":
            plan_views.append(observation)
        elif mode == "skip":
            continue
        else:
            full_views.append(observation)

    # --- 1. Cluster interior views by the room they depict ----------------
    clusters: Dict[str, List[ImageObservation]] = {}
    for observation in full_views:
        profile = profiles.get(observation.image_id)
        room_type = _room_type_of(observation, profile)
        clusters.setdefault(room_type, []).append(observation)

    # --- 2. Match clusters to regions -------------------------------------
    cluster_types = sorted(clusters, key=lambda t: (-len(clusters[t]), t))
    assignment, rationale = _match_clusters_to_regions(cluster_types, regions)

    groups: List[RoomGroup] = []
    warnings: List[str] = []

    used_regions = set()
    for room_type in cluster_types:
        region_id = assignment.get(room_type)
        if region_id is None:
            warnings.append(
                f"no plausible room found for the {room_type.replace('_', ' ')} imagery; "
                f"its {sum(len(o.objects) for o in clusters[room_type])} detections were dropped"
            )
            continue

        region = next(r for r in regions if r.id == region_id)
        used_regions.add(region_id)
        members = clusters[room_type]

        region.room_type = room_type
        region.room_type_confidence = max(
            (o.room_type_confidence for o in members), default=0.0
        )

        groups.append(
            RoomGroup(
                region=region,
                room_type=room_type,
                room_type_confidence=region.room_type_confidence,
                style=_dominant_style(members),
                image_ids=[o.image_id for o in members],
                observations=members,
                rationale=rationale.get(room_type, ""),
            )
        )

    # --- 3. Regions with no imagery stay empty ----------------------------
    for region in regions:
        if region.id in used_regions:
            continue
        groups.append(
            RoomGroup(
                region=region,
                room_type=region.room_type or "unknown",
                rationale="no reference image covers this room; left unfurnished",
            )
        )

    groups.sort(key=lambda gr: -gr.region.area)

    unfurnished = [g for g in groups if not g.has_imagery]
    if unfurnished:
        warnings.append(
            f"{len(unfurnished)} of {len(regions)} rooms have no reference imagery "
            "and were left unfurnished"
        )

    return AssignmentResult(
        groups=groups,
        plan_observations=plan_views,
        geometry_observations=geometry_views,
        warnings=warnings,
        stats={
            "regions": len(regions),
            "interior_views": len(full_views),
            "plan_views": len(plan_views),
            "geometry_views": len(geometry_views),
            "clusters": {t: len(v) for t, v in clusters.items()},
            "rooms_with_imagery": sum(1 for g in groups if g.has_imagery),
            "rooms_unfurnished": len(unfurnished),
        },
    )


def _room_type_of(observation: ImageObservation, profile: Optional[ImageProfile]) -> str:
    """Best available room label for an image."""
    if observation.room_type and observation.room_type != "unknown":
        return observation.room_type
    if profile and profile.room_type_hint and profile.room_type_hint != "unknown":
        return profile.room_type_hint

    # Fall back to what the furniture implies.
    inferred, _ = catalog.infer_room_type([o.category for o in observation.objects])
    return inferred


def _dominant_style(observations: Sequence[ImageObservation]) -> str:
    tally: Dict[str, float] = {}
    for observation in observations:
        if observation.style and observation.style != "unknown":
            tally[observation.style] = tally.get(observation.style, 0.0) + 1.0
    if not tally:
        return "unknown"
    return max(sorted(tally), key=lambda k: tally[k])


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _match_clusters_to_regions(
    cluster_types: Sequence[str], regions: Sequence[RoomRegion]
) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """Assign each room-type cluster to a distinct region.

    Solved globally rather than greedily: picking the single best region for
    the first cluster can strand a later one with nothing plausible left, and
    the plans here are small enough to enumerate exactly.
    """
    if not cluster_types:
        return {}, {}

    scores: Dict[Tuple[str, str], float] = {}
    for room_type in cluster_types:
        for region in regions:
            scores[(room_type, region.id)] = _score(room_type, region, regions)

    assignment: Dict[str, Optional[str]] = {t: None for t in cluster_types}
    rationale: Dict[str, str] = {}

    if len(cluster_types) <= EXHAUSTIVE_LIMIT and len(regions) <= 8:
        best_total, best_combo = -1.0, None
        for combo in permutations(range(len(regions)), len(cluster_types)):
            total = sum(
                scores[(cluster_types[i], regions[combo[i]].id)]
                for i in range(len(cluster_types))
            )
            if total > best_total:
                best_total, best_combo = total, combo

        if best_combo is not None:
            for index, room_type in enumerate(cluster_types):
                region = regions[best_combo[index]]
                if scores[(room_type, region.id)] >= MIN_ASSIGNMENT_SCORE:
                    assignment[room_type] = region.id
                    rationale[room_type] = _explain(room_type, region, scores[(room_type, region.id)])
        return assignment, rationale

    # Greedy fallback for large plans.
    taken: set = set()
    for room_type in cluster_types:
        candidates = [
            (scores[(room_type, r.id)], r) for r in regions if r.id not in taken
        ]
        if not candidates:
            break
        score, region = max(candidates, key=lambda pair: pair[0])
        if score >= MIN_ASSIGNMENT_SCORE:
            assignment[room_type] = region.id
            taken.add(region.id)
            rationale[room_type] = _explain(room_type, region, score)

    return assignment, rationale


def _score(room_type: str, region: RoomRegion, regions: Sequence[RoomRegion]) -> float:
    """How well does ``region`` suit ``room_type``?"""
    score = area_plausibility(room_type, region.area)

    # Living rooms are typically the largest space; bathrooms the smallest.
    # A rank bonus breaks ties that area alone leaves ambiguous.
    ordered = sorted(regions, key=lambda r: -r.area)
    rank = ordered.index(region) / max(1, len(ordered) - 1) if len(ordered) > 1 else 0.0

    if room_type in ("living_room", "dining_room"):
        score += 0.25 * (1.0 - rank)
    elif room_type in ("bathroom", "hallway"):
        score += 0.25 * rank

    # Bathrooms and hallways are usually narrow; a long thin space suits them.
    if room_type == "hallway" and region.aspect > 2.2:
        score += 0.2

    return max(0.0, score)


def _explain(room_type: str, region: RoomRegion, score: float) -> str:
    return (
        f"matched {room_type.replace('_', ' ')} to {region.id} "
        f"({region.area:.1f} m2, {region.width:.1f}x{region.depth:.1f} m), score {score:.2f}"
    )


# ---------------------------------------------------------------------------
# Object → room
# ---------------------------------------------------------------------------


def room_for_point(
    point: Tuple[float, float], regions: Sequence[RoomRegion]
) -> Optional[RoomRegion]:
    """Region containing ``point``, or the nearest one if it is in a wall."""
    for region in regions:
        if region.contains(point):
            return region

    best, best_distance = None, float("inf")
    for region in regions:
        _, distance = g2.nearest_point_on_polygon(point, region.polygon)
        if distance < best_distance:
            best, best_distance = region, distance

    # Beyond a couple of metres it is not "just outside" any room.
    return best if best_distance < 2.0 else None


def stamp_room_ids(objects, lights, openings, regions: Sequence[RoomRegion]) -> Dict[str, int]:
    """Record which room every entity sits in.

    Objects grounded inside a room frame already carry the right ``room_id``;
    this fills in anything placed by position (plan views) and verifies the
    rest, so nothing reaches Blender unattributed.
    """
    counts: Dict[str, int] = {}

    for obj in objects:
        if not obj.room_id:
            region = room_for_point((obj.position.x, obj.position.y), regions)
            obj.room_id = region.id if region else ""
            if region is None:
                obj.flags.append("outside_every_room")
        counts[obj.room_id or "unassigned"] = counts.get(obj.room_id or "unassigned", 0) + 1

    for light in lights:
        if not light.room_id:
            region = room_for_point((light.position.x, light.position.y), regions)
            light.room_id = region.id if region else ""

    for opening in openings:
        if not opening.room_id:
            region = room_for_point((opening.position.x, opening.position.y), regions)
            opening.room_id = region.id if region else ""

    return counts
