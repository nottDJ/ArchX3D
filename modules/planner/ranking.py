"""
ArchX3D — Expected gain, cost, and the order they imply
=======================================================
How much an action is likely to be worth, what it will cost to find out, and
which to try first.

What an estimate is for
-----------------------
Nothing here predicts the future. The optimiser applies each action, re-renders
and re-evaluates, so the *actual* gain is always measured — an estimate that
turns out wrong costs one iteration and is recorded as such.

The estimate exists only to **order** the queue. That lowers the stakes
considerably: it has to be roughly right about which action is more promising,
not about how much either will achieve. Where the two disagree the measurement
wins, always, and :mod:`optimizer.metrics` reports the calibration error so a
systematically optimistic prior is visible rather than assumed.

Gain
----
Three factors multiply:

``evidence``   the group's severity x confidence — how loudly the findings
               argue for this change.
``headroom``   how much the axes it targets could still gain. An axis already
               at 0.97 has 0.03 to give, and an action aimed at it cannot beat
               one aimed at an axis sitting at 0.4 however severe its finding.
``efficacy``   a per-type prior: how much of what a finding measures this kind
               of action historically moves. Lighting adjustments move the
               lighting axis nearly one-for-one; a decor admission moves the
               object axis by one object out of many.

Cost
----
Measured in rebuild-and-evaluate cycles, because that is what an iteration
actually spends. A camera correction needs no Blender rebuild — the preview
renderer reconstructs cameras from the graph — so it costs a fraction of
everything else. That is not a rounding difference: it is roughly 3 s against
40 s, which is why cost is in the ranking at all.

Priority
--------
``gain / cost``, discounted by risk. A cheap action with a modest gain
genuinely should be tried before an expensive one with a slightly larger gain,
because the loop learns something either way and the cheap one leaves more
budget behind.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .action_graph import Action, ActionType, sort_key
from .findings import FindingSet

# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------

#: How much of the measured difference an action of each type typically
#: removes. These are priors, not measurements — the optimiser records actual
#: gains against them, and :mod:`optimizer.metrics` reports the error so they
#: can be revised from evidence rather than opinion.
#:
#: Lighting and palette changes act on every pixel of a room, so they move a
#: score furthest. Per-object actions move one object out of many. Camera
#: correction is high because a wrong camera corrupts an entire viewpoint's
#: layout measurement, so fixing it recovers all of it at once.
EFFICACY: Dict[str, float] = {
    ActionType.LIGHTING_ADJUSTMENT: 0.70,
    ActionType.PALETTE_ADJUSTMENT: 0.45,
    ActionType.MATERIAL_ADJUSTMENT: 0.50,
    ActionType.CAMERA_CORRECTION: 0.80,
    ActionType.FURNITURE_TRANSLATION: 0.55,
    ActionType.FURNITURE_ROTATION: 0.35,
    ActionType.FURNITURE_SCALE: 0.30,
    ActionType.ASSET_REPLACEMENT: 0.40,
    ActionType.ASSET_VARIANT_SWAP: 0.25,
    ActionType.DECOR_DENSITY: 0.45,
    ActionType.STYLE_REFINEMENT: 0.35,
}

#: Cost in rebuild-and-evaluate cycles.
BASE_COST: Dict[str, float] = {
    #: No Blender rebuild — the preview renderer reconstructs cameras from the
    #: graph, so only the render and evaluation are paid for.
    ActionType.CAMERA_CORRECTION: 0.15,
}
DEFAULT_COST = 1.0

#: Extra cost per affected object, for actions that touch several. A decor
#: admission that adds eight objects makes the rebuild measurably slower and
#: makes the outcome harder to attribute, and both deserve to be priced.
COST_PER_OBJECT = 0.03

#: How much of the plan's estimated gain to believe when several actions
#: target the same axis. Two lighting adjustments in two rooms do not each
#: deliver their full estimate against a single building score; the second is
#: discounted, the third more so.
OVERLAP_DECAY = 0.6

#: Actions the evaluation cannot verify are penalised rather than dropped —
#: they may still be right, but the loop will not be able to tell.
UNVERIFIABLE_PENALTY = 0.25


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def estimate_gain(action: Action, finding_set: FindingSet,
                  evidence: Optional[float] = None) -> float:
    """Expected similarity gain from one action, in ``[0, 1]``.

    ``evidence`` overrides the weight derived from the action's own confidence
    — the grouping layer already computed it from the findings, and passing it
    through avoids recomputing a slightly different number here.
    """
    strength = evidence if evidence is not None else action.confidence
    efficacy = EFFICACY.get(action.type, 0.3)
    headroom = _headroom(action, finding_set)

    gain = strength * efficacy * headroom
    if _unverifiable(action, finding_set):
        gain *= UNVERIFIABLE_PENALTY
    return max(0.0, min(1.0, gain))


def _headroom(action: Action, finding_set: FindingSet) -> float:
    """How much the axes this action targets could still gain.

    The maximum rather than the sum: an action that improves two axes is
    limited by the better opportunity of the two, and adding them would let a
    broad action claim more headroom than the score has to give.
    """
    axes = action.axes or []
    if not axes:
        # No axis attribution: fall back to the building's own headroom, which
        # is the most that any change could possibly deliver.
        return max(0.0, 1.0 - finding_set.baseline_score)
    return max(finding_set.axis_headroom(axis) for axis in axes)


def _unverifiable(action: Action, finding_set: FindingSet) -> bool:
    """Whether every axis this action targets went unmeasured.

    Such an action might be exactly right, and the loop would have no way to
    know: applying it changes a score that was not computed, so the accept/
    reject decision would be made on noise from the other axes.
    """
    axes = action.axes or []
    return bool(axes) and all(axis in finding_set.unmeasured_axes for axis in axes)


def estimate_cost(action: Action) -> float:
    """What trying this action will spend, in rebuild-and-evaluate cycles."""
    base = BASE_COST.get(action.type, DEFAULT_COST)
    return round(base + COST_PER_OBJECT * len(action.objects), 4)


def risk(action: Action) -> float:
    """How likely the estimate is to be wrong, in ``[0, 1]``.

    Low-confidence findings and actions touching many objects at once are both
    harder to predict — the first because the evidence is weak, the second
    because several effects land together and the measurement cannot separate
    them.
    """
    breadth = min(0.4, 0.05 * len(action.objects))
    return max(0.0, min(1.0, (1.0 - action.confidence) * 0.6 + breadth))


def priority(action: Action) -> float:
    """Ranking score: gain per unit cost, discounted by risk.

    Cost is added to rather than divided into 1 so that a free action does not
    produce an infinite score, and so the difference between 0.15 and 1.0
    cycles is meaningful without being overwhelming.
    """
    return (action.expected_gain * (1.0 - 0.5 * risk(action))) / (0.5 + action.cost)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def rank(actions: Sequence[Action], finding_set: FindingSet,
         evidence: Optional[Dict[str, float]] = None) -> List[Action]:
    """Estimate, score and order a set of actions in place.

    Returns the same objects, sorted. Mutating rather than copying because the
    plan holds one identity per action and the graph refers to them by id;
    two copies with different priorities would be a bug waiting to happen.
    """
    evidence = evidence or {}
    for action in actions:
        action.cost = estimate_cost(action)
        action.expected_gain = estimate_gain(action, finding_set,
                                             evidence.get(action.id))
        action.priority = round(priority(action), 6)
    return sorted(actions, key=sort_key)


def plan_gain(actions: Sequence[Action], finding_set: FindingSet) -> float:
    """Expected gain for a whole plan, with overlapping claims discounted.

    Summing the individual estimates would double-count: five actions each
    claiming to fix the lighting axis cannot together deliver five times its
    headroom. Later claims on the same axis decay, and the total is capped at
    the headroom that actually exists.
    """
    seen: Dict[str, int] = {}
    total = 0.0
    for action in sorted(actions, key=sort_key):
        axis = action.axes[0] if action.axes else "*"
        occurrences = seen.get(axis, 0)
        total += action.expected_gain * (OVERLAP_DECAY ** occurrences)
        seen[axis] = occurrences + 1
    ceiling = max(0.0, 1.0 - finding_set.baseline_score)
    return round(min(total, ceiling), 4)


def explain(action: Action, finding_set: FindingSet) -> Dict[str, Any]:
    """The estimate's working, so a ranking can be argued with."""
    return {
        "evidence_strength": round(action.confidence, 4),
        "efficacy_prior": EFFICACY.get(action.type, 0.3),
        "axis_headroom": round(_headroom(action, finding_set), 4),
        "unverifiable": _unverifiable(action, finding_set),
        "expected_gain": round(action.expected_gain, 4),
        "cost_cycles": round(action.cost, 4),
        "risk": round(risk(action), 4),
        "priority": round(action.priority, 6),
    }
