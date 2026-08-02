"""
ArchX3D — Similarity fitting
============================
The arithmetic that turns matched points into a transform.

Why a similarity and not least-squares affine
---------------------------------------------
Six free parameters will always fit the data better than four. That is the
problem, not the point. A floor plan is a uniformly scaled orthographic view
of a floor; it is never sheared and never stretched along one axis. Given
four noisy correspondences an affine fit absorbs the noise into a plausible
looking shear and reports a small residual, so the one number that would have
warned us the match is wrong — the residual — is destroyed by the extra
freedom. A similarity has nowhere to hide the error, so a bad correspondence
set produces a visibly bad residual and gets rejected.

Why closed form and not iterative
---------------------------------
The weighted 2D similarity that minimises squared error has an exact
solution, so there is nothing to converge to and no tolerance to tune. This is
Umeyama's result specialised to the plane, with weights carried through and
the vertical flip between image and plan space folded in.

Stdlib only — no numpy, deliberately. Everything here is a handful of sums.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from .schema import Correspondence, PlanTransform

Point = Tuple[float, float]

#: A fitted scale outside this range is not a floor plan. Expressed in metres
#: spanned by the full width of the image: a plan of a broom cupboard drawn
#: edge to edge is a couple of metres, a site plan of an estate is hundreds.
#: Anything outside means the correspondences were nonsense.
MIN_SCALE_M = 0.5
MAX_SCALE_M = 500.0

#: Fitted rotations within this many degrees of a right angle are snapped.
#: Drawings are printed square to the sheet; a fitted 0.8 degree tilt is
#: correspondence noise, and leaving it in rotates every placement.
ROTATION_SNAP_DEG = 4.0


def fit_similarity(
    image_points: Sequence[Point],
    plan_points: Sequence[Point],
    weights: Optional[Sequence[float]] = None,
    fixed_rotation_deg: Optional[float] = None,
) -> Optional[PlanTransform]:
    """Least-squares similarity mapping image ``(u, v)`` to plan metres.

    Returns ``None`` when the inputs cannot determine one: fewer than two
    pairs, coincident image points, or a degenerate scale. A caller must treat
    ``None`` as "no transform", never as identity — silently substituting
    identity is how a failed registration becomes furniture in the wrong room.

    ``fixed_rotation_deg`` constrains the fit to a known orientation, which is
    what ``snap_rotation`` uses to re-solve scale and translation after
    deciding the drawing is square to the sheet.
    """
    count = min(len(image_points), len(plan_points))
    if count < 2:
        return None

    weights = list(weights or [1.0] * count)[:count]
    weights = [max(0.0, w) for w in weights]
    total_weight = sum(weights)
    if total_weight <= 0.0:
        return None

    # Image space is flipped into plan orientation before fitting: v runs down
    # the image and y runs up the plan, and a proper (non-reflecting)
    # similarity cannot express that flip on its own.
    source = [(image_points[i][0], -image_points[i][1]) for i in range(count)]
    target = [tuple(plan_points[i]) for i in range(count)]

    src_mean = _weighted_mean(source, weights, total_weight)
    dst_mean = _weighted_mean(target, weights, total_weight)

    src_centred = [(p[0] - src_mean[0], p[1] - src_mean[1]) for p in source]
    dst_centred = [(p[0] - dst_mean[0], p[1] - dst_mean[1]) for p in target]

    variance = sum(
        w * (p[0] * p[0] + p[1] * p[1]) for p, w in zip(src_centred, weights)
    )
    if variance < 1e-12:
        # Every image point is in the same place; direction is unrecoverable.
        return None

    # Dot and cross products accumulate the rotation that best aligns the two
    # centred point sets; their magnitude carries the scale.
    dot = sum(
        w * (s[0] * d[0] + s[1] * d[1])
        for s, d, w in zip(src_centred, dst_centred, weights)
    )
    cross = sum(
        w * (s[0] * d[1] - s[1] * d[0])
        for s, d, w in zip(src_centred, dst_centred, weights)
    )

    if fixed_rotation_deg is None:
        if abs(dot) < 1e-15 and abs(cross) < 1e-15:
            return None
        theta = math.atan2(cross, dot)
        scale = math.hypot(dot, cross) / variance
    else:
        theta = math.radians(fixed_rotation_deg)
        # With the angle fixed, the optimal scale is the projection of the
        # target onto the rotated source, normalised by the source's spread.
        cos, sin = math.cos(theta), math.sin(theta)
        projection = sum(
            w * ((cos * s[0] - sin * s[1]) * d[0] + (sin * s[0] + cos * s[1]) * d[1])
            for s, d, w in zip(src_centred, dst_centred, weights)
        )
        scale = projection / variance

    if not math.isfinite(scale) or scale < MIN_SCALE_M or scale > MAX_SCALE_M:
        return None

    cos, sin = math.cos(theta), math.sin(theta)
    tx = dst_mean[0] - scale * (cos * src_mean[0] - sin * src_mean[1])
    ty = dst_mean[1] - scale * (sin * src_mean[0] + cos * src_mean[1])

    return PlanTransform.from_similarity(scale, math.degrees(theta), tx, ty)


def snap_rotation(
    transform: PlanTransform,
    image_points: Sequence[Point],
    plan_points: Sequence[Point],
    weights: Optional[Sequence[float]] = None,
    tolerance_deg: float = ROTATION_SNAP_DEG,
) -> Tuple[PlanTransform, bool]:
    """Square a nearly-square transform to the sheet, and re-solve.

    A plan printed on a sheet is square to it. A fit that comes back 1.3
    degrees off is reporting label-centroid noise, not a tilted drawing, and
    keeping that tilt swings placements at the far end of a long building by
    more than the error it came from.

    The scale and translation are re-solved with the angle held fixed rather
    than merely overwritten, so the snapped transform is still the best fit
    *given* that orientation, not the old fit with a corner bent.

    Returns ``(transform, snapped)``.
    """
    rotation = transform.rotation_deg
    nearest = round(rotation / 90.0) * 90.0
    if abs(_angle_difference(rotation, nearest)) > tolerance_deg:
        return transform, False

    refitted = fit_similarity(
        image_points, plan_points, weights, fixed_rotation_deg=nearest
    )
    if refitted is None:
        return transform, False
    return refitted, True


def residual(transform: PlanTransform, correspondence: Correspondence) -> float:
    """Distance in metres between where the image says a label is and where
    the drawing says it is."""
    mapped = transform.apply(*correspondence.image_uv)
    return math.dist(mapped, correspondence.plan_xy)


def score_residuals(
    transform: PlanTransform, correspondences: Sequence[Correspondence]
) -> Tuple[float, float]:
    """``(mean, max)`` residual in metres over ``correspondences``."""
    if not correspondences:
        return 0.0, 0.0
    values = [residual(transform, c) for c in correspondences]
    return sum(values) / len(values), max(values)


def image_region_of_plan(
    transform: PlanTransform, bounds_min: Point, bounds_max: Point
) -> Optional[Tuple[float, float, float, float]]:
    """Where the plan's extent lands back in the image, as ``(u0, v0, u1, v1)``.

    This is what identifies a composite sheet. Inverting the fitted transform
    and mapping the building's own bounding box through it says which part of
    the frame the drawing occupies — so a sheet whose plan fills a fifth of
    the page reports that, instead of the pipeline assuming a full frame and
    scattering every detection outside every room.
    """
    inverse = transform.inverse()
    if inverse is None:
        return None

    corners = [
        inverse.apply(bounds_min[0], bounds_min[1]),
        inverse.apply(bounds_max[0], bounds_min[1]),
        inverse.apply(bounds_max[0], bounds_max[1]),
        inverse.apply(bounds_min[0], bounds_max[1]),
    ]
    us = [p[0] for p in corners]
    vs = [p[1] for p in corners]
    return (min(us), min(vs), max(us), max(vs))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _weighted_mean(
    points: Sequence[Point], weights: Sequence[float], total: float
) -> Point:
    return (
        sum(p[0] * w for p, w in zip(points, weights)) / total,
        sum(p[1] * w for p, w in zip(points, weights)) / total,
    )


def _angle_difference(a: float, b: float) -> float:
    """Signed smallest difference between two angles in degrees."""
    return (a - b + 180.0) % 360.0 - 180.0
