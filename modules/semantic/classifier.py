"""
ArchX3D — Room Classifier
=========================
Decides what every room in a plan is *for*, from all available evidence.

This is the module the brief's Stage 3 describes. Room type never rests on one
signal; every signal contributes, and the result carries a confidence and the
reasons behind it::

    bedroom  92%
      text label "MASTER BED" inside the room
      BED block at 2.1 m from the centroid
      WARDROBE block present
      adjacent to bathroom
      13.4 m2 suits a bedroom

Signals implemented
-------------------
============================  =====  ==========================================
signal                        tier   what it reads
============================  =====  ==========================================
room_name_attribute             1    ROOM_NAME on a room-tag block
block_fixture                   2    toilet / sink / cooktop / bed blocks
block_furniture                 2    sofa, wardrobe, dining table blocks
layer_role                      3    plumbing / casework layers inside the room
room_label                      4    TEXT or MTEXT naming the room
area                            5    floor area against per-type priors
aspect                          5    elongation (the hallway discriminator)
window_count                    5    openings on exterior walls
door_count                      5    a room with four doors is circulation
adjacency                       5    an en-suite opens onto a bedroom
privacy_depth                   5    graph distance from the entrance
vision                          6    the room type the imagery reported
============================  =====  ==========================================

Each is deliberately independent, because fusion assumes independence. Where
two signals would double-count the same underlying fact — a ``PLUMBING`` layer
and the ``WC`` block drawn on it — the block wins and the layer signal skips
the region, which ``_layer_evidence`` handles explicitly.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import taxonomy
from .evidence import Conflict, Evidence, fuse

Point = Tuple[float, float]

#: Trust tiers, mirroring ``cad.Source``. Duplicated as plain integers so this
#: package does not import ``cad`` — the classifier must work on a scene graph
#: whose CAD document was never available.
TIER_METADATA = 1
TIER_BLOCK = 2
TIER_LAYER = 3
TIER_TEXT = 4
TIER_GEOMETRY = 5
TIER_IMAGE = 6

#: Fewest rooms for which circulation depth carries information.
MIN_ROOMS_FOR_DEPTH = 4


@dataclass
class RoomEvidenceInput:
    """Everything known about one region, in the classifier's own terms.

    A plain input record rather than a ``RoomRegion`` or a ``Room``: the
    classifier is used from the vision pipeline, from CLI tooling and from
    tests, and coupling it to any one of those types would drag that type's
    whole dependency tree along with it.
    """

    room_id: str
    area: float
    #: Boundary polygon in plan metres.
    polygon: List[Point] = field(default_factory=list)
    width: float = 0.0
    depth: float = 0.0
    centroid: Point = (0.0, 0.0)

    #: Room-naming text found inside the polygon: ``(text, room_type, conf)``.
    labels: List[Tuple[str, str, float]] = field(default_factory=list)
    #: Floor area the drawing itself states for this room, m²; 0 when none.
    #: Independent of ``area``, which comes from segmentation — comparing the
    #: two is the only available check on whether segmentation worked.
    declared_area: float = 0.0
    #: Structured room names from block attributes: ``(value, room_type)``.
    name_attributes: List[Tuple[str, str]] = field(default_factory=list)
    #: Blocks inside the polygon: ``(category, kind, confidence, distance_m)``.
    blocks: List[Tuple[str, str, float, float]] = field(default_factory=list)
    #: Layer roles of geometry inside the polygon, with entity counts.
    layer_roles: Dict[str, int] = field(default_factory=dict)

    door_count: int = 0
    window_count: int = 0
    #: Whether opening counts are actually known.
    #:
    #: Critical distinction: a ``window_count`` of 0 because the drawing has
    #: no glazing layer is *not* the same claim as a room genuinely having no
    #: window. Conflating them turns absence of evidence into evidence of
    #: absence — on a legacy geometry file with no opening data every room
    #: scores as windowless, and the types that expect to be windowless
    #: (garage, store, shaft) win on rooms they have no business winning.
    openings_known: bool = False
    #: Ids of regions reachable through a doorway.
    neighbours: List[str] = field(default_factory=list)
    #: Graph distance from the entrance, normalised to [0, 1]; -1 if unknown.
    depth_normalised: float = -1.0

    #: Room type the vision layer reported, with its confidence.
    vision_room_type: str = ""
    vision_confidence: float = 0.0
    #: Object categories the vision layer detected in this room.
    vision_categories: List[str] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        short = min(self.width, self.depth)
        return (max(self.width, self.depth) / short) if short > 1e-6 else 1.0


@dataclass
class RoomClassification:
    """The classifier's verdict on one room."""

    room_id: str
    room_type: str
    confidence: float
    #: The specific label where it refines the canonical type, e.g.
    #: "master_bedroom" classified as "bedroom".
    specific_type: str = ""
    reasons: List[str] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    posterior: Dict[str, float] = field(default_factory=dict)
    evidence: List[Evidence] = field(default_factory=list)
    decided_by: str = ""
    runner_up: str = ""
    runner_up_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_type": self.room_type,
            "specific_type": self.specific_type,
            "confidence": round(self.confidence, 3),
            "runner_up": self.runner_up,
            "runner_up_confidence": round(self.runner_up_confidence, 3),
            "decided_by": self.decided_by,
            "reasons": list(self.reasons),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "posterior": {k: round(v, 4) for k, v in self.posterior.items()},
            "evidence": [e.to_dict() for e in self.evidence],
        }

    def summary(self) -> str:
        """One-line human summary, the form the brief asks for."""
        head = f"{self.room_type} {self.confidence:.0%}"
        if self.reasons:
            return head + "\n  " + "\n  ".join(self.reasons)
        return head


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _name_attribute_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 1 — a room-tag block whose attributes name the space.

    Authoritative: structured metadata written against this specific room is
    the strongest statement a DXF can make about it.
    """
    out = []
    for value, room_type in room.name_attributes:
        canonical = taxonomy.normalise_category(room_type)
        canonical = _canonical_room(canonical)
        if canonical not in taxonomy.ROOM_PRIORS:
            continue
        out.append(Evidence(
            signal="room_name_attribute",
            tier=TIER_METADATA,
            scores={canonical: 5.0},
            reason=f'block attribute names this room "{value}"',
            source="cad_metadata",
            authoritative=True,
        ))
    return out


def _label_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 4 — a TEXT/MTEXT room label inside the polygon.

    Authoritative for the same reason as an attribute: it *states* the room
    type rather than implying it. It sits below block evidence in the trust
    order only because a label can survive a plan revision that redrew the
    blocks, which is exactly the case the conflict report exists to surface.
    """
    out = []
    for text, room_type, confidence in room.labels:
        canonical = _canonical_room(room_type)
        if canonical not in taxonomy.ROOM_PRIORS:
            continue
        out.append(Evidence(
            signal="room_label",
            tier=TIER_TEXT,
            scores={canonical: 4.5},
            reason=f'text label "{text}" inside the room',
            source="cad_text",
            weight=max(0.5, confidence),
            authoritative=True,
        ))
    return out


