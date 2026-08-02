"""
ArchX3D — The planner's view of evaluation findings
===================================================
The **only** module that reads an :class:`evaluation.schema.EvaluationResult`.

Why the boundary exists
-----------------------
The optimiser must never consume raw findings. A finding is an *observation* —
"the render is 0.42 darker than the reference" — and an optimiser fed
observations directly ends up re-deriving intent from prose at the moment it is
about to mutate a scene graph. That is the wrong place to be making
judgements: three findings that share one cause become three edits, each
measured against the others' side effects, and the loop thrashes.

So findings stop here. Everything downstream of this module works in
:class:`planner.action_graph.Action` objects, which state what to change rather
than what is wrong. The evaluation engine can change its wording, add axes, or
re-scale a severity without any of it reaching the optimiser.

What this module does
---------------------
Normalises the result into a :class:`FindingSet`: deduplicated, indexed by the
things a planner needs to group on — subsystem, room, object, material — and
carrying the evaluation's own scores so gain can be estimated against them.

It does not decide anything. Grouping is :mod:`planner.grouping`, estimation is
:mod:`planner.ranking`. This is the adapter, and keeping it that thin is what
makes the boundary worth having.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class PlannedFinding:
    """One evaluation finding, in the form the planner works with.

    A frozen copy rather than a reference: the planner must not be able to
    mutate the evaluation's output, and a value type makes the grouping code
    obviously side-effect free.

    ``key`` is stable across runs of an unchanged build, which is what lets a
    history file from yesterday be compared with a plan from today.
    """

    key: str
    axis: str
    #: The kind of finding within its axis — ``exposure``, ``warmth``,
    #: ``displacement``. What tells two complaints about one room apart.
    code: str
    subsystem: str
    summary: str
    severity: float
    confidence: float
    difference: float
    unit: str
    why: str
    remedy: str
    room: str
    viewpoint: str
    objects: Tuple[str, ...]
    materials: Tuple[str, ...]
    evidence: Dict[str, Any]

    @property
    def weight(self) -> float:
        """Severity discounted by confidence — how much this finding argues.

        Both matter and neither substitutes for the other: a severe finding
        the engine is unsure of should not outrank a moderate one it is
        certain about, and multiplying is the simplest combination that says
        so.
        """
        return self.severity * self.confidence

    @property
    def scope(self) -> str:
        """The narrowest thing this finding is about.

        Object findings are about objects, material findings about materials,
        the rest about a room. Grouping keys off this, so it decides whether
        two findings are candidates for the same action.
        """
        if self.objects:
            return f"object:{self.objects[0]}"
        if self.materials:
            return f"material:{self.materials[0]}"
        return f"room:{self.room}" if self.room else "building"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "axis": self.axis,
            "code": self.code,
            "subsystem": self.subsystem,
            "summary": self.summary,
            "severity": round(self.severity, 4),
            "confidence": round(self.confidence, 4),
            "difference": round(self.difference, 4),
            "unit": self.unit,
            "room": self.room,
            "viewpoint": self.viewpoint,
            "objects": list(self.objects),
            "materials": list(self.materials),
        }


@dataclass
class FindingSet:
    """Every finding from one evaluation, indexed for grouping.

    Also carries the scores, because a planner has to know what it is trying
    to improve: an axis already at 0.95 has almost nothing to gain, and
    proposing an expensive action against it wastes an iteration.
    """

    findings: List[PlannedFinding] = field(default_factory=list)
    #: Building score at the time of evaluation.
    baseline_score: float = 0.0
    #: Per-axis score, for axes that were measured.
    axis_scores: Dict[str, float] = field(default_factory=dict)
    #: Axes the evaluation could not measure. Actions aimed at an unmeasured
    #: axis cannot be verified, which the planner has to know before it
    #: proposes one.
    unmeasured_axes: Tuple[str, ...] = ()
    #: Room id -> that room's score.
    room_scores: Dict[str, float] = field(default_factory=dict)
    #: Subsystem -> accumulated severity x confidence, straight from the
    #: evaluation's own aggregation.
    subsystem_pressure: Dict[str, float] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @staticmethod
    def from_evaluation(result) -> "FindingSet":
        """Adapt an :class:`EvaluationResult`. The boundary, in one function.

        ``result.findings`` is already deduplicated and ranked by the
        evaluation engine, so this does not repeat that work — it converts and
        indexes. Re-merging here would risk the two implementations drifting
        apart and disagreeing about what counts as the same finding.
        """
        building = getattr(result, "building", None)
        axis_scores: Dict[str, float] = {}
        unmeasured: List[str] = []
        for axis, score in (getattr(building, "axes", {}) or {}).items():
            if getattr(score, "measured", False):
                axis_scores[axis] = float(score.score)
            else:
                unmeasured.append(axis)

        return FindingSet(
            findings=[_convert(f) for f in (result.findings or [])],
            baseline_score=float(getattr(result, "score", 0.0) or 0.0),
            axis_scores=axis_scores,
            unmeasured_axes=tuple(sorted(unmeasured)),
            room_scores=dict(getattr(building, "room_scores", {}) or {}),
            subsystem_pressure=dict(getattr(building, "subsystem_pressure", {}) or {}),
        )

    # -- queries ------------------------------------------------------------

    def by_subsystem(self, subsystem: str) -> List[PlannedFinding]:
        return [f for f in self.findings if f.subsystem == subsystem]

    def by_room(self, room: str) -> List[PlannedFinding]:
        return [f for f in self.findings if f.room == room]

    def for_object(self, object_id: str) -> List[PlannedFinding]:
        return [f for f in self.findings if object_id in f.objects]

    def rooms(self) -> List[str]:
        """Every room any finding mentions, in a stable order."""
        return sorted({f.room for f in self.findings if f.room})

    def axis_headroom(self, axis: str) -> float:
        """How much this axis could still gain, in ``[0, 1]``.

        An unmeasured axis reports zero headroom rather than a full point of
        it. The optimiser verifies every change by re-evaluating, so an action
        aimed at something nothing can measure is an action whose outcome is
        unknowable — and proposing it would burn an iteration to learn
        nothing.
        """
        if axis in self.unmeasured_axes:
            return 0.0
        return max(0.0, 1.0 - self.axis_scores.get(axis, 1.0))

    def total_weight(self) -> float:
        return sum(f.weight for f in self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseline_score": round(self.baseline_score, 4),
            "axis_scores": {k: round(v, 4) for k, v in sorted(self.axis_scores.items())},
            "unmeasured_axes": list(self.unmeasured_axes),
            "room_scores": {k: round(v, 4) for k, v in sorted(self.room_scores.items())},
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _convert(finding) -> PlannedFinding:
    return PlannedFinding(
        key=_stable_key(finding),
        axis=str(getattr(finding, "axis", "")),
        code=str(getattr(finding, "code", "")),
        subsystem=str(getattr(finding, "subsystem", "")),
        summary=str(getattr(finding, "summary", "")),
        severity=float(getattr(finding, "severity", 0.0) or 0.0),
        confidence=float(getattr(finding, "confidence", 0.0) or 0.0),
        difference=float(getattr(finding, "difference", 0.0) or 0.0),
        unit=str(getattr(finding, "unit", "")),
        why=str(getattr(finding, "why", "")),
        remedy=str(getattr(finding, "remedy", "")),
        room=str(getattr(finding, "room", "")),
        viewpoint=str(getattr(finding, "viewpoint", "")),
        objects=tuple(getattr(finding, "objects", ()) or ()),
        materials=tuple(getattr(finding, "materials", ()) or ()),
        evidence=dict(getattr(finding, "evidence", {}) or {}),
    )


def _stable_key(finding) -> str:
    """An identifier that survives re-evaluation of an unchanged build.

    Built from what the finding is *about* rather than from its wording or its
    position in a list: a summary can be rephrased and a rank can move, but the
    axis, subsystem and affected entities are the finding's identity. The
    history file relies on this to say "this is the same problem as last time".
    """
    scope = ",".join(sorted(getattr(finding, "objects", ()) or ())
                     + sorted(getattr(finding, "materials", ()) or ()))
    parts = [
        str(getattr(finding, "axis", "")),
        str(getattr(finding, "subsystem", "")),
        str(getattr(finding, "code", "")),
        str(getattr(finding, "room", "")),
        scope,
    ]
    return "|".join(parts)
