"""
ArchX3D — Evaluation vocabulary
===============================
The types the evaluation engine produces. Everything downstream — the JSON
files, the HTML report, and whatever refinement pass comes next — reads only
these.

Why a finding is not a score
----------------------------
"Colour similarity: 0.71" is unactionable. It does not say which colour, in
which room, decided by which subsystem, or what to do about it. A refinement
pass given only that number can do nothing but guess.

A :class:`Finding` is the unit this engine actually exists to produce. It
names one difference, quantifies it, says *why* it believes that, attaches the
objects and materials involved, and nominates the subsystem that owns the fix.
Scores remain — they are how a run is compared with the run before it — but
they are a summary of the findings rather than the product.

Traceability
------------
Every score carries the measurements behind it (``AxisScore.detail``) and
every finding carries its evidence. A number that cannot be explained is a
number nobody can act on, and one that silently changed meaning between runs
is worse than none.

Unmeasured is not zero
----------------------
An axis that could not be measured — no reference photograph, no albedo pass,
Pillow not installed — reports ``measured=False`` and is *excluded from
normalisation*. Scoring it zero would say "the reconstruction is wrong here",
which is a different and false claim; excluding it says "this was not
assessed", which is true, and the confidence figure carries the cost.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------

COLOUR = "colour"
MATERIAL = "material"
LIGHTING = "lighting"
LAYOUT = "layout"
OBJECTS = "objects"

#: Evaluated independently, and reported independently. The order is the order
#: they appear in every report.
AXES = (COLOUR, MATERIAL, LIGHTING, LAYOUT, OBJECTS)

#: How much each axis contributes to a combined score.
#:
#: Objects weigh most because a missing sofa is a reconstruction failure while
#: a wall two shades too warm is a finish error — one is a different room, the
#: other is the same room mis-tinted. Material weighs least because it is the
#: axis measured most indirectly: a photograph has no albedo, so the material
#: comparison is the one making the largest inference.
DEFAULT_WEIGHTS: Dict[str, float] = {
    COLOUR: 0.20,
    MATERIAL: 0.15,
    LIGHTING: 0.20,
    LAYOUT: 0.20,
    OBJECTS: 0.25,
}


# ---------------------------------------------------------------------------
# Subsystems
# ---------------------------------------------------------------------------


class Subsystem:
    """Who owns the fix. Each name is a real, addressable part of ArchX3D.

    The value of a finding is mostly in this field: it converts "the render is
    too dark" into "change the LightingEnvironment", which is a thing someone
    can go and do. Inventing categories that do not correspond to code would
    make the whole exercise decorative, so every constant here names a module
    or a schema type that exists.
    """

    #: ``schema.LightingEnvironment`` — room-scale brightness, warmth, daylight.
    LIGHTING_ENVIRONMENT = "LightingEnvironment"
    #: ``schema.LightSource`` — the individual luminaires.
    LIGHT_SOURCE = "LightSource"
    #: ``schema.ColourPalette`` — the room's colour roles.
    COLOUR_PALETTE = "ColourPalette"
    #: ``blender.materials`` species vocabulary — walnut vs oak vs laminate.
    MATERIAL_SPECIES = "MaterialSpecies"
    #: ``schema.Finish`` on a wall, floor or ceiling.
    SURFACE_FINISH = "SurfaceFinish"
    #: ``vision.assets`` choice and ``blender_furniture`` construction.
    ASSET_PLACEMENT = "AssetPlacement"
    #: ``SceneObject.position`` / ``rotation_z`` / ``dimensions``.
    SCENE_GRAPH_TRANSFORM = "SceneGraphTransform"
    #: The vision pass's confidence in a detection.
    OBJECT_DETECTION = "ObjectDetection"
    #: ``schema.ViewPoint`` — the fitted camera itself.
    CAMERA_FIT = "CameraFit"
    #: DXF extraction and wall construction.
    GEOMETRY = "Geometry"
    #: The preview pipeline's own settings.
    RENDER_SETTINGS = "RenderSettings"

    ALL = (
        LIGHTING_ENVIRONMENT, LIGHT_SOURCE, COLOUR_PALETTE, MATERIAL_SPECIES,
        SURFACE_FINISH, ASSET_PLACEMENT, SCENE_GRAPH_TRANSFORM,
        OBJECT_DETECTION, CAMERA_FIT, GEOMETRY, RENDER_SETTINGS,
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One named, quantified, attributable difference.

    ``difference`` is the headline number a human reads; ``unit`` says what it
    is in (``""`` for a normalised 0–1 quantity, ``"m"`` for metres, ``"dE"``
    for a CIE colour difference). ``severity`` is separate and always 0–1: a
    38 cm shift and a 0.42 brightness gap are not comparable in their own
    units, but their severities are, and that is what ranking needs.
    """

    axis: str
    summary: str
    subsystem: str

    #: What *kind* of finding this is, within its axis: ``exposure``,
    #: ``warmth``, ``displacement``, ``missing``. A short stable slug, and the
    #: only thing that distinguishes two findings about the same subsystem in
    #: the same room.
    #:
    #: Without it, "the render is darker" and "the light is warmer" share an
    #: identity and the more severe silently swallows the other — which loses
    #: a real, separately actionable fact and makes the room look like it has
    #: one problem instead of two.
    code: str = ""

    #: The measured quantity this finding is about.
    difference: float = 0.0
    unit: str = ""
    #: 0 (cosmetic) to 1 (the reconstruction is wrong here). Drives ranking.
    severity: float = 0.0
    #: How much to trust this finding, given the evidence available.
    confidence: float = 0.5

    #: Why the engine believes it — the reasoning, not the number.
    why: str = ""
    #: The measurements behind it, so the claim can be checked.
    evidence: Dict[str, Any] = field(default_factory=dict)
    #: What to change. Concrete where possible.
    remedy: str = ""

    #: Scope. Empty room/viewpoint means the finding is building-wide.
    room: str = ""
    viewpoint: str = ""
    objects: List[str] = field(default_factory=list)
    materials: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "code": self.code,
            "summary": self.summary,
            "subsystem": self.subsystem,
            "difference": _round(self.difference, 4),
            "unit": self.unit,
            "severity": _round(self.severity, 3),
            "confidence": _round(self.confidence, 3),
            "why": self.why,
            "evidence": _round_values(self.evidence),
            "remedy": self.remedy,
            "room": self.room,
            "viewpoint": self.viewpoint,
            "objects": list(self.objects),
            "materials": list(self.materials),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Finding":
        return Finding(
            axis=str(d.get("axis", "")),
            code=str(d.get("code", "")),
            summary=str(d.get("summary", "")),
            subsystem=str(d.get("subsystem", "")),
            difference=float(d.get("difference", 0.0) or 0.0),
            unit=str(d.get("unit", "")),
            severity=float(d.get("severity", 0.0) or 0.0),
            confidence=float(d.get("confidence", 0.5) or 0.0),
            why=str(d.get("why", "")),
            evidence=dict(d.get("evidence") or {}),
            remedy=str(d.get("remedy", "")),
            room=str(d.get("room", "")),
            viewpoint=str(d.get("viewpoint", "")),
            objects=list(d.get("objects") or []),
            materials=list(d.get("materials") or []),
        )

    @property
    def key(self) -> str:
        """Identity for de-duplication across viewpoints of the same room.

        Two viewpoints of one room both seeing a too-dark render are one
        finding about the room, not two. But two *different* complaints about
        the same room's lighting are two findings, and the affected entities
        alone cannot tell them apart — which is what ``code`` is for.

        The room is part of it for the same reason: two rooms that are both
        too dark are two problems with two separate lighting environments, and
        merging them would hide one of them behind the other.

        Deliberately excludes the summary: summaries embed measured numbers
        ("sits 62 cm from"), so keying on them would stop the same finding
        merging across viewpoints, which is the behaviour this exists for.
        """
        scope = ",".join(sorted(self.objects) + sorted(self.materials))
        return f"{self.axis}|{self.subsystem}|{self.code}|{self.room}|{scope}"


