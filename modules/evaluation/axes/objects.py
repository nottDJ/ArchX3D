"""
ArchX3D — Object axis
=====================
What the reference images were observed to contain, against what the pipeline
actually built. **Measured from the scene graph, never from the render.**

Why not detect objects in the render
------------------------------------
Running a detector over the preview would compare one model's opinion of the
render with another model's opinion of the photograph. Two sources of error,
neither observable, and a "missing sofa" that might just be a detector having
a bad day.

The graph already knows. The vision pass recorded every object it saw and how
sure it was; the generator recorded which of those it built and which it
withheld. The difference is exact, free, and — unlike a detection — it comes
with the *reason*: an object below the confidence floor was deliberately
omitted, and that is a policy decision someone can revisit.

The four ways a reconstruction diverges
---------------------------------------
``missing``    observed, not built. The reason is recoverable: below the
               confidence threshold, degenerate dimensions, or unbuildable.
``extra``      built without a corresponding observation. Rare by
               construction, but a placement bug can duplicate an object.
``replaced``   built, but as something other than what was detected — a
               chaise standing in for a sectional because no closer asset
               existed. Half a match, and scored as such.
``omitted``    withheld deliberately by the confidence policy. Counted with
               ``missing`` but reported separately, because the fix is a
               threshold rather than a detection.

Scope
-----
Per viewpoint, only objects the vision pass recorded *in that photograph* are
considered — an object behind the camera is not missing from the shot. Per
room, every object in the room counts, which is where a genuinely absent
object surfaces even if no single viewpoint framed it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import imaging
from ..schema import OBJECTS, AxisScore, Finding, Subsystem

#: Below this the pipeline withholds an object; mirrors
#: ``vision.schema.ConfidencePolicy.ACCEPT``. Read from the graph's own policy
#: at runtime when available, so the two cannot drift apart.
DEFAULT_ACCEPT = 0.65

#: An asset chosen with a score below this is a stand-in rather than a match.
ASSET_SUBSTITUTE = 0.5


def evaluate(ctx, scope: str = "viewpoint") -> Tuple[AxisScore, List[Finding]]:
    """Compare detected objects with built ones.

    ``scope`` selects which objects are in question: ``"viewpoint"`` restricts
    to what this photograph recorded, ``"room"`` takes the whole room.
    """
    if ctx.graph is None:
        return AxisScore.unmeasured(OBJECTS, "no scene graph"), []

    observed = _observed(ctx, scope)
    if not observed:
        return AxisScore.unmeasured(
            OBJECTS,
            "no objects were recorded against this "
            + ("viewpoint" if scope == "viewpoint" else "room"),
        ), []

    built = {obj.id for obj in ctx.graph.buildable_objects(include_uncertain=False)}

    missing: List[Any] = []
    replaced: List[Any] = []
    matched: List[Any] = []
    for obj in observed:
        if obj.id not in built:
            missing.append(obj)
        elif _is_substitute(obj):
            replaced.append(obj)
        else:
            matched.append(obj)

    extra = _extra(ctx, observed, built, scope)

    total = float(len(observed))
    # A substitution is half a failure: the object is there, at the right
    # place and size, wearing the wrong shape. Scoring it as a total miss
    # would make "sofa built as a box" indistinguishable from "no sofa".
    penalty = (len(missing) + 0.5 * len(replaced) + len(extra)) / max(total, 1.0)
    score = imaging.clamp01(1.0 - penalty)

    detail: Dict[str, Any] = {
        "scope": scope,
        "observed": int(total),
        "built": len(matched) + len(replaced),
        "missing": [_describe(o) for o in missing],
        "replaced": [_describe(o) for o in replaced],
        "extra": [_describe(o) for o in extra],
    }

    findings: List[Finding] = []
    findings.extend(_missing_findings(ctx, missing, total))
    findings.extend(_replaced_findings(ctx, replaced))
    findings.extend(_extra_findings(ctx, extra))

    # The graph is exact about this, so confidence is high — but it is
    # confidence in the comparison, not in the vision pass that produced the
    # observations, which is why it is not 1.0.
    return AxisScore(axis=OBJECTS, score=score, measured=True, confidence=0.9,
                     detail=detail), findings


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _observed(ctx, scope: str) -> List[Any]:
    if scope == "room":
        return [o for o in ctx.graph.objects if o.room_id == ctx.room_id]
    return ctx.observed_objects()


def _extra(ctx, observed: List[Any], built: set, scope: str) -> List[Any]:
    """Built objects with no observation backing them.

    Only meaningful at room scope: at viewpoint scope an object legitimately
    built for the room but not recorded in *this* photograph is not extra, it
    is out of shot, and reporting it would produce a finding per viewpoint for
    every correctly built object.
    """
    if scope != "room":
        return []
    observed_ids = {o.id for o in observed}
    return [
        obj for obj in ctx.graph.objects
        if obj.id in built and obj.room_id == ctx.room_id and obj.id not in observed_ids
    ]


def _is_substitute(obj) -> bool:
    """Whether what was built stands in for what was detected."""
    if not getattr(obj, "asset", ""):
        return False
    return float(getattr(obj, "asset_score", 0.0) or 0.0) < ASSET_SUBSTITUTE


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _missing_findings(ctx, missing: List[Any], total: float) -> List[Finding]:
    findings = []
    for obj in missing:
        reason, subsystem, remedy = _why_missing(ctx, obj)
        label = _label(obj)
        findings.append(Finding(
            axis=OBJECTS,
            code="missing",
            summary=f"{label.capitalize()} omitted",
            subsystem=subsystem,
            difference=1.0,
            severity=imaging.clamp01(0.4 + 0.6 / max(total, 1.0)) * _weight(obj),
            confidence=0.9,
            why=reason,
            evidence=_describe(obj),
            remedy=remedy,
            room=obj.room_id or ctx.room_id,
            viewpoint=ctx.viewpoint_id,
            objects=[obj.id],
        ))
    return findings


def _why_missing(ctx, obj) -> Tuple[str, str, str]:
    """The recoverable reason an observed object was not built."""
    threshold = _accept_threshold(ctx)
    confidence = float(getattr(obj, "confidence", 0.0) or 0.0)

    if getattr(obj, "uncertain", False) or confidence < threshold:
        return (
            f"detected with confidence {confidence:.2f}, below the {threshold:.2f} "
            f"acceptance threshold, so the generator withheld it — the pipeline "
            f"prefers an omission to a guess",
            Subsystem.OBJECT_DETECTION,
            f"raise the detection confidence for this object, or build with "
            f"--include-uncertain to accept detections above "
            f"{getattr(ctx.config, 'review_threshold', 0.4):.2f}",
        )

    if obj.dimensions.is_degenerate():
        return (
            f"dimensions {obj.dimensions.to_dict()} include a zero extent, so "
            f"there is nothing to build",
            Subsystem.ASSET_PLACEMENT,
            "supply plausible dimensions for this category, or let the "
            "catalogue's defaults fill them in",
        )

    return (
        "recorded in the graph but absent from the built set for a reason the "
        "graph does not state",
        Subsystem.ASSET_PLACEMENT,
        "check the generator log for this object id",
    )


def _replaced_findings(ctx, replaced: List[Any]) -> List[Finding]:
    findings = []
    for obj in replaced:
        score = float(getattr(obj, "asset_score", 0.0) or 0.0)
        findings.append(Finding(
            axis=OBJECTS,
            code="substitute",
            summary=f"{_label(obj).capitalize()} built from a stand-in asset",
            subsystem=Subsystem.ASSET_PLACEMENT,
            difference=1.0 - score,
            severity=imaging.clamp01(1.0 - score) * 0.5,
            confidence=0.8,
            why=f"the closest available asset was {obj.asset!r}, matched at "
                f"{score:.2f} — below the {ASSET_SUBSTITUTE:.2f} mark at which "
                f"a match stops resembling what was detected",
            evidence=_describe(obj),
            remedy=f"add an asset for the {obj.category!r} category, or relax "
                   f"the proportions the matcher scores against",
            room=obj.room_id or ctx.room_id,
            viewpoint=ctx.viewpoint_id,
            objects=[obj.id],
        ))
    return findings


def _extra_findings(ctx, extra: List[Any]) -> List[Finding]:
    findings = []
    for obj in extra:
        findings.append(Finding(
            axis=OBJECTS,
            code="extra",
            summary=f"{_label(obj).capitalize()} built without an observation",
            subsystem=Subsystem.ASSET_PLACEMENT,
            difference=1.0,
            severity=0.5,
            confidence=0.7,
            why="the object is in the built set and assigned to this room, but "
                "no reference image recorded it",
            evidence=_describe(obj),
            remedy="check whether this object was duplicated during fusion",
            room=obj.room_id or ctx.room_id,
            objects=[obj.id],
        ))
    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _accept_threshold(ctx) -> float:
    """The generator's own acceptance threshold, not a copy of it.

    Read from ``ConfidencePolicy`` so a change there cannot leave this module
    quietly explaining omissions against a stale number.
    """
    try:
        from vision.schema import ConfidencePolicy

        return float(ConfidencePolicy.ACCEPT)
    except (ImportError, AttributeError):
        return DEFAULT_ACCEPT


def _weight(obj) -> float:
    """How much this object's absence matters.

    A missing sectional changes the room; a missing vase does not. Footprint
    is the available proxy, and the ``group`` field sharpens it — decor is
    decoration by definition.
    """
    area = float(getattr(obj.dimensions, "footprint_area", 0.0) or 0.0)
    base = imaging.clamp01(0.35 + area / 3.0)
    if getattr(obj, "group", "") == "decor":
        base *= 0.6
    return base


def _label(obj) -> str:
    category = str(getattr(obj, "category", "") or "").replace("_", " ").strip()
    return category or str(getattr(obj, "id", "object"))


def _describe(obj) -> Dict[str, Any]:
    return {
        "id": obj.id,
        "category": obj.category,
        "label": getattr(obj, "label", ""),
        "confidence": round(float(getattr(obj, "confidence", 0.0) or 0.0), 3),
        "uncertain": bool(getattr(obj, "uncertain", False)),
        "asset": getattr(obj, "asset", ""),
        "asset_score": round(float(getattr(obj, "asset_score", 0.0) or 0.0), 3),
        "room": obj.room_id,
    }
