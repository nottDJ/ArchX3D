"""
ArchX3D — Layout axis
=====================
Is everything where the photograph says it should be — and when it is not, by
how many centimetres?

Two measurements, one image-space and one metric
------------------------------------------------
**Visual mass agreement.** Where the busy parts of the frame sit, on a coarse
grid, compared between reference and render. Uses local contrast rather than
brightness so it does not re-measure the exposure difference the lighting axis
already owns. This catches the gross case: a room whose furniture is all along
the wrong wall.

**Per-object displacement, in metres.** This is the finding the axis exists
for. It needs no detection in the render, because both halves are already
stored:

* ``SceneObject.bbox_2d`` — where the vision pass saw the object in the
  reference photograph;
* ``ViewPoint`` — the camera fitted to that photograph.

Back-projecting the box's bottom edge onto the floor gives the position the
*photograph* implies. The graph holds the position the pipeline actually
built. The distance between them is how far the object moved after detection —
collision resolution, relationship enforcement and wall snapping all push
objects around, and this is the only view of by how much.

That residual is attributable. Both numbers derive from the same photograph,
so anything between them was introduced downstream of the vision pass, which
is precisely what makes ``SceneGraphTransform`` the right subsystem to name.

Where the depth pass comes in
-----------------------------
Depth is not compared against the reference — a photograph has none. It is
used to describe the render's own spatial structure as evidence (how much of
the frame is near, mid and far), and to convert an image-space discrepancy
into metres when the analytic route is unavailable. Findings that would need
depth the reference does not have are not made.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .. import imaging
from ..projection import planar_distance
from ..schema import LAYOUT, AxisScore, Finding, Subsystem

#: Displacement worth a finding. 25 cm is about where a sofa stops looking
#: like the same sofa in the same place.
DISPLACEMENT_FINDING = 0.25

#: Displacement at which the object counts as entirely misplaced, for scoring.
DISPLACEMENT_SATURATION = 2.0

#: A back-projection landing further than this from the camera is treated as
#: unreliable: near the horizon a pixel of box-edge error becomes metres of
#: floor, and a finding built on it would be arithmetic noise.
MAX_BACKPROJECTION_RANGE = 12.0

#: Below this many measured objects, a shared offset cannot be distinguished
#: from two objects that happen to be wrong the same way.
MIN_OBJECTS_FOR_SYSTEMATIC = 3

#: Share of objects that must agree with the common offset before it is
#: blamed on the camera. At 0.7, three of four agreeing is enough and two of
#: four is not — which is the line between "the view is off" and "some things
#: moved".
COHERENCE_THRESHOLD = 0.7

#: How far an object may sit from the common offset and still count as
#: agreeing with it. Objects at different depths never move by exactly the
#: same amount under a camera error.
AGREEMENT_TOLERANCE = 0.25


def evaluate(ctx) -> Tuple[AxisScore, List[Finding]]:
    reason = ctx.missing()
    if reason:
        return AxisScore.unmeasured(LAYOUT, reason), []

    numpy, _ = imaging.backend()
    reference_grid = imaging.mass_grid(ctx.pair.reference)
    render_grid = imaging.mass_grid(ctx.pair.render)
    agreement = float(numpy.minimum(reference_grid, render_grid).sum())

    detail: Dict[str, Any] = {
        "mass_agreement": agreement,
        "mass_shift": _describe_shift(reference_grid, render_grid),
    }

    depth_profile = _depth_profile(ctx)
    if depth_profile is not None:
        detail["depth_profile"] = depth_profile

    displacements = _displacements(ctx)
    findings: List[Finding] = []
    if displacements:
        systematic = _systematic_offset(displacements)
        values = [d["residual"] for d in displacements]
        detail["displacement"] = {
            "measured_objects": len(values),
            "mean_m": sum(values) / len(values),
            "max_m": max(values),
            "raw_mean_m": sum(d["distance"] for d in displacements) / len(values),
            "systematic": systematic,
        }
        if systematic["systematic"]:
            findings.extend(_camera_finding(ctx, systematic, len(displacements)))
        findings.extend(_displacement_findings(ctx, displacements))

    # Mass agreement carries the score when nothing could be back-projected;
    # otherwise the metric evidence dominates, because it is the stronger
    # claim and the one a person can act on.
    if displacements:
        mean = detail["displacement"]["mean_m"]
        placement = 1.0 - imaging.clamp01(mean / DISPLACEMENT_SATURATION)
        score = imaging.clamp01(0.4 * agreement + 0.6 * placement)
        confidence = 0.8
    else:
        score = imaging.clamp01(agreement)
        confidence = 0.5
        detail["displacement"] = {"measured_objects": 0}

    findings.extend(_mass_finding(ctx, agreement, detail))
    return AxisScore(axis=LAYOUT, score=score, measured=True,
                     confidence=confidence, detail=detail), findings


# ---------------------------------------------------------------------------
# Per-object displacement
# ---------------------------------------------------------------------------


def _displacements(ctx) -> List[Dict[str, Any]]:
    """How far each observed object sits from where its detection implies."""
    if ctx.camera is None or ctx.graph is None:
        return []

    measured: List[Dict[str, Any]] = []
    for obj in ctx.observed_objects():
        bbox = getattr(obj, "bbox_2d", None)
        if bbox is None:
            continue
        # Wall- and ceiling-mounted objects do not touch the floor, so the
        # bottom edge of their box is not a floor contact point and the
        # back-projection would be meaningless.
        if getattr(obj, "support", "floor") != "floor":
            continue

        implied = ctx.camera.ground_position(bbox, height=0.0)
        if implied is None:
            continue

        actual = (obj.position.x, obj.position.y, obj.position.z)
        projected = ctx.camera.project(implied)
        range_m = projected[2] if projected else 0.0
        if range_m <= 0 or range_m > MAX_BACKPROJECTION_RANGE:
            continue

        measured.append({
            "object": obj.id,
            "category": obj.category,
            "distance": planar_distance(implied, actual),
            # The signed offset, which is what makes a *common* error
            # separable from a per-object one.
            "offset": [implied[0] - actual[0], implied[1] - actual[1]],
            "implied": [round(implied[0], 3), round(implied[1], 3)],
            "actual": [round(actual[0], 3), round(actual[1], 3)],
            "range_m": range_m,
            "locked": bool(getattr(obj, "locked", False)),
            "flags": list(getattr(obj, "flags", []) or []),
        })

    _attach_residuals(measured)
    return sorted(measured, key=lambda d: -d["residual"])


def _systematic_offset(displacements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect a single offset shared by every object.

    This is the difference between a useful report and fifteen lines of noise.
    If every object in a view appears displaced by roughly the same vector,
    the objects did not all move — the *camera* is in the wrong place, and one
    ``CameraFit`` finding says so where fifteen ``SceneGraphTransform``
    findings would send a refinement pass off to move furniture that is
    already correct relative to everything else.

    The test is the *median* offset and how many objects agree with it. The
    median rather than the mean because a single badly misplaced object drags
    a mean a long way, and the question being asked is precisely whether one
    object is the problem or all of them are. Agreement is then the share of
    objects sitting within a tolerance of that median — a genuine camera error
    moves everything alike, so agreement approaches 1, while one displaced
    sofa among four leaves the median near zero and nothing to attribute.
    """
    if len(displacements) < MIN_OBJECTS_FOR_SYSTEMATIC:
        return {"systematic": False, "reason": "too few objects to tell",
                "offset_m": [0.0, 0.0], "magnitude_m": 0.0, "coherence": 0.0,
                "objects": len(displacements)}

    offset = (
        _median([d["offset"][0] for d in displacements]),
        _median([d["offset"][1] for d in displacements]),
    )
    magnitude = math.hypot(*offset)

    # Tolerance scales with the offset: a 3 m camera error will not move every
    # object by exactly 3 m, because they sit at different depths.
    tolerance = max(AGREEMENT_TOLERANCE, 0.4 * magnitude)
    agreeing = sum(
        1 for d in displacements
        if math.hypot(d["offset"][0] - offset[0], d["offset"][1] - offset[1]) <= tolerance
    )
    coherence = agreeing / len(displacements)

    systematic = (
        magnitude >= DISPLACEMENT_FINDING
        and coherence >= COHERENCE_THRESHOLD
    )
    return {
        "systematic": bool(systematic),
        "offset_m": [round(offset[0], 3), round(offset[1], 3)],
        "magnitude_m": magnitude,
        "coherence": coherence,
        "agreeing_objects": agreeing,
        "objects": len(displacements),
    }


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _attach_residuals(displacements: List[Dict[str, Any]]) -> None:
    """Give every record its displacement net of the shared component.

    Scoring and per-object findings both use the residual: an object carried
    along by a camera error has not itself been misplaced, and charging it for
    the camera's mistake would double-count one fault as many.
    """
    summary = _systematic_offset(displacements)
    if summary.get("systematic"):
        offset = summary["offset_m"]
    else:
        offset = (0.0, 0.0)
    for record in displacements:
        record["residual"] = math.hypot(
            record["offset"][0] - offset[0], record["offset"][1] - offset[1]
        )