def rank(findings: Iterable[Finding], limit: Optional[int] = None) -> List[Finding]:
    """Most severe first, ties broken deterministically.

    Severity alone leaves ties, and a report whose order changes between runs
    of an unchanged scene is one nobody can diff. Confidence then summary text
    settles it.
    """
    ordered = sorted(
        findings,
        key=lambda f: (-round(f.severity, 6), -round(f.confidence, 6), f.axis, f.summary),
    )
    return ordered[:limit] if limit else ordered


def merge(findings: Sequence[Finding]) -> List[Finding]:
    """Collapse findings that say the same thing about the same scope.

    Keeps the most severe instance and records how many viewpoints agreed —
    corroboration is information, and three viewpoints reporting the same dark
    room is a stronger signal than one.
    """
    grouped: Dict[str, List[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.key, []).append(finding)

    merged: List[Finding] = []
    for group in grouped.values():
        worst = rank(group)[0]
        if len(group) > 1:
            worst = Finding(**{**asdict(worst)})
            worst.evidence = dict(worst.evidence)
            worst.evidence["seen_in_viewpoints"] = sorted(
                {f.viewpoint for f in group if f.viewpoint}
            )
            worst.evidence["agreeing_viewpoints"] = len(group)
            # Corroboration raises confidence but never to certainty: several
            # views of one room share a light rig, so they are not independent.
            worst.confidence = min(0.99, worst.confidence + 0.05 * (len(group) - 1))
            worst.viewpoint = ""
        merged.append(worst)
    return rank(merged)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


@dataclass
class AxisScore:
    """One axis's verdict, and everything needed to defend it."""

    axis: str
    #: 0 (nothing in common) to 1 (indistinguishable). Meaningless when
    #: ``measured`` is False.
    score: float = 0.0
    #: False when the inputs for this axis were not available.
    measured: bool = False
    confidence: float = 0.0
    #: Why it could not be measured. Empty when it was.
    reason: str = ""
    #: The raw measurements, kept so a score can be checked rather than
    #: believed.
    detail: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def unmeasured(axis: str, reason: str) -> "AxisScore":
        return AxisScore(axis=axis, score=0.0, measured=False, confidence=0.0,
                         reason=reason)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "axis": self.axis,
            "measured": self.measured,
            "confidence": _round(self.confidence, 3),
        }
        if self.measured:
            out["score"] = _round(self.score, 4)
            out["detail"] = _round_values(self.detail)
        else:
            out["reason"] = self.reason
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AxisScore":
        return AxisScore(
            axis=str(d.get("axis", "")),
            score=float(d.get("score", 0.0) or 0.0),
            measured=bool(d.get("measured", False)),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            reason=str(d.get("reason", "")),
            detail=dict(d.get("detail") or {}),
        )


