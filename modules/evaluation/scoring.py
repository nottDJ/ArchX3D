"""
ArchX3D — Aggregation
=====================
Turning per-axis measurements into per-viewpoint, per-room and building
scores, without any of the three quietly lying about the others.

Unmeasured is excluded, not zero
--------------------------------
The rule this module exists to enforce. An axis that could not be measured —
no reference photograph, no albedo pass, no numpy — contributes *nothing to
either side* of the average. Scoring it zero would assert "the reconstruction
is wrong here", which is a claim the evidence does not support; averaging it
in as 1.0 would assert the opposite. Excluding it says "not assessed", which
is what actually happened.

The cost of exclusion is carried by two figures that travel with every score:

``weight_used``   the share of the total axis weight that was measurable. A
                  0.9 over five axes and a 0.9 over two are different claims,
                  and this is what stops the second passing as the first.
``confidence``    how much to trust the number, folding in both the axes'
                  own confidence and how much of the picture was visible.

Aggregating upward
------------------
Room scores are the mean of their viewpoints' scores, weighted by each
viewpoint's confidence: a view whose reference photograph was blurry and whose
camera fit was shaky should not outvote a clean one. The building score is the
mean of the room scores, weighted by room area, because a 40 m² living room
being wrong matters more than a 3 m² utility cupboard. Where areas are
unknown, rooms weigh equally — an honest fallback rather than a guess.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import (
    AXES,
    DEFAULT_WEIGHTS,
    AxisScore,
    Finding,
    ScoreSet,
    merge,
    rank,
)


# ---------------------------------------------------------------------------
# Across axes
# ---------------------------------------------------------------------------


def combine(axes: Dict[str, AxisScore],
            weights: Optional[Dict[str, float]] = None) -> ScoreSet:
    """One score from several axes, excluding the ones that were not measured.

    Returns a zero-confidence, zero-weight :class:`ScoreSet` when nothing was
    measurable at all — which is distinguishable from a genuine score of zero
    by ``weight_used``, and is why that field is not decoration.
    """
    weights = weights or DEFAULT_WEIGHTS
    total_weight = sum(weights.get(axis, 0.0) for axis in AXES)

    measured: List[str] = []
    unmeasured: List[str] = []
    weighted_score = 0.0
    weighted_confidence = 0.0
    used_weight = 0.0

    for axis in AXES:
        score = axes.get(axis)
        if score is None:
            unmeasured.append(axis)
            continue
        if not score.measured:
            unmeasured.append(axis)
            continue
        weight = weights.get(axis, 0.0)
        measured.append(axis)
        used_weight += weight
        weighted_score += weight * score.score
        weighted_confidence += weight * score.confidence

    if used_weight <= 0.0:
        return ScoreSet(score=0.0, confidence=0.0, measured_axes=[],
                        unmeasured_axes=unmeasured, weight_used=0.0)

    coverage = used_weight / total_weight if total_weight else 0.0
    return ScoreSet(
        score=weighted_score / used_weight,
        # Confidence is the axes' own confidence discounted by how much of the
        # picture was measurable. Three axes agreeing strongly is still only a
        # partial view, and the number should say so.
        confidence=(weighted_confidence / used_weight) * coverage,
        measured_axes=measured,
        unmeasured_axes=unmeasured,
        weight_used=coverage,
    )


# ---------------------------------------------------------------------------
# Across viewpoints and rooms
# ---------------------------------------------------------------------------


def merge_axes(sources: Sequence[Dict[str, AxisScore]]) -> Dict[str, AxisScore]:
    """Combine the same axis measured across several viewpoints.

    An axis is measured for the group if it was measured anywhere; its score
    is the confidence-weighted mean of the places it *was* measured. A room
    with three good views and one where the reference photograph is missing
    should score on the three, not be dragged down by the fourth.
    """
    merged: Dict[str, AxisScore] = {}

    for axis in AXES:
        instances = [s[axis] for s in sources if axis in s and s[axis].measured]
        if not instances:
            reasons = [s[axis].reason for s in sources
                       if axis in s and not s[axis].measured and s[axis].reason]
            merged[axis] = AxisScore.unmeasured(
                axis, reasons[0] if reasons else "not measured in any viewpoint"
            )
            continue

        weights = [max(0.05, instance.confidence) for instance in instances]
        total = sum(weights)
        merged[axis] = AxisScore(
            axis=axis,
            score=sum(i.score * w for i, w in zip(instances, weights)) / total,
            measured=True,
            confidence=sum(i.confidence for i in instances) / len(instances),
            detail={
                "sources": len(instances),
                "scores": [round(i.score, 4) for i in instances],
                "spread": round(max(i.score for i in instances)
                                - min(i.score for i in instances), 4),
            },
        )
    return merged


def weighted_mean(pairs: Iterable[Tuple[float, float]]) -> float:
    """``[(value, weight)]`` -> weighted mean, 0.0 when nothing has weight."""
    values = [(v, w) for v, w in pairs if w > 0]
    if not values:
        return 0.0
    total = sum(w for _, w in values)
    return sum(v * w for v, w in values) / total


def room_weight(room) -> float:
    """How much a room contributes to the building score.

    Floor area, because a large room dominates what the building looks like.
    Rooms with no recorded area fall back to 1.0 so they still count — a room
    the segmentation could not measure is not a room that does not matter.
    """
    if room is None:
        return 1.0
    area = float(getattr(room, "area", 0.0) or 0.0)
    return area if area > 0.1 else 1.0


# ---------------------------------------------------------------------------
# Subsystem pressure
# ---------------------------------------------------------------------------


def subsystem_pressure(findings: Sequence[Finding]) -> Dict[str, float]:
    """Which subsystem the evidence points at hardest.

    The sum of severity times confidence per subsystem, which answers the only
    question a refinement pass actually has: *what should I change first?* A
    single severe finding and five mild ones can reach the same total, and
    that is the intended behaviour — five rooms slightly too dark is a
    lighting problem exactly as much as one room badly so.
    """
    pressure: Dict[str, float] = {}
    for finding in findings:
        if not finding.subsystem:
            continue
        pressure[finding.subsystem] = pressure.get(finding.subsystem, 0.0) + (
            finding.severity * finding.confidence
        )
    return pressure


def coverage(total_viewpoints: int, evaluated: int, with_reference: int,
             passes_seen: Sequence[str]) -> Dict[str, Any]:
    """How much of the reconstruction the evaluation could actually see.

    Reported alongside every building score because the two are meaningless
    apart: a 0.95 drawn from one of nine viewpoints is not a good building,
    it is an unexamined one.
    """
    return {
        "viewpoints_total": int(total_viewpoints),
        "viewpoints_evaluated": int(evaluated),
        "viewpoints_with_reference": int(with_reference),
        "reference_coverage": (
            round(with_reference / total_viewpoints, 3) if total_viewpoints else 0.0
        ),
        "passes_available": sorted(set(passes_seen)),
    }


def top_findings(collected: Sequence[Finding], limit: int = 12) -> List[Finding]:
    """The findings worth acting on first, deduplicated across viewpoints."""
    return rank(merge(collected), limit=limit)