def _displacement_findings(ctx, displacements: List[Dict[str, Any]]) -> List[Finding]:
    findings: List[Finding] = []
    for record in displacements:
        distance = record.get("residual", record["distance"])
        if distance < DISPLACEMENT_FINDING:
            continue

        centimetres = distance * 100.0
        moved_by = _blame(record["flags"])
        findings.append(Finding(
            axis=LAYOUT,
            summary=f"{_label(record)} sits {centimetres:.0f} cm from where the "
                    f"reference places it",
            subsystem=Subsystem.SCENE_GRAPH_TRANSFORM,
            difference=distance,
            unit="m",
            severity=imaging.clamp01(distance / DISPLACEMENT_SATURATION),
            # Confidence falls with range: the same pixel of box-edge error
            # spans more floor the further away the object is.
            confidence=_confidence_for(record["range_m"]),
            why=f"back-projecting the detection box's floor contact through the "
                f"fitted camera puts it at {record['implied']}, while the graph "
                f"builds it at {record['actual']}"
                + (f"; {moved_by}" if moved_by else ""),
            evidence=record,
            remedy=("review the placement solver's adjustment for this object"
                    if moved_by else
                    "check the object's position against its detection"),
            room=ctx.room_id,
            viewpoint=ctx.viewpoint_id,
            objects=[record["object"]],
        ))
    return findings


