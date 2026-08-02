"""
ArchX3D — Plan-view registration
================================
Answers, for one top-down reference image: *where on this drawing is this
picture, and at what scale?*

The failure this replaces
-------------------------
``grounding.ground_plan_view`` maps the whole image linearly onto the plan's
bounding box. That is correct exactly when the image is one floor plan filling
the frame, and wrong the rest of the time — and it is wrong *silently*, with
every detection landing outside every room polygon and being discarded one by
one. A real reported case: a sheet with an exterior render across the top and
two floor plans side by side below it lost all eleven detections, and the
error message blamed the user for not supplying reference images.

The fix is to stop assuming and start measuring. The DXF yields room labels
with plan coordinates; the same words are printed on the sheet and a vision
model can read them with pixel coordinates. Matching them gives point
correspondences, and four parameters — scale, rotation, and two translations
— are all that separate the two coordinate systems.

The ladder
----------
Each rung is tried in turn and the result records which one answered, because
"we measured this" and "we assumed this" must never look alike downstream:

1. ``label_consensus`` — two or more labels agreed on one transform. The real
   answer, and the only rung that can detect a composite sheet.
2. ``single_anchor`` — exactly one label matched. Position is anchored to a
   fact; scale is still assumed. Weak, and marked weak.
3. ``plan_bounds`` — the legacy full-frame assumption, preserved so nothing
   regresses, but now labelled as the guess it always was.
4. ``none`` — no transform. The caller drops the image's positions rather
   than inventing them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import consensus as consensus_mod
from . import labels as labels_mod
from .schema import (
    Correspondence,
    Method,
    PlanTransform,
    RegistrationResult,
    SheetRegion,
)
from .transform import image_region_of_plan

Point = Tuple[float, float]

#: A fit that puts less than this share of the drawing inside the frame has
#: mapped the building off the page. Two labels can always be made to agree by
#: a transform that flings everything else into the margins; this is the check
#: that catches it.
MIN_PLAN_IN_FRAME = 0.30

#: Confidence ceiling per inlier count. Two correspondences fit a similarity
#: exactly, so a zero residual proves nothing about them — the fit is only
#: corroborated from the third label onwards.
_BASE_CONFIDENCE = {2: 0.45, 3: 0.68, 4: 0.78}
_BASE_CONFIDENCE_MANY = 0.86


def register_plan_view(
    document: Any,
    observation: Any,
    bounds_min: Point,
    bounds_max: Point,
    *,
    allow_assumed: bool = True,
    snap_rotation: bool = True,
) -> RegistrationResult:
    """Register one plan-view image against the drawing.

    ``document`` is a ``cad.CadDocument`` or ``None``; ``observation`` is a
    ``vision.observe.ImageObservation``. ``bounds_min`` / ``bounds_max`` are
    the plan's extent in metres, which is what the assumed rungs fall back to.

    ``allow_assumed=False`` stops the ladder after ``single_anchor``, so a
    caller that would rather place nothing than place from a guess can say so.
    """
    image_id = str(getattr(observation, "image_id", "") or "")
    result = RegistrationResult(image_id=image_id)

    cad_anchors = labels_mod.anchors_from_cad(document)
    image_anchors = labels_mod.anchors_from_observation(observation)

    result.unmatched_cad_labels = sorted({a.match_key for a in cad_anchors})
    result.unmatched_image_labels = sorted({a.match_key for a in image_anchors})

    if not cad_anchors or not image_anchors:
        result.reason = _absence_reason(cad_anchors, image_anchors)
        return _assume(result, bounds_min, bounds_max, allow_assumed)

    candidates = labels_mod.candidates(image_anchors, cad_anchors)
    if not candidates:
        result.reason = (
            f"none of the {len(image_anchors)} label(s) read in the image match "
            f"any of the {len(cad_anchors)} label(s) in the drawing"
        )
        return _assume(result, bounds_min, bounds_max, allow_assumed)

    tolerance = consensus_mod.tolerance_for(bounds_min, bounds_max)
    found = consensus_mod.find(candidates, tolerance, snap=snap_rotation)

    result.correspondences = candidates

    if found.ok and _plan_lands_in_frame(found.transform, bounds_min, bounds_max):
        return _accept(result, found, bounds_min, bounds_max, image_anchors, cad_anchors)

    if found.ok:
        result.warnings.append(
            "a transform was found but it maps the drawing outside the image "
            "frame; treating it as a mismatch"
        )
        for correspondence in candidates:
            correspondence.inlier = False

    single = _single_anchor(candidates, bounds_min, bounds_max)
    if single is not None:
        return _accept_single(
            result, single, bounds_min, bounds_max, image_anchors, cad_anchors
        )

    result.reason = (
        f"{len(candidates)} candidate label match(es) considered; no two agreed "
        f"on a transform within {tolerance:.2f} m"
    )
    return _assume(result, bounds_min, bounds_max, allow_assumed)


def register_plan_views(
    document: Any,
    observations: Sequence[Any],
    bounds_min: Point,
    bounds_max: Point,
    **options: Any,
) -> Dict[str, RegistrationResult]:
    """Register every plan-view observation, keyed by image id."""
    return {
        str(getattr(o, "image_id", "") or f"image_{index}"): register_plan_view(
            document, o, bounds_min, bounds_max, **options
        )
        for index, o in enumerate(observations)
    }


# ---------------------------------------------------------------------------
# Rungs
# ---------------------------------------------------------------------------


def _accept(
    result: RegistrationResult,
    found: "consensus_mod.Consensus",
    bounds_min: Point,
    bounds_max: Point,
    image_anchors: Sequence[labels_mod.LabelAnchor],
    cad_anchors: Sequence[labels_mod.LabelAnchor],
) -> RegistrationResult:
    """Accept a consensus fit and describe it."""
    result.transform = found.transform
    result.method = Method.LABEL_CONSENSUS
    result.residual_mean_m = found.residual_mean_m
    result.residual_max_m = found.residual_max_m
    result.confidence = _confidence(found)
    result.sheet_region = _sheet_region(found.transform, bounds_min, bounds_max)

    result.unmatched_image_labels = labels_mod.unmatched(
        image_anchors, result.correspondences, "image"
    )
    result.unmatched_cad_labels = labels_mod.unmatched(
        cad_anchors, result.correspondences, "cad"
    )

    matched = ", ".join(sorted({c.text for c in found.correspondences})[:5])
    result.reason = (
        f"{found.inlier_count} label(s) agreed on one transform "
        f"({matched}); mean residual {found.residual_mean_m:.2f} m"
    )

    if found.rotation_snapped:
        result.warnings.append(
            "the fitted rotation was within a few degrees of square and was "
            "snapped to the sheet"
        )

    if result.sheet_region and result.sheet_region.looks_composite:
        result.warnings.append(
            f"the drawing occupies only {result.sheet_region.coverage:.0%} of this "
            f"image (u {result.sheet_region.u0:.2f}-{result.sheet_region.u1:.2f}, "
            f"v {result.sheet_region.v0:.2f}-{result.sheet_region.v1:.2f}); it is a "
            "composite sheet and the rest of the frame is something else"
        )

    # Labels the image showed that this floor does not contain are the clearest
    # signal available that the sheet holds more than one plan. Reporting them
    # by name is what lets a user see *which* other floor is on the page.
    if len(result.unmatched_image_labels) >= 2:
        names = ", ".join(result.unmatched_image_labels[:6])
        result.warnings.append(
            f"{len(result.unmatched_image_labels)} label(s) read in the image are "
            f"not in this drawing ({names}); they most likely belong to another "
            "plan on the same sheet, whose furniture must not be read into this one"
        )

    return result


def _accept_single(
    result: RegistrationResult,
    pair: Tuple[Correspondence, PlanTransform],
    bounds_min: Point,
    bounds_max: Point,
    image_anchors: Sequence[labels_mod.LabelAnchor],
    cad_anchors: Sequence[labels_mod.LabelAnchor],
) -> RegistrationResult:
    """Accept a one-label anchor, and be explicit that scale was assumed."""
    correspondence, transform = pair
    correspondence.inlier = True
    correspondence.residual_m = 0.0

    result.transform = transform
    result.method = Method.SINGLE_ANCHOR
    result.confidence = round(0.30 * max(0.4, correspondence.weight), 3)
    result.sheet_region = _sheet_region(transform, bounds_min, bounds_max)
    result.unmatched_image_labels = labels_mod.unmatched(
        image_anchors, result.correspondences, "image"
    )
    result.unmatched_cad_labels = labels_mod.unmatched(
        cad_anchors, result.correspondences, "cad"
    )
    result.reason = (
        f"only one label matched ({correspondence.text}); position is anchored "
        "to it but the scale assumes the plan fills the frame"
    )
    result.warnings.append(
        "scale and rotation were assumed, not measured — a second matching "
        "label anywhere on the sheet would determine both"
    )
    return result


def _assume(
    result: RegistrationResult,
    bounds_min: Point,
    bounds_max: Point,
    allow_assumed: bool,
) -> RegistrationResult:
    """The last rung: the legacy full-frame stretch, or nothing at all."""
    if not allow_assumed:
        result.method = Method.NONE
        result.transform = None
        result.confidence = 0.0
        result.warnings.append(
            "no transform could be measured and assumed transforms are "
            "disabled; this image contributes no positions"
        )
        return result

    result.transform = PlanTransform.stretch_to_bounds(bounds_min, bounds_max)
    result.method = Method.PLAN_BOUNDS
    result.confidence = 0.15
    result.sheet_region = SheetRegion(0.0, 0.0, 1.0, 1.0)
    result.warnings.append(
        "falling back to the assumption that this image is one floor plan "
        "filling the frame; if it is a composite sheet, every placement taken "
        "from it will be wrong"
    )
    return result


def _single_anchor(
    candidates: Sequence[Correspondence],
    bounds_min: Point,
    bounds_max: Point,
) -> Optional[Tuple[Correspondence, PlanTransform]]:
    """A transform through the one unambiguous correspondence, if there is one.

    "Unambiguous" is doing the work: a candidate only qualifies when its image
    label matched exactly one CAD label and that CAD label matched exactly one
    image label. A ``BEDROOM`` that could be any of three is not an anchor, it
    is a coin toss with three sides.
    """
    by_image: Dict[str, List[Correspondence]] = {}
    by_cad: Dict[str, List[Correspondence]] = {}
    for correspondence in candidates:
        by_image.setdefault(correspondence.image_label_id, []).append(correspondence)
        by_cad.setdefault(correspondence.cad_uid, []).append(correspondence)

    unique = [
        c for c in candidates
        if len(by_image[c.image_label_id]) == 1 and len(by_cad[c.cad_uid]) == 1
    ]
    if len(unique) != 1:
        return None

    correspondence = unique[0]

    # Assume the drawing fills the frame, isotropically. The larger of the two
    # plan dimensions sets the scale: a plan is fitted to the sheet by its long
    # side, and over-estimating scale scatters placements outward where they
    # can be caught, while under-estimating piles them into the middle where
    # they look plausible and are not.
    scale = max(bounds_max[0] - bounds_min[0], bounds_max[1] - bounds_min[1])
    if scale <= 0.0:
        return None

    u, v = correspondence.image_uv
    transform = PlanTransform.from_similarity(
        scale=scale,
        rotation_deg=0.0,
        tx=correspondence.plan_xy[0] - scale * u,
        ty=correspondence.plan_xy[1] - scale * v,
    )
    return correspondence, transform


# ---------------------------------------------------------------------------
# Validation and description
# ---------------------------------------------------------------------------


def _plan_lands_in_frame(
    transform: Optional[PlanTransform], bounds_min: Point, bounds_max: Point
) -> bool:
    """Does the fitted transform put the building on the page?

    Any two correspondences can be satisfied exactly, including two that are
    both wrong. What a wrong pair cannot do is keep the rest of the drawing
    inside the picture, so this is an independent check — it uses the plan's
    extent, which no correspondence contributed to.
    """
    if transform is None:
        return False
    region = image_region_of_plan(transform, bounds_min, bounds_max)
    if region is None:
        return False

    u0, v0, u1, v1 = region
    area = max(0.0, u1 - u0) * max(0.0, v1 - v0)
    if area <= 1e-9:
        return False

    inside = (
        max(0.0, min(u1, 1.0) - max(u0, 0.0))
        * max(0.0, min(v1, 1.0) - max(v0, 0.0))
    )
    return (inside / area) >= MIN_PLAN_IN_FRAME


def _sheet_region(
    transform: PlanTransform, bounds_min: Point, bounds_max: Point
) -> Optional[SheetRegion]:
    """Where the drawing sits in the frame, clipped to what is visible."""
    region = image_region_of_plan(transform, bounds_min, bounds_max)
    if region is None:
        return None
    u0, v0, u1, v1 = region
    return SheetRegion(
        u0=max(0.0, min(1.0, u0)),
        v0=max(0.0, min(1.0, v0)),
        u1=max(0.0, min(1.0, u1)),
        v1=max(0.0, min(1.0, v1)),
    )


def _confidence(found: "consensus_mod.Consensus") -> float:
    """How much to trust a consensus fit.

    Three independent factors, because they fail independently: how many
    labels agreed, how tightly they agreed, and how good the underlying text
    matches were. A fit from five fuzzy matches with a residual at the
    tolerance limit should not read like one from five exact matches at
    100 mm.
    """
    base = _BASE_CONFIDENCE.get(found.inlier_count, _BASE_CONFIDENCE_MANY)

    tolerance = found.tolerance_m or 1.0
    tightness = max(0.0, 1.0 - (found.residual_mean_m / tolerance))

    mean_weight = (
        sum(c.weight for c in found.correspondences) / len(found.correspondences)
        if found.correspondences else 0.0
    )

    score = base * (0.60 + 0.40 * tightness) * (0.70 + 0.30 * min(1.0, mean_weight))
    return round(min(0.97, max(0.0, score)), 3)


def _absence_reason(cad_anchors: Sequence[Any], image_anchors: Sequence[Any]) -> str:
    if not cad_anchors and not image_anchors:
        return (
            "the drawing carries no room labels and none were read in the image; "
            "there is nothing to match"
        )
    if not cad_anchors:
        return (
            "the drawing carries no usable text labels, so image labels have "
            "nothing to match against"
        )
    return (
        "no labels were read in the image — either the model was not asked for "
        "them, or the sheet prints none legibly at this resolution"
    )
