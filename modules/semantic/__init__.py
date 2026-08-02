"""
ArchX3D — Semantic Understanding
================================
Decides what every room in a plan is *for*, by fusing every available signal
into one explained, calibrated answer.

This package implements Stage 3 of the platform brief. It is deliberately
independent of both ``cad`` and ``vision``: the classifier consumes plain
``RoomEvidenceInput`` records, so it can be driven from a DXF, from a scene
graph, from reference imagery, or from a hand-written test fixture, and it
imports neither ezdxf nor numpy.

Typical use::

    from cad import read_dxf
    from semantic import build_inputs, classify_plan

    document = read_dxf("plan.dxf")
    inputs = build_inputs(document, regions)
    for result in classify_plan(inputs):
        print(result.summary())

    bedroom 92%
      text label "MASTER BED" inside the room
      BED block 1.8 m from the centroid
      WARDROBE block
      adjacent to bathroom

The three layers, in dependency order:

``taxonomy``
    The priors. Every room type's plausible area, proportions, expected
    openings, adjacency affinities, and what each object category implies.
``evidence``
    The fusion mechanism. Log-likelihood accumulation with an authoritative
    override, so corroboration compounds but a stated fact is never outvoted
    by a pile of heuristics.
``classifier``
    The signals themselves, and the two-pass orchestration that lets
    adjacency evidence propagate between neighbouring rooms.

``bridge`` joins a ``cad.CadDocument`` and a set of segmented regions into
classifier inputs; it is the only module here that knows CAD types exist.
"""

from __future__ import annotations

from .bridge import build_inputs
from .classifier import (
    RoomClassification,
    RoomEvidenceInput,
    classify_plan,
    classify_room,
    summarise,
)
from .evidence import Conflict, Evidence, FusionResult, fuse
from .taxonomy import (
    ROOM_PRIORS,
    ROOM_TYPES,
    STYLEABLE_ROOM_TYPES,
    RoomPrior,
    normalise_category,
    object_evidence,
    prior,
)

__all__ = [
    "Conflict",
    "Evidence",
    "FusionResult",
    "ROOM_PRIORS",
    "ROOM_TYPES",
    "STYLEABLE_ROOM_TYPES",
    "RoomClassification",
    "RoomEvidenceInput",
    "RoomPrior",
    "build_inputs",
    "classify_plan",
    "classify_room",
    "fuse",
    "normalise_category",
    "object_evidence",
    "prior",
    "summarise",
]
