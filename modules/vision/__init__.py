"""
ArchX3D — Vision package
========================
Reference images in, a validated scene graph out.

Stage order (see ``pipeline.py``):

    prompts + vlm  →  observe  →  fusion  →  grounding  →  relations
                                              →  assets  →  validate

Only :mod:`schema` and :mod:`catalog` are stdlib-only and therefore safe to
import from inside Blender's bundled Python; everything else runs in the host
interpreter.
"""

from .schema import (  # noqa: F401
    SCHEMA_VERSION,
    ConfidencePolicy,
    SceneGraph,
    SceneObject,
    validate_graph,
)

__all__ = [
    "SCHEMA_VERSION",
    "ConfidencePolicy",
    "SceneGraph",
    "SceneObject",
    "validate_graph",
]
