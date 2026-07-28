"""
ArchX3D — Reconstruction evaluation engine
==========================================
Measures how far the generated reconstruction is from the photographs it was
built from, and — the part that matters — says *why* and *what should change*.

    reference photograph ─┐
    generated preview ────┼──► five axes ──► findings ──► subsystem to change
    scene graph ──────────┘

The engine never modifies the scene graph. It only measures. Every number it
produces is derived from inputs that already exist, so two runs over unchanged
inputs produce identical documents.

Findings, not scores
--------------------
A score says a reconstruction is 0.71 similar. A finding says the walnut floor
renders 34% too desaturated, that the material-ID pass puts it across 22% of
the frame, and that ``MaterialSpecies`` owns the fix. Only the second can be
acted on, and producing those is what this package is for. The scores remain,
because comparing one run against the previous one needs a number, but they
are a summary of the findings rather than the product.

Five axes, measured independently
---------------------------------
``colour``    palette and cast, localised per material region
``material``  saturation and texture energy, from the albedo pass
``lighting``  exposure, contrast and warmth, attributed via albedo
``layout``    visual mass, plus per-object displacement in metres
``objects``   scene-graph comparison — never image detection

The object axis is deliberately not a detector. Re-detecting furniture in the
render would compare one model's opinion with another's; the graph already
records what was seen and what was built, and the difference between them is
exact and comes with its reason attached.

Unmeasured is not zero
----------------------
An axis whose inputs were unavailable is excluded from normalisation and says
why. Scoring it zero would assert a failure that was never observed.

Layout
------
``schema``      the vocabulary: findings, axis scores, the four documents
``imaging``     image loading, colour maths, masks — the only numpy/Pillow user
``projection``  the stored ViewPoint as a projective camera
``context``     everything one axis needs about one viewpoint
``axes/``       the five measurements
``scoring``     aggregation, confidence, subsystem pressure
``report``      HTML and difference overlays
``engine``      orchestration and the four JSON documents

Entry point
-----------
``evaluation.evaluate()`` — also reachable as ``vision.similarity.evaluate()``,
which is where the rest of the pipeline expects to find it.
"""

from .engine import EvaluationConfig, Evaluator, evaluate, write_documents
from .schema import (
    AXES,
    DEFAULT_WEIGHTS,
    AxisScore,
    BuildingSummary,
    EvaluationResult,
    Finding,
    RoomEvaluation,
    ScoreSet,
    Subsystem,
    ViewpointEvaluation,
)

EVALUATION_ENGINE_VERSION = "1.0"

__all__ = [
    "EVALUATION_ENGINE_VERSION",
    "AXES",
    "DEFAULT_WEIGHTS",
    "AxisScore",
    "BuildingSummary",
    "EvaluationConfig",
    "EvaluationResult",
    "Evaluator",
    "Finding",
    "RoomEvaluation",
    "ScoreSet",
    "Subsystem",
    "ViewpointEvaluation",
    "evaluate",
    "write_documents",
]
