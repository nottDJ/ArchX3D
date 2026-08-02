"""
ArchX3D — Interior-view registration
====================================
Answers, for a group of interior photographs: *which room in the drawing is
this?*

Why this is registration and not classification
-----------------------------------------------
A perspective photograph of a bedroom shares no coordinate system with the
plan, so there is no transform to fit — the plan-view machinery does not
apply. But the question is the same question: which part of the authoritative
model does this picture describe? Getting it wrong has the same consequence,
which is a sofa in the bathroom.

What was wrong before
---------------------
The pipeline classifies every room from the drawing first — labels, blocks,
layers — and stamps the answer onto each region, with a comment saying this is
what lets a bedroom photo be matched to the region the drawing already calls a
bedroom. Then the matcher scored regions on floor area alone and never read
it. A drawing that *states* ``MASTER BEDROOM`` at a known position was being
ignored in favour of guessing from square metres, and worse, the winning image
then overwrote the drawing's room type with its own.

That inverts the project's trust hierarchy at the one point where it matters
most. CAD text is tier 4; a vision model's impression is tier 6. This module
puts them back in order:

* A region the drawing names is matched on that name.
* Area plausibility is a *prior*, used where the drawing is silent and to
  break ties, never to overrule a stated fact.
* When the image and the drawing disagree, the drawing wins and the
  disagreement is recorded rather than resolved silently.

Design constraints
------------------
Stdlib only, and no vision imports. The area prior is injected as a callable
so that ``vision.rooms``' table stays the single definition of it — this
module must not grow a second, drifting copy.
"""

from __future__ import annotations

from itertools import permutations
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .schema import RoomRegistration

#: Below this score a room type has no plausible home in the plan.
MIN_SCORE = 0.15

#: Exhaustive matching is only tractable for small plans. Mirrors the limits
#: the previous implementation used, so behaviour on large plans is unchanged.
EXHAUSTIVE_TYPES = 6
EXHAUSTIVE_REGIONS = 8

#: How strongly a CAD-stated room type dominates. At full CAD confidence a
#: name match contributes this much, and a name *mismatch* removes this share
#: of whatever the other signals contributed.
CAD_AGREEMENT_BONUS = 1.30
CAD_CONFLICT_PENALTY = 0.85

#: A CAD room type below this confidence is treated as a hint, not a statement.
CAD_STATEMENT_THRESHOLD = 0.45

AreaPrior = Callable[[str, float], float]


def register_interior_views(
    observed_types: Sequence[str],
    regions: Sequence[Any],
    *,
    area_plausibility: Optional[AreaPrior] = None,
    image_ids_by_type: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, RoomRegistration]:
    """Match each observed room type to the region that is that room.

    ``observed_types`` are the room types the imagery was clustered into.
    ``regions`` expose ``id``, ``area``, ``room_type``, ``room_type_confidence``
    and ``aspect`` — ``vision.rooms.RoomRegion`` does, and so does any stand-in
    a test builds.

    Solved globally rather than greedily wherever the plan is small enough:
    taking the single best region for the first cluster can strand a later one
    with nothing plausible left, and one bad early pick then cascades through
    every remaining room.
    """
    image_ids_by_type = image_ids_by_type or {}
    if not observed_types or not regions:
        return {}

    scores: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
    for room_type in observed_types:
        for region in regions:
            scores[(room_type, region.id)] = score_region(
                room_type, region, regions, area_plausibility=area_plausibility
            )

    chosen = _solve(observed_types, regions, scores)

    registrations: Dict[str, RoomRegistration] = {}
    for room_type in observed_types:
        region_id = chosen.get(room_type)
        if region_id is None:
            registrations[room_type] = RoomRegistration(
                image_ids=list(image_ids_by_type.get(room_type, [])),
                observed_room_type=room_type,
                method="none",
                reasons=[
                    f"no region scored above {MIN_SCORE:.2f} as a "
                    f"{room_type.replace('_', ' ')}"
                ],
            )
            continue

        region = next(r for r in regions if r.id == region_id)
        score, reasons = scores[(room_type, region_id)]
        cad_type = _cad_room_type(region)
        conflicts = bool(cad_type) and cad_type != room_type

        registrations[room_type] = RoomRegistration(
            image_ids=list(image_ids_by_type.get(room_type, [])),
            room_id=region_id,
            observed_room_type=room_type,
            cad_room_type=cad_type or "unknown",
            score=score,
            confidence=_confidence(score, region, conflicts),
            method="cad_room_type" if cad_type else "area_plausibility",
            reasons=reasons,
            conflicts_with_cad=conflicts,
        )

    return registrations


