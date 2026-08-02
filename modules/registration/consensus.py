"""
ArchX3D — Robust consensus fitting
==================================
Picks the transform that the largest self-consistent set of correspondences
agrees on, and throws the rest away.

The problem this solves
-----------------------
Label matching is ambiguous by construction. A flat with three bedrooms
produces three candidate pairings for one ``BEDROOM`` printed on the sheet,
and only one of them is true. Fitting a transform to all of them averages a
correct answer with two wrong ones and produces a third answer that is wrong
everywhere.

The insight is that wrong pairings do not agree with each other. Any two
correct pairings imply the same transform; a wrong pairing implies a
different one and is contradicted by everything else. So rather than fitting
all the data, we look for the transform with the largest agreeing subset —
RANSAC, with the sampling made exhaustive because a floor plan has tens of
labels, not thousands of feature points, and enumerating every pair is both
faster than randomising and completely deterministic.

Determinism matters more than it sounds. A registration that changes between
two runs on unchanged input would move furniture between two builds of the
same project, and no cache or diff downstream could be trusted.

Two refinements on textbook RANSAC
----------------------------------
* **One-to-one consensus.** A transform is not allowed to count the same
  image label twice, nor to claim two image labels are the same CAD entity.
  Without this, a degenerate transform that collapses the plan to a point
  scores brilliantly by matching everything to everything.
* **Local re-fitting.** The winning minimal sample is re-solved against its
  whole consensus set, which is where the accuracy actually comes from: two
  points give an exact but noise-bound fit, eight give a least-squares one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .schema import Correspondence, PlanTransform
from .transform import fit_similarity, residual, score_residuals, snap_rotation

#: Inlier tolerance as a fraction of the building's diagonal, and a floor in
#: metres. A label's anchor in the DXF is the text insertion point while the
#: image gives the centre of the printed string, so even a perfect
#: registration leaves a systematic offset of roughly half a label width.
#: The tolerance has to clear that without admitting the room next door.
TOLERANCE_FRACTION = 0.06
TOLERANCE_FLOOR_M = 0.75

#: Total candidates considered. Beyond this the input is not a floor plan's
#: worth of labels — it is a schedule or a legend — and the pair enumeration
#: stops being the cheap operation the design assumes.
MAX_CANDIDATES = 150

#: Local re-fit passes after a winner is found. Two is enough to converge:
#: the first pulls the exact two-point fit onto its consensus set, the second
#: picks up any correspondence that the improved fit brought into tolerance.
REFIT_PASSES = 2


@dataclass
class Consensus:
    """The best transform found, and what agreed with it."""

    transform: Optional[PlanTransform] = None
    correspondences: List[Correspondence] = field(default_factory=list)
    inlier_count: int = 0
    #: Summed match weight of the inliers — the quantity actually maximised.
    support: float = 0.0
    residual_mean_m: float = 0.0
    residual_max_m: float = 0.0
    rotation_snapped: bool = False
    tolerance_m: float = 0.0

    @property
    def ok(self) -> bool:
        return self.transform is not None and self.inlier_count >= 2


def tolerance_for(bounds_min: Tuple[float, float], bounds_max: Tuple[float, float]) -> float:
    """Inlier tolerance in metres, scaled to the building.

    Absolute tolerances do not survive contact with real drawings: 1 m is
    generous in a studio flat and far too tight across a school. Scaling to
    the diagonal keeps the test meaning the same thing — "this label is where
    that label is, relative to the size of the thing being drawn".
    """
    diagonal = math.dist(bounds_min, bounds_max)
    return max(TOLERANCE_FLOOR_M, TOLERANCE_FRACTION * diagonal)


def find(
    candidates: Sequence[Correspondence],
    tolerance_m: float,
    snap: bool = True,
) -> Consensus:
    """The transform with the largest agreeing subset of ``candidates``.

    Returns a ``Consensus`` whose ``ok`` is False when nothing agreed — which
    is a legitimate outcome for a sheet whose labels genuinely do not match
    the drawing, and must not be papered over with a best-effort transform.
    """
    pool = _prune(candidates)
    if len(pool) < 2:
        return Consensus(tolerance_m=tolerance_m)

    best = Consensus(tolerance_m=tolerance_m)

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            first, second = pool[i], pool[j]

            # A minimal sample that reuses either side is not two independent
            # observations; it is one, and it cannot determine a transform.
            if first.image_label_id == second.image_label_id:
                continue
            if first.cad_uid == second.cad_uid:
                continue

            transform = fit_similarity(
                [first.image_uv, second.image_uv],
                [first.plan_xy, second.plan_xy],
                [first.weight, second.weight],
            )
            if transform is None:
                continue

            candidate = _evaluate(transform, pool, tolerance_m)
            if _better(candidate, best):
                best = candidate

    if not best.ok:
        return Consensus(tolerance_m=tolerance_m)

    return _refine(best, pool, tolerance_m, snap)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _prune(candidates: Sequence[Correspondence]) -> List[Correspondence]:
    """Cap the candidate pool, keeping the strongest text matches.

    Sorted by weight then by identity so the cut is deterministic — two runs
    on the same drawing must keep the same candidates, or the registration
    itself becomes non-reproducible.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-c.weight, c.image_label_id, c.cad_uid),
    )
    return ordered[:MAX_CANDIDATES]