def _block_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 2 — furniture and fixture blocks placed inside the room.

    Not authoritative: a bed block is overwhelming evidence *for* a bedroom
    but does not state that the room is one (a showroom, a studio). It is
    strong enough that fusion reaches the right answer regardless, without
    suppressing an explicit label that disagrees.

    Repeated instances of one category are counted once at full strength with
    a small bonus per extra instance. Six dining chairs are more informative
    than one, but not six times more — and letting them add linearly is
    exactly how a dining set out-votes a decisive fixture.
    """
    grouped: Dict[str, List[Tuple[str, float, float]]] = {}
    for category, kind, confidence, distance in room.blocks:
        normalised = taxonomy.normalise_category(category)
        if not normalised:
            continue
        grouped.setdefault(normalised, []).append((kind, confidence, distance))

    out = []
    for category, instances in grouped.items():
        table = taxonomy.object_evidence(category)
        if not table:
            continue

        count = len(instances)
        best_confidence = max(c for _, c, _ in instances)
        nearest = min(d for _, _, d in instances)
        kind = instances[0][0]

        # Diminishing returns on repeats.
        multiplicity = 1.0 + 0.25 * math.log(count) if count > 1 else 1.0
        weight = max(0.4, best_confidence) * multiplicity

        plural = f" x{count}" if count > 1 else ""
        decisive = category in taxonomy.DECISIVE_CATEGORIES
        reason = (
            f"{category.upper()}{plural} block"
            + (f" {nearest:.1f} m from the centroid" if nearest > 0 else "")
            + (" (decisive)" if decisive else "")
        )

        out.append(Evidence(
            signal="block_fixture" if kind in (
                "plumbing_fixture", "kitchen_fixture", "appliance"
            ) else "block_furniture",
            tier=TIER_BLOCK,
            scores=dict(table),
            reason=reason,
            source="cad_block",
            weight=weight,
        ))
    return out


def _layer_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 3 — layer roles of geometry inside the room.

    Skipped for any role already accounted for by a block, because a ``WC``
    block sitting on an ``A-FLOR-PFIX`` layer is one fact, not two, and
    counting it twice inflates confidence on exactly the rooms where the
    evidence is most decisive.
    """
    covered_kinds = {kind for _, kind, _, _ in room.blocks}
    role_to_kind = {
        "plumbing_fixture": "plumbing_fixture",
        "casework": "casework",
        "appliance": "appliance",
        "furniture": "furniture",
    }

    out = []
    for role, count in room.layer_roles.items():
        table = taxonomy.LAYER_ROLE_EVIDENCE.get(role)
        if not table:
            continue
        if role_to_kind.get(role) in covered_kinds:
            continue
        out.append(Evidence(
            signal="layer_role",
            tier=TIER_LAYER,
            scores=dict(table),
            reason=f"{count} entities on a {role.replace('_', ' ')} layer",
            source="cad_layer",
            weight=min(1.0, 0.5 + 0.1 * count),
        ))
    return out