def _camera_finding(ctx, systematic: Dict[str, Any], objects: int) -> List[Finding]:
    """One finding for a whole-view offset, in place of many object ones."""
    magnitude = systematic["magnitude_m"]
    return [Finding(
        axis=LAYOUT,
        summary=f"Every object in this view is offset by about "
                f"{magnitude * 100:.0f} cm in the same direction",
        subsystem=Subsystem.CAMERA_FIT,
        difference=magnitude,
        unit="m",
        severity=imaging.clamp01(magnitude / DISPLACEMENT_SATURATION),
        confidence=imaging.clamp01(0.5 + 0.4 * systematic["coherence"]),
        why=f"{systematic.get('agreeing_objects', objects)} of {objects} "
            f"objects back-project to positions displaced by about "
            f"{systematic['offset_m']} m — the same offset, not scattered ones. "
            f"Objects misplaced independently disagree with each other; a "
            f"shared offset means the camera this view was fitted with is in "
            f"the wrong place, not the furniture",
        evidence=systematic,
        remedy="re-fit this viewpoint's camera; the per-object offsets below "
               "are reported net of this shared error",
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


def _blame(flags: List[str]) -> str:
    """Name the recorded adjustment that most likely moved an object.

    The graph keeps a human-readable flag whenever validation or the placement
    solver changes something. Surfacing it turns "38 cm off" into "38 cm off,
    because collision resolution pushed it", which is a different and far more
    useful sentence.
    """
    for flag in flags:
        lowered = flag.lower()
        for marker in ("moved", "nudged", "collision", "overlap", "snapped",
                       "clamped", "pushed", "adjusted"):
            if marker in lowered:
                return f"the graph records: {flag}"
    return ""


def _confidence_for(range_m: float) -> float:
    if range_m <= 0:
        return 0.3
    return imaging.clamp01(0.85 - 0.04 * max(0.0, range_m - 2.0))


def _label(record: Dict[str, Any]) -> str:
    category = str(record.get("category") or "").replace("_", " ")
    return category.strip() or str(record.get("object", "object"))


# ---------------------------------------------------------------------------
# Visual mass
# ---------------------------------------------------------------------------


def _mass_finding(ctx, agreement: float, detail: Dict[str, Any]) -> List[Finding]:
    if agreement >= 0.6:
        return []

    shift = detail.get("mass_shift", "")
    return [Finding(
        axis=LAYOUT,
        summary="Visual mass is distributed differently from the reference",
        subsystem=Subsystem.SCENE_GRAPH_TRANSFORM,
        difference=1.0 - agreement,
        severity=imaging.clamp01((0.6 - agreement) / 0.6) * 0.8,
        confidence=0.55,
        why=f"on a coarse contrast grid the two frames agree only "
            f"{agreement * 100:.0f}%" + (f"; {shift}" if shift else "")
            + ". A camera that was fitted imperfectly produces this as "
              "readily as misplaced furniture, so it is corroboration rather "
              "than proof",
        evidence={"mass_agreement": agreement, "shift": shift},
        remedy="check object placement in this room, then the fitted camera",
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


def _describe_shift(reference_grid, render_grid) -> str:
    """Which way the mass moved, in plain words."""
    numpy, _ = imaging.backend()
    rows, columns = reference_grid.shape

    def centroid(grid):
        total = grid.sum()
        if total <= 1e-9:
            return (0.5, 0.5)
        ys, xs = numpy.mgrid[0:rows, 0:columns]
        return (float((grid * xs).sum() / total / max(1, columns - 1)),
                float((grid * ys).sum() / total / max(1, rows - 1)))

    reference_x, reference_y = centroid(reference_grid)
    render_x, render_y = centroid(render_grid)
    dx, dy = render_x - reference_x, render_y - reference_y

    if abs(dx) < 0.04 and abs(dy) < 0.04:
        return "no clear directional shift"
    parts = []
    if abs(dx) >= 0.04:
        parts.append(f"{'right' if dx > 0 else 'left'} by {abs(dx) * 100:.0f}% of frame")
    if abs(dy) >= 0.04:
        parts.append(f"{'down' if dy > 0 else 'up'} by {abs(dy) * 100:.0f}% of frame")
    return "the render's mass sits " + " and ".join(parts)


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


def _depth_profile(ctx) -> Optional[Dict[str, Any]]:
    """How the render's geometry is distributed in depth. Evidence only.

    Reported rather than scored: the reference has no depth channel, so there
    is nothing to compare it against. It is here because "the whole frame is
    within 2 m of the camera" explains a bad mass agreement in a way no other
    measurement does.
    """
    raw = ctx.pair.passes.get("depth")
    if raw is None:
        return None

    numpy, _ = imaging.backend()
    metres = imaging.depth_metres(raw, ctx.depth_range)
    valid = metres[numpy.isfinite(metres)]
    if valid.size == 0:
        return {"surfaces": 0.0}

    return {
        "surfaces": float(valid.size) / float(metres.size),
        "near_m": float(numpy.percentile(valid, 5)),
        "median_m": float(numpy.median(valid)),
        "far_m": float(numpy.percentile(valid, 95)),
        "beyond_range": float((raw[..., 0] >= 255).mean()),
    }