def score_region(
    room_type: str,
    region: Any,
    regions: Sequence[Any],
    *,
    area_plausibility: Optional[AreaPrior] = None,
) -> Tuple[float, List[str]]:
    """How well ``region`` suits imagery showing a ``room_type``.

    Returns ``(score, reasons)``. The reasons are surfaced in the review UI
    and in the pipeline log, because an assignment a user disagrees with is
    only actionable if they can see what drove it.
    """
    reasons: List[str] = []
    score = 0.0

    # --- The drawing's own answer, where it has one ------------------------
    cad_type = _cad_room_type(region)
    cad_confidence = float(getattr(region, "room_type_confidence", 0.0) or 0.0)

    # --- Weak geometric priors --------------------------------------------
    if area_plausibility is not None:
        area = float(getattr(region, "area", 0.0) or 0.0)
        plausibility = max(0.0, float(area_plausibility(room_type, area)))
        if plausibility > 0.0:
            score += plausibility
            reasons.append(
                f"{area:.1f} m2 is a plausible {room_type.replace('_', ' ')} "
                f"({plausibility:.2f})"
            )

    score += _rank_bonus(room_type, region, regions, reasons)

    # --- The drawing arbitrates -------------------------------------------
    # Applied last so it acts on the total: a stated name should be able to
    # overturn every heuristic combined, which is what tier 4 outranking
    # tier 6 means in practice.
    if cad_type and cad_confidence >= CAD_STATEMENT_THRESHOLD:
        if cad_type == room_type:
            bonus = CAD_AGREEMENT_BONUS * cad_confidence
            score += bonus
            reasons.insert(
                0,
                f"the drawing names this room {cad_type.replace('_', ' ')} "
                f"({cad_confidence:.0%} confident)",
            )
        else:
            score *= 1.0 - CAD_CONFLICT_PENALTY * cad_confidence
            reasons.insert(
                0,
                f"the drawing names this room {cad_type.replace('_', ' ')}, not "
                f"{room_type.replace('_', ' ')} ({cad_confidence:.0%} confident)",
            )

    return max(0.0, score), reasons


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _cad_room_type(region: Any) -> str:
    """The drawing-derived room type stamped on a region, if any."""
    value = str(getattr(region, "room_type", "") or "").strip()
    return "" if value in ("", "unknown") else value


def _rank_bonus(
    room_type: str, region: Any, regions: Sequence[Any], reasons: List[str]
) -> float:
    """Size- and shape-based tie-breakers, preserved from the previous matcher.

    These are genuinely weak — they encode "living rooms tend to be the
    biggest space" — so they stay small enough that any stated fact outranks
    them, but they still resolve the case where the drawing is silent and two
    regions are equally plausible by area.
    """
    bonus = 0.0

    ordered = sorted(regions, key=lambda r: -float(getattr(r, "area", 0.0) or 0.0))
    if len(ordered) > 1:
        rank = ordered.index(region) / (len(ordered) - 1)
    else:
        rank = 0.0

    if room_type in ("living_room", "dining_room"):
        bonus += 0.25 * (1.0 - rank)
        if rank < 0.34:
            reasons.append("one of the largest spaces in the plan")
    elif room_type in ("bathroom", "hallway"):
        bonus += 0.25 * rank
        if rank > 0.66:
            reasons.append("one of the smallest spaces in the plan")

    if room_type == "hallway" and float(getattr(region, "aspect", 1.0) or 1.0) > 2.2:
        bonus += 0.2
        reasons.append("long and narrow, as circulation is")

    return bonus


def _solve(
    observed_types: Sequence[str],
    regions: Sequence[Any],
    scores: Dict[Tuple[str, str], Tuple[float, List[str]]],
) -> Dict[str, Optional[str]]:
    """Assign each room type a distinct region, maximising total score."""
    chosen: Dict[str, Optional[str]] = {t: None for t in observed_types}

    if len(observed_types) <= EXHAUSTIVE_TYPES and len(regions) <= EXHAUSTIVE_REGIONS:
        best_total, best_combo = -1.0, None
        for combo in permutations(range(len(regions)), len(observed_types)):
            total = sum(
                scores[(observed_types[i], regions[combo[i]].id)][0]
                for i in range(len(observed_types))
            )
            if total > best_total:
                best_total, best_combo = total, combo

        if best_combo is not None:
            for index, room_type in enumerate(observed_types):
                region = regions[best_combo[index]]
                if scores[(room_type, region.id)][0] >= MIN_SCORE:
                    chosen[room_type] = region.id
        return chosen

    taken: set = set()
    for room_type in observed_types:
        available = [r for r in regions if r.id not in taken]
        if not available:
            break
        region = max(available, key=lambda r: scores[(room_type, r.id)][0])
        if scores[(room_type, region.id)][0] >= MIN_SCORE:
            chosen[room_type] = region.id
            taken.add(region.id)

    return chosen


def _confidence(score: float, region: Any, conflicts: bool) -> float:
    """Trust in one interior registration.

    A match the drawing corroborates is worth far more than a match the
    drawing merely fails to contradict, and one it actively contradicts is
    worth very little — the image is being placed there because nothing better
    was available, not because the evidence points there.
    """
    cad_confidence = float(getattr(region, "room_type_confidence", 0.0) or 0.0)
    cad_type = _cad_room_type(region)

    if conflicts:
        return round(max(0.05, 0.25 * (1.0 - cad_confidence)), 3)
    if cad_type and cad_confidence >= CAD_STATEMENT_THRESHOLD:
        return round(min(0.95, 0.55 + 0.40 * cad_confidence), 3)
    return round(min(0.6, 0.25 + 0.25 * min(1.0, score)), 3)