@dataclass
class ScoreSet:
    """A combined score over a set of axes, and how it was reached.

    Carries ``weight_used`` because the normalisation is the interesting part:
    a 0.9 over five axes and a 0.9 over two are different claims, and the
    second should not be able to masquerade as the first.
    """

    score: float = 0.0
    confidence: float = 0.0
    measured_axes: List[str] = field(default_factory=list)
    unmeasured_axes: List[str] = field(default_factory=list)
    #: Fraction of the total axis weight that was actually measurable.
    weight_used: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.unmeasured_axes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": _round(self.score, 4),
            "confidence": _round(self.confidence, 3),
            "measured_axes": list(self.measured_axes),
            "unmeasured_axes": list(self.unmeasured_axes),
            "weight_used": _round(self.weight_used, 3),
        }


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


@dataclass
class ViewpointEvaluation:
    """One reference photograph against the preview rendered from its camera."""

    viewpoint_id: str = ""
    room: str = ""
    #: Absolute or project-relative paths, as given to the engine.
    reference: str = ""
    render: str = ""
    #: Which auxiliary passes were available and used.
    passes_used: List[str] = field(default_factory=list)
    axes: Dict[str, AxisScore] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    totals: ScoreSet = field(default_factory=ScoreSet)
    #: Provenance carried through from the manifest, so an evaluation can be
    #: matched to the exact render it scored.
    scene_hash: str = ""
    camera_hash: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.totals.score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewpoint_id": self.viewpoint_id,
            "room": self.room,
            "reference": self.reference,
            "render": self.render,
            "passes_used": list(self.passes_used),
            "totals": self.totals.to_dict(),
            "axes": {name: self.axes[name].to_dict() for name in AXES if name in self.axes},
            "findings": [f.to_dict() for f in rank(self.findings)],
            "scene_hash": self.scene_hash,
            "camera_hash": self.camera_hash,
            "notes": list(self.notes),
        }


@dataclass
class RoomEvaluation:
    """Every viewpoint of one room, plus the room-level object comparison."""

    room_id: str = ""
    room_type: str = ""
    style: str = ""
    viewpoint_ids: List[str] = field(default_factory=list)
    axes: Dict[str, AxisScore] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    totals: ScoreSet = field(default_factory=ScoreSet)
    notes: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.totals.score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_type": self.room_type,
            "style": self.style,
            "viewpoint_ids": list(self.viewpoint_ids),
            "totals": self.totals.to_dict(),
            "axes": {name: self.axes[name].to_dict() for name in AXES if name in self.axes},
            "findings": [f.to_dict() for f in rank(self.findings)],
            "notes": list(self.notes),
        }