def _geometry_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 5 — area, aspect, doors and windows."""
    out: List[Evidence] = []

    if room.area > 0:
        area_scores = {
            room_type: prior.area_score(room.area)
            for room_type, prior in taxonomy.ROOM_PRIORS.items()
        }
        # The reason states the measurement, not which type it best suits:
        # one Evidence scores every type, and it is rendered in the context of
        # whichever type won, so naming the global best-fit here would caption
        # a bathroom's evidence with "suits a store".
        out.append(Evidence(
            signal="area", tier=TIER_GEOMETRY, scores=area_scores,
            reason=f"{room.area:.1f} m2 floor area",
            source="cad_geometry",
        ))

    if room.width > 0 and room.depth > 0:
        aspect = room.aspect
        aspect_scores = {
            room_type: prior.aspect_score(aspect)
            for room_type, prior in taxonomy.ROOM_PRIORS.items()
        }
        out.append(Evidence(
            signal="aspect", tier=TIER_GEOMETRY, scores=aspect_scores,
            reason=(
                f"{aspect:.1f}:1 proportions"
                + (" suit a corridor" if aspect > 3.0 else " are room-like")
            ),
            source="cad_geometry",
        ))

    # Opening counts are only evidence when openings were actually surveyed.
    if room.openings_known:
        window_scores = {
            room_type: taxonomy.window_evidence(room.window_count, room_type)
            for room_type in taxonomy.ROOM_PRIORS
        }
        if any(window_scores.values()):
            out.append(Evidence(
                signal="window_count", tier=TIER_GEOMETRY, scores=window_scores,
                reason=(
                    f"{room.window_count} window(s)"
                    if room.window_count else "no windows"
                ),
                source="cad_geometry",
            ))

        door_scores = {
            room_type: taxonomy.door_evidence(room.door_count, room_type)
            for room_type in taxonomy.ROOM_PRIORS
        }
        if any(door_scores.values()):
            out.append(Evidence(
                signal="door_count", tier=TIER_GEOMETRY, scores=door_scores,
                reason=f"{room.door_count} door(s)",
                source="cad_geometry",
            ))

    return out


def _topology_evidence(
    room: RoomEvidenceInput, neighbour_types: Dict[str, str]
) -> List[Evidence]:
    """Tier 5 — adjacency and depth from the entrance.

    Runs in a second pass, because it needs the first pass's answers for the
    neighbouring rooms. That two-pass structure is what makes "adjacent to a
    bathroom" usable evidence for "bedroom".
    """
    out: List[Evidence] = []

    types_nearby = [
        neighbour_types[n] for n in room.neighbours
        if neighbour_types.get(n) and neighbour_types[n] != "unknown"
    ]
    if types_nearby:
        scores: Dict[str, float] = {}
        all_reasons: List[str] = []
        for room_type in taxonomy.ROOM_PRIORS:
            score, reasons = taxonomy.adjacency_evidence(room_type, types_nearby)
            if score:
                scores[room_type] = score
                all_reasons.extend(reasons)
        if scores:
            unique = sorted(set(all_reasons))
            out.append(Evidence(
                signal="adjacency", tier=TIER_GEOMETRY, scores=scores,
                reason=", ".join(unique[:3]),
                source="cad_geometry",
            ))

    if room.depth_normalised >= 0:
        depth_scores = {
            room_type: taxonomy.privacy_evidence(room_type, room.depth_normalised)
            for room_type in taxonomy.ROOM_PRIORS
        }
        if any(abs(v) > 0.05 for v in depth_scores.values()):
            descriptor = (
                "deep in the plan" if room.depth_normalised > 0.6
                else "near the entrance" if room.depth_normalised < 0.35
                else "mid-plan"
            )
            out.append(Evidence(
                signal="privacy_depth", tier=TIER_GEOMETRY, scores=depth_scores,
                reason=f"{descriptor} (depth {room.depth_normalised:.2f})",
                source="reasoning",
            ))

    return out


def _vision_evidence(room: RoomEvidenceInput) -> List[Evidence]:
    """Tier 6 — what the reference imagery reported.

    Strong but not authoritative, and deliberately below CAD: a photograph is
    matched to a room by inference, so a confident detection in the *wrong*
    room is a real failure mode that CAD evidence should be able to overrule.
    """
    out: List[Evidence] = []

    if room.vision_room_type and room.vision_confidence > 0:
        canonical = _canonical_room(taxonomy.normalise_category(room.vision_room_type))
        if canonical in taxonomy.ROOM_PRIORS:
            out.append(Evidence(
                signal="vision", tier=TIER_IMAGE,
                scores={canonical: 3.0},
                reason=(
                    f"reference imagery reads as a {canonical.replace('_', ' ')} "
                    f"({room.vision_confidence:.0%})"
                ),
                source="image",
                weight=max(0.3, room.vision_confidence),
            ))

    # Objects the vision layer detected score through the same table as CAD
    # blocks, so the two streams corroborate on one set of priors.
    seen: Dict[str, int] = {}
    for category in room.vision_categories:
        normalised = taxonomy.normalise_category(category)
        if normalised:
            seen[normalised] = seen.get(normalised, 0) + 1

    for category, count in seen.items():
        table = taxonomy.object_evidence(category)
        if not table:
            continue
        out.append(Evidence(
            signal="vision_object", tier=TIER_IMAGE, scores=dict(table),
            reason=f"{category} detected in the imagery"
                   + (f" (x{count})" if count > 1 else ""),
            source="image",
            # Halved against CAD: an image detection is an inference about
            # what is present, where a block is a record of it.
            weight=0.5 * (1.0 + 0.2 * math.log(count) if count > 1 else 1.0),
        ))

    return out


# ---------------------------------------------------------------------------
# Cross-checks
# ---------------------------------------------------------------------------

#: How far the segmented area may drift from the drawing's stated area before
#: it is reported. Generous: a stated size is usually the *internal clear*
#: dimension while segmentation measures to the wall faces, and labels are
#: routinely rounded, so a third either way is ordinary.
AREA_DISAGREEMENT_RATIO = 1.5


def _area_disagreement(room: RoomEvidenceInput) -> List[Conflict]:
    """Report a room whose measured area contradicts its stated one.

    This is the one genuinely independent check available on room
    segmentation. Flood-fill silently merges rooms through an unclosed
    doorway and silently splits them at a wall drawn as two offset lines;
    either way the geometry looks plausible and nothing errors. But a label
    reading ``BED ROOM 16'0" X 15'9"`` states 23 m², and if the region under
    it measures 4.6 m² then segmentation lost most of that room.

    Reported, never corrected: the fix is a segmentation parameter or a repair
    to the drawing, and silently resizing a room to match its label would
    invent geometry that is not there.
    """
    if room.declared_area <= 0 or room.area <= 0:
        return []

    ratio = max(room.area, room.declared_area) / min(room.area, room.declared_area)
    if ratio <= AREA_DISAGREEMENT_RATIO:
        return []

    lost = room.declared_area > room.area
    return [Conflict(
        claimed=f"{room.declared_area:.1f} m2 (stated in the drawing)",
        contradicted_by=f"{room.area:.1f} m2 (measured by segmentation)",
        signal="area_mismatch",
        detail=(
            f"the drawing states {room.declared_area:.1f} m2 but segmentation "
            f"recovered {room.area:.1f} m2 ({ratio:.1f}x "
            f"{'smaller' if lost else 'larger'}); "
            + (
                "the room boundary is probably not closed, so part of it was "
                "lost" if lost else
                "adjacent rooms were probably merged through an opening"
            )
        ),
        severity=min(0.95, 0.3 + 0.1 * ratio),
    )]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def classify_room(
    room: RoomEvidenceInput,
    neighbour_types: Optional[Dict[str, str]] = None,
) -> RoomClassification:
    """Classify one room from every available signal."""
    evidence: List[Evidence] = []
    evidence.extend(_name_attribute_evidence(room))
    evidence.extend(_label_evidence(room))
    evidence.extend(_block_evidence(room))
    evidence.extend(_layer_evidence(room))
    evidence.extend(_geometry_evidence(room))
    evidence.extend(_vision_evidence(room))
    if neighbour_types:
        evidence.extend(_topology_evidence(room, neighbour_types))

    result = fuse(evidence, taxonomy.scoreable_types())
    result.conflicts.extend(_area_disagreement(room))

    return RoomClassification(
        room_id=room.room_id,
        room_type=result.label,
        confidence=result.confidence,
        specific_type=_specific_label(room, result.label),
        reasons=result.reasons,
        conflicts=result.conflicts,
        posterior=result.posterior,
        evidence=result.evidence,
        decided_by=result.decided_by,
        runner_up=result.runner_up,
        runner_up_confidence=result.runner_up_confidence,
    )


def classify_plan(
    rooms: Sequence[RoomEvidenceInput],
    *,
    passes: int = 2,
) -> List[RoomClassification]:
    """Classify every room, letting adjacency evidence propagate.

    Two passes by default. The first classifies each room independently; the
    second re-runs with every room's neighbours labelled, which is what turns
    "small windowless room off a bedroom" into "en-suite bathroom".

    Convergence is not guaranteed in general, so the pass count is bounded and
    the loop stops early once labels stop changing. Oscillation between two
    equally-supported labels is possible in principle and harmless in
    practice: the run ends and the conflict is reported.
    """
    if not rooms:
        return []

    _infer_depths(rooms)

    results = {room.room_id: classify_room(room) for room in rooms}

    for _ in range(max(0, passes - 1)):
        neighbour_types = {
            room_id: result.room_type for room_id, result in results.items()
        }
        updated = {
            room.room_id: classify_room(room, neighbour_types) for room in rooms
        }
        if all(
            updated[k].room_type == results[k].room_type for k in results
        ):
            results = updated
            break
        results = updated

    _resolve_singletons(rooms, results)

    return [results[room.room_id] for room in rooms]


def _resolve_singletons(
    rooms: Sequence[RoomEvidenceInput],
    results: Dict[str, RoomClassification],
) -> None:
    """Flag duplicate non-repeatable rooms rather than silently reassigning.

    A plan with two kitchens is either an open-plan space read twice or a
    genuine mistake. Both readings are useful to a human and neither is safe
    to fix automatically, so the weaker instance is annotated, not changed.
    """
    by_type: Dict[str, List[str]] = {}
    for room_id, result in results.items():
        prior = taxonomy.ROOM_PRIORS.get(result.room_type)
        if prior is not None and not prior.repeatable:
            by_type.setdefault(result.room_type, []).append(room_id)

    for room_type, room_ids in by_type.items():
        if len(room_ids) < 2:
            continue
        ranked = sorted(room_ids, key=lambda r: -results[r].confidence)
        winner = ranked[0]
        for loser in ranked[1:]:
            results[loser].conflicts.append(Conflict(
                claimed=room_type,
                contradicted_by=room_type,
                signal="uniqueness",
                detail=(
                    f"a plan usually has one {room_type.replace('_', ' ')}; "
                    f"{winner} is the stronger candidate "
                    f"({results[winner].confidence:.0%} vs "
                    f"{results[loser].confidence:.0%})"
                ),
                severity=0.4,
            ))


def _infer_depths(rooms: Sequence[RoomEvidenceInput]) -> None:
    """Fill ``depth_normalised`` by BFS from the most entrance-like room.

    The entrance is taken to be the room with the most external doors, or
    failing that the most connected room — an entrance hall is the space
    everything else hangs off. Only computed when a caller has not supplied
    depths already.
    """
    if any(room.depth_normalised >= 0 for room in rooms):
        return

    # Depth from the entrance is a property of a *plan*. In a two- or
    # three-room fragment the "entrance" is whichever room happened to win a
    # tie-break, and the resulting privacy score is noise that can actively
    # mislead — an en-suite picked as the entrance is penalised for being
    # private, which is the opposite of the truth.
    if len(rooms) < MIN_ROOMS_FOR_DEPTH:
        return

    by_id = {room.room_id: room for room in rooms}
    start = max(rooms, key=lambda r: (r.door_count, len(r.neighbours)))

    distances: Dict[str, int] = {start.room_id: 0}
    queue = deque([start.room_id])
    while queue:
        current = queue.popleft()
        for neighbour in by_id[current].neighbours:
            if neighbour in distances or neighbour not in by_id:
                continue
            distances[neighbour] = distances[current] + 1
            queue.append(neighbour)

    if not distances:
        return
    furthest = max(distances.values())
    if furthest <= 0:
        return

    for room in rooms:
        hops = distances.get(room.room_id)
        room.depth_normalised = (hops / furthest) if hops is not None else -1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_room(room_type: str) -> str:
    """Collapse refined types (``master_bedroom``) onto their scored parent."""
    parents = {
        "master_bedroom": "bedroom",
        "staircase": "staircase",
        "studio": "studio",
    }
    return parents.get(room_type, room_type)


def _specific_label(room: RoomEvidenceInput, canonical: str) -> str:
    """Keep a more specific label when the drawing supplied one.

    ``master_bedroom`` scores as ``bedroom`` — the priors are the same — but a
    user reading the review UI should still see "Master Bedroom".
    """
    for _, room_type, _ in room.labels:
        if room_type != canonical and _canonical_room(room_type) == canonical:
            return room_type
    for _, room_type in room.name_attributes:
        if room_type != canonical and _canonical_room(room_type) == canonical:
            return room_type
    return ""


def summarise(classifications: Sequence[RoomClassification]) -> Dict[str, Any]:
    """Aggregate statistics for diagnostics."""
    if not classifications:
        return {"rooms": 0}

    by_type: Dict[str, int] = {}
    by_signal: Dict[str, int] = {}
    confidences = []

    for classification in classifications:
        by_type[classification.room_type] = by_type.get(classification.room_type, 0) + 1
        confidences.append(classification.confidence)
        if classification.decided_by:
            by_signal[classification.decided_by] = (
                by_signal.get(classification.decided_by, 0) + 1
            )

    identified = sum(1 for c in classifications if c.room_type != "unknown")
    confident = sum(1 for c in classifications if c.confidence >= 0.65)

    return {
        "rooms": len(classifications),
        "identified": identified,
        "unidentified": len(classifications) - identified,
        "confident": confident,
        "mean_confidence": round(sum(confidences) / len(confidences), 3),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "decided_by": by_signal,
        "conflicts": sum(len(c.conflicts) for c in classifications),
    }
