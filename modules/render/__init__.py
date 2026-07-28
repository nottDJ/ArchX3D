"""
ArchX3D — Render evaluation pipeline
====================================
Deterministic, cacheable, low-resolution preview renders — one per stored
:class:`vision.schema.ViewPoint` — produced so the reconstruction can be
*measured* rather than admired.

What this package is for
------------------------
These images are **not** deliverables. They exist to be fed to
``vision.similarity``, which scores a render against the photograph its camera
was fitted to. That makes them evaluation instruments, and instruments have
different requirements from products:

* **Deterministic.** Two identical scenes must produce identical pixels, or a
  similarity score cannot distinguish "the material changed" from "the sampler
  rolled differently". Every stochastic knob Blender exposes is pinned; see
  :mod:`render.renderer`.
* **Cheap.** A refinement loop renders the whole building after every tweak.
  640x360 at 16 EEVEE samples, batched into a single Blender process.
* **Incremental.** Editing one sofa should re-render the previews that could
  show that sofa and nothing else. See :mod:`render.cache`.

Layering
--------
Two tiers, split on whether ``bpy`` is required — the same split the
``blender`` package uses, and for the same reason: the interesting logic is
testable without a running Blender.

``cache``, ``manifest``, ``scheduler``, ``preview``
    Pure stdlib. Hashing, invalidation, bookkeeping, orchestration. No Blender
    import, so the whole invalidation model is unit-testable.

``renderer``, ``_blender_render``
    The Blender boundary. ``renderer`` builds the job and launches
    ``blender --background scene.blend --python _blender_render.py``;
    ``_blender_render`` is the only module that imports ``bpy``.

Nothing here generates geometry. The ``.blend`` written by
``blender_generator`` is the input, loaded and rendered as-is.

Entry points
------------
``preview.render_scene``      every viewpoint in the graph
``preview.render_room``       every viewpoint in one room
``preview.render_viewpoint``  a single viewpoint

each of which writes ``preview/<room>/viewpoint_NN.png`` plus a
``manifest.json`` describing what was rendered and from what.
"""

from . import cache, manifest, scheduler  # noqa: F401  (bpy-free, always safe)

#: Bumped whenever a change to this package alters the pixels it produces.
#: It is folded into every cache key, so a bump re-renders everything exactly
#: once — which is what you want after touching the determinism settings.
#: Defined in ``renderer`` beside the settings it describes, and re-exported
#: here so there is one value rather than two that can drift apart.
from .renderer import PIPELINE_VERSION as RENDER_PIPELINE_VERSION  # noqa: E402

__all__ = [
    "RENDER_PIPELINE_VERSION",
    "cache",
    "manifest",
    "scheduler",
    "renderer",
    "preview",
]
