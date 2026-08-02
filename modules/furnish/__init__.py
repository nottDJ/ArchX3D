"""
ArchX3D — Procedural Interior Generation
========================================
Stage 7. Furnishes a semantically-typed scene graph.

Consumes the scene graph and nothing else — not the DXF, not the reference
images. By the time this runs the building has been understood, and every
decision follows from a room's *type*, *size* and *shape*.

    from furnish import furnish

    report = furnish(graph)
    print(report.to_dict()["objects_created"])

Three layers:

``programme``
    What a room of a given type and size should contain, as an ordered list of
    candidates with minimum areas. Includes the lighting scheme, because a
    furnished room with no luminaire renders black.
``placement``
    Where each item goes. Candidate generation and scoring rather than
    constraint solving, so an over-constrained room degrades to "the bed fits,
    nothing else does" instead of failing outright.
``furnisher``
    The orchestrator. Skips rooms that already have observed contents —
    an observation always beats a convention — and reports every item it
    could not place, with the reason.

Everything generated is flagged ``procedural`` so a user can always tell a
design decision from an observation.
"""

from __future__ import annotations

from .furnisher import (
    PROCEDURAL_CONFIDENCE,
    FurnishReport,
    RoomFurnishing,
    furnish,
)
from .placement import (
    MIN_CIRCULATION_M,
    Placement,
    RoomSpace,
    Solver,
    rect_corners,
    rects_overlap,
)
from .programme import (
    LIGHTING_PROGRAMMES,
    PROGRAMMES,
    UNFURNISHED_TYPES,
    PlannedItem,
    PlannedLight,
    ProgrammeItem,
    furnishable_types,
    plan_lighting,
    plan_room,
    programme_for,
)

__all__ = [
    "LIGHTING_PROGRAMMES",
    "MIN_CIRCULATION_M",
    "PROCEDURAL_CONFIDENCE",
    "PROGRAMMES",
    "UNFURNISHED_TYPES",
    "FurnishReport",
    "Placement",
    "PlannedItem",
    "PlannedLight",
    "ProgrammeItem",
    "RoomFurnishing",
    "RoomSpace",
    "Solver",
    "furnish",
    "furnishable_types",
    "plan_lighting",
    "plan_room",
    "programme_for",
    "rect_corners",
    "rects_overlap",
]
