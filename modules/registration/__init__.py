"""
ArchX3D — CAD ↔ Image Registration
==================================
The join between the two things this pipeline knows about a building: the
drawing, which is authoritative about structure and function, and the
photographs, which are authoritative about appearance.

Until this package existed, that join was assumed rather than established. A
plan-view sheet was mapped onto the drawing by stretching it to fill the
frame; an interior photograph was matched to a room by comparing floor areas.
Both are guesses, and the project's first principle is that a guess is only
acceptable where no reliable information exists. In both cases it did — the
architect printed room names into the drawing *and* onto the sheet, and the
semantic layer had already worked out what every room is.

What registration means here differs by view, but the question does not:

``plan``
    Top-down sheets. Fits a similarity transform image → plan metres from
    room-label correspondences, robustly, and reports which sub-rectangle of a
    composite sheet the drawing actually occupies.
``interior``
    Perspective photographs. Chooses the region each image group depicts,
    anchored on the room type the drawing states rather than on floor area.

``labels`` produces the evidence, ``consensus`` decides which of it is real,
``transform`` is the arithmetic, and ``schema`` holds the results.

Typical use::

    from cad.schema import CadDocument
    from registration import register_plan_view

    result = register_plan_view(document, observation, plan_min, plan_max)
    if result.registered:
        x, y = result.transform.apply(u, v)
    else:
        print(result.explain())   # says exactly what was missing

Stdlib only, and independent of both ``cad`` and ``vision`` except for two
adapter functions in ``labels``. The fitting machinery can therefore be
exercised from hand-built anchors, with no DXF, no imagery and no API key —
which is what makes a registration testable at all.
"""

from __future__ import annotations

from .consensus import Consensus, find, tolerance_for
from .interior import register_interior_views, score_region
from .labels import (
    LabelAnchor,
    anchors_from_cad,
    anchors_from_observation,
    candidates,
    normalise,
    strip_measurements,
    text_similarity,
)
from .plan import register_plan_view, register_plan_views
from .schema import (
    SCHEMA_VERSION,
    Correspondence,
    Method,
    PlanTransform,
    RegistrationResult,
    RoomRegistration,
    SheetRegion,
)
from .transform import (
    fit_similarity,
    image_region_of_plan,
    residual,
    score_residuals,
    snap_rotation,
)

__all__ = [
    "Consensus",
    "Correspondence",
    "LabelAnchor",
    "Method",
    "PlanTransform",
    "RegistrationResult",
    "RoomRegistration",
    "SCHEMA_VERSION",
    "SheetRegion",
    "anchors_from_cad",
    "anchors_from_observation",
    "candidates",
    "find",
    "fit_similarity",
    "image_region_of_plan",
    "normalise",
    "register_interior_views",
    "register_plan_view",
    "register_plan_views",
    "residual",
    "score_region",
    "score_residuals",
    "snap_rotation",
    "strip_measurements",
    "text_similarity",
    "tolerance_for",
]
