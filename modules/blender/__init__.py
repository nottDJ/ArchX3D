"""
ArchX3D — Blender appearance layer
==================================
Turns the scene graph's appearance information into Blender data.

The scene graph knows what the rooms look like — species-level materials, a
per-room colour palette, a lighting environment, a style, and the camera pose
of every reference photograph. Until now the generator ignored all of it and
built flat colours and a generic light rig. These modules close that gap.

Layering
--------
Two tiers, split on whether ``bpy`` is required:

``colour``, ``palette``, ``styles``
    Pure decision logic. What colour should this surface be? Is that tint
    physically believable for walnut? Which material does an industrial scene
    substitute for plain metal? No Blender import, so it runs — and is tested —
    outside Blender.

``materials``, ``lighting``, ``camera``
    Blender construction. Node graphs, light datablocks, camera objects. These
    import ``bpy`` and consume the decisions made above.

That split is what makes the appearance rules testable at all: the interesting
judgements are in the first tier, and none of them need a running Blender.

Geometry is untouched by everything here. The DXF remains the single source of
truth for shape; this package only decides how that shape is shaded and lit.
"""

from . import colour, palette, styles  # noqa: F401  (bpy-free, always safe)

__all__ = ["colour", "palette", "styles", "materials", "lighting", "camera"]