def _evaluate(
    transform: PlanTransform,
    pool: Sequence[Correspondence],
    tolerance_m: float,
) -> Consensus:
    """Score a transform by its one-to-one consensus set."""
    scored = sorted(
        ((residual(transform, c), c) for c in pool),
        key=lambda pair: (pair[0], pair[1].image_label_id, pair[1].cad_uid),
    )

    used_images: set = set()
    used_cad: set = set()
    inliers: List[Correspondence] = []

    for distance, correspondence in scored:
        if distance > tolerance_m:
            break
        # Nearest wins the pairing: a label already claimed by a closer match
        # cannot be double-counted, which is what stops a collapsed transform
        # from scoring perfectly by mapping everything onto one point.
        if correspondence.image_label_id in used_images:
            continue
        if correspondence.cad_uid in used_cad:
            continue
        used_images.add(correspondence.image_label_id)
        used_cad.add(correspondence.cad_uid)
        inliers.append(correspondence)

    mean, worst = score_residuals(transform, inliers)
    return Consensus(
        transform=transform,
        correspondences=list(inliers),
        inlier_count=len(inliers),
        support=sum(c.weight for c in inliers),
        residual_mean_m=mean,
        residual_max_m=worst,
        tolerance_m=tolerance_m,
    )


def _better(candidate: Consensus, incumbent: Consensus) -> bool:
    """Prefer more agreement; break ties on tighter agreement.

    Support rather than raw inlier count, so a fit backed by three exact label
    matches beats one backed by four fuzzy ones — the fuzzy set is more likely
    to be four coincidences.
    """
    if candidate.inlier_count < 2:
        return False
    if abs(candidate.support - incumbent.support) > 1e-9:
        return candidate.support > incumbent.support
    return candidate.residual_mean_m < incumbent.residual_mean_m


def _refine(
    best: Consensus,
    pool: Sequence[Correspondence],
    tolerance_m: float,
    snap: bool,
) -> Consensus:
    """Re-solve the winner against its whole consensus set.

    The minimal sample that won is exact through two points and therefore
    carries their noise undiluted. Re-fitting over every inlier is what turns
    a lucky pair into a measurement.
    """
    current = best

    for _ in range(REFIT_PASSES):
        if len(current.correspondences) < 2:
            break
        refitted = fit_similarity(
            [c.image_uv for c in current.correspondences],
            [c.plan_xy for c in current.correspondences],
            [c.weight for c in current.correspondences],
        )
        if refitted is None:
            break
        improved = _evaluate(refitted, pool, tolerance_m)
        if not _better(improved, current):
            break
        current = improved

    snapped = False
    if snap and current.transform is not None and len(current.correspondences) >= 2:
        squared, snapped = snap_rotation(
            current.transform,
            [c.image_uv for c in current.correspondences],
            [c.plan_xy for c in current.correspondences],
            [c.weight for c in current.correspondences],
        )
        if snapped:
            # Only keep the squared transform if it did not cost agreement.
            # A drawing genuinely printed at an angle exists, and forcing it
            # square would be the engine overruling its own measurement.
            candidate = _evaluate(squared, pool, tolerance_m)
            if candidate.inlier_count >= current.inlier_count:
                candidate.rotation_snapped = True
                current = candidate
            else:
                snapped = False

    for correspondence in pool:
        correspondence.inlier = False
    for correspondence in current.correspondences:
        correspondence.inlier = True
        correspondence.residual_m = residual(current.transform, correspondence)

    current.rotation_snapped = snapped
    return current