@dataclass
class BuildingSummary:
    """The whole reconstruction in one record."""

    totals: ScoreSet = field(default_factory=ScoreSet)
    axes: Dict[str, AxisScore] = field(default_factory=dict)
    room_scores: Dict[str, float] = field(default_factory=dict)
    #: The findings worth acting on first, across every room.
    findings: List[Finding] = field(default_factory=list)
    #: How much of the building the evaluation could actually see.
    coverage: Dict[str, Any] = field(default_factory=dict)
    #: Which subsystem the evidence points at most, and how strongly.
    subsystem_pressure: Dict[str, float] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.totals.score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "totals": self.totals.to_dict(),
            "axes": {name: self.axes[name].to_dict() for name in AXES if name in self.axes},
            "room_scores": {k: _round(v, 4) for k, v in sorted(self.room_scores.items())},
            "coverage": _round_values(self.coverage),
            "subsystem_pressure": {
                k: _round(v, 3)
                for k, v in sorted(self.subsystem_pressure.items(),
                                   key=lambda kv: (-kv[1], kv[0]))
            },
            "findings": [f.to_dict() for f in rank(self.findings)],
        }


@dataclass
class EvaluationResult:
    """What :func:`evaluation.evaluate` returns, and what gets written out.

    The engine never touches the scene graph. This object is the only thing it
    produces, and it is entirely derived: run it twice on the same inputs and
    you get the same numbers.
    """

    schema_version: str = SCHEMA_VERSION
    generated_at: str = ""
    building: BuildingSummary = field(default_factory=BuildingSummary)
    rooms: List[RoomEvaluation] = field(default_factory=list)
    viewpoints: List[ViewpointEvaluation] = field(default_factory=list)
    #: Versions, paths, weights and timings — enough to reproduce the run.
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Anything that limited the evaluation: missing references, absent passes.
    notes: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.building.totals.score

    @property
    def findings(self) -> List[Finding]:
        """Every finding, deduplicated and ranked."""
        collected: List[Finding] = []
        for viewpoint in self.viewpoints:
            collected.extend(viewpoint.findings)
        for room in self.rooms:
            collected.extend(room.findings)
        return merge(collected)

    def room(self, room_id: str) -> Optional[RoomEvaluation]:
        return next((r for r in self.rooms if r.room_id == room_id), None)

    def viewpoint(self, viewpoint_id: str) -> Optional[ViewpointEvaluation]:
        return next((v for v in self.viewpoints if v.viewpoint_id == viewpoint_id), None)

    def summary(self) -> str:
        if not self.viewpoints:
            return "nothing evaluated"
        totals = self.building.totals
        parts = [f"score {totals.score:.2f}",
                 f"confidence {totals.confidence:.2f}",
                 f"{len(self.viewpoints)} viewpoint(s)",
                 f"{len(self.findings)} finding(s)"]
        if totals.unmeasured_axes:
            parts.append(f"unmeasured: {', '.join(totals.unmeasured_axes)}")
        return ", ".join(parts)

    # -- the four documents -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """``evaluation.json`` — everything, in one document."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "metadata": self.metadata,
            "building": self.building.to_dict(),
            "rooms": [r.to_dict() for r in self.rooms],
            "viewpoints": [v.to_dict() for v in self.viewpoints],
            "findings": [f.to_dict() for f in self.findings],
            "notes": list(self.notes),
        }

    def building_document(self) -> Dict[str, Any]:
        """``building_summary.json`` — the top-level verdict alone."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "building": self.building.to_dict(),
            "notes": list(self.notes),
        }

    def room_document(self) -> Dict[str, Any]:
        """``per_room.json`` — one entry per room."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "rooms": [r.to_dict() for r in self.rooms],
        }

    def viewpoint_document(self) -> Dict[str, Any]:
        """``per_viewpoint.json`` — one entry per reference/render pair."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "viewpoints": [v.to_dict() for v in self.viewpoints],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round(value: Any, places: int) -> Any:
    """Round for output, leaving anything non-numeric alone.

    Rounding at the boundary rather than during measurement keeps the maths
    honest and the JSON diffable.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    # Counts stay integers. Rounding turns 2 into 2.0, which reads as a
    # measurement rather than a tally and makes the documents needlessly
    # harder to diff.
    if isinstance(value, int):
        return value
    if not math.isfinite(value):
        return None
    return round(float(value), places)


def _round_values(payload: Any, places: int = 4) -> Any:
    if isinstance(payload, dict):
        return {k: _round_values(v, places) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_round_values(v, places) for v in payload]
    return _round(payload, places)
