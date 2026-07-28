"""
ArchX3D — Evaluation context
============================
Everything one axis needs to measure one viewpoint, assembled once and passed
around read-only.

The axes are deliberately dumb about where their inputs came from: they are
handed a reference image, a render, whichever passes exist, the scene graph,
and a camera. That keeps each axis a pure function of its context — which is
what makes them testable one at a time, and what stops the engine from
acquiring five slightly different opinions about which reference image belongs
to which render.

Nothing here mutates the scene graph. The context holds a reference to it and
every axis treats it as immutable; the engine only measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import imaging
from .projection import Camera


@dataclass
class ViewContext:
    """One reference photograph, its render, and the scene behind them."""

    viewpoint_id: str = ""
    room_id: str = ""
    #: ``vision.schema.ViewPoint``.
    viewpoint: Any = None
    #: ``vision.schema.SceneGraph`` — read only, never modified.
    graph: Any = None
    #: ``vision.schema.Room`` for :attr:`room_id`, when it resolves.
    room: Any = None
    #: The loaded images.
    pair: imaging.ImagePair = field(default_factory=imaging.ImagePair)
    #: ``render.passes.IndexMap`` — which index means which object/material.
    index_map: Any = None
    depth_range: float = 20.0
    camera: Optional[Camera] = None
    #: Evaluation configuration (thresholds, weights).
    config: Any = None

    # -- availability -------------------------------------------------------

    @property
    def has_pixels(self) -> bool:
        """Both images loaded, so a pixel comparison is possible at all."""
        return imaging.available() and self.pair.ok

    def has_pass(self, *names: str) -> bool:
        return self.pair.has(*names)

    def missing(self, *names: str) -> str:
        """A reason string for an axis that cannot run. Empty when it can."""
        if not imaging.available():
            return imaging.unavailable_reason()
        if not self.pair.ok:
            return self.pair.notes[0] if self.pair.notes else "images unavailable"
        absent = [name for name in names if not self.pair.has(name)]
        if absent:
            return f"{', '.join(absent)} pass not rendered"
        return ""

    # -- regions ------------------------------------------------------------

    def material_regions(self) -> Dict[str, Any]:
        """Masks of the render keyed by *material name*, largest first.

        The masks come from the render, and are applied to the reference as
        well. That is an assumption worth being explicit about: it holds while
        the two images broadly agree on where surfaces are, which is true of
        walls, floors and ceilings — the large architectural regions this is
        used for — and degrades where the reconstruction is grossly misplaced.
        When it does degrade, the layout axis is the one that reports it, so
        the failure is visible rather than silent.
        """
        return self._named_regions("material_id", "materials")

    def object_regions(self) -> Dict[str, Any]:
        """Masks of the render keyed by scene-graph object id, largest first."""
        return self._named_regions("object_id", "objects")

    def _named_regions(self, pass_name: str, kind: str) -> Dict[str, Any]:
        raw = self.pair.passes.get(pass_name)
        if raw is None or self.index_map is None:
            return {}
        indices = imaging.index_plane(raw)
        masks = imaging.region_masks(indices)
        lookup = (self.index_map.materials if kind == "materials"
                  else self.index_map.objects)

        named: Dict[str, Any] = {}
        for index, mask in masks.items():
            name = lookup.get(index)
            if not name:
                # An index with no entry means the pass and the map disagree —
                # a stale manifest, most likely. Skipping is right: a mask
                # nobody can name cannot appear in a finding anyway.
                continue
            if name in named:
                named[name] = named[name] | mask
            else:
                named[name] = mask
        return dict(
            sorted(named.items(), key=lambda kv: -imaging.fraction(kv[1]))
        )

    # -- graph queries ------------------------------------------------------

    def observed_objects(self) -> List[Any]:
        """Objects the vision pass saw *in this viewpoint's own photograph*.

        Matched on ``source_images`` rather than on room membership: the
        question a viewpoint answers is "what should be visible in this shot",
        and an object in the same room behind the camera is not missing from
        it. Objects with no recorded source fall back to room membership,
        because a graph that lost its provenance is still worth evaluating.
        """
        if self.graph is None:
            return []
        image_id = self.viewpoint_id
        objects = []
        for obj in self.graph.objects:
            sources = list(getattr(obj, "source_images", []) or [])
            if sources:
                if image_id in sources:
                    objects.append(obj)
            elif obj.room_id and obj.room_id == self.room_id:
                objects.append(obj)
        return objects

    def visible_objects(self) -> List[Any]:
        """Observed objects whose graph position actually lands in frame.

        An object can be recorded against this image and still project outside
        the rendered frame — the fitted camera is an approximation, and a wide
        photograph cropped to the render's aspect loses its edges. Judging
        those as "missing from the render" would be blaming the reconstruction
        for the framing, so they are excluded and counted separately.
        """
        if self.camera is None:
            return self.observed_objects()
        visible = []
        for obj in self.observed_objects():
            point = (obj.position.x, obj.position.y, obj.position.z)
            if self.camera.in_frame(point, margin=0.15):
                visible.append(obj)
        return visible

    def palette(self):
        """The room's colour palette, or ``None`` when it has none.

        Rooms without an observed palette are common — the vision pass only
        records one when it saw enough to be sure — so every caller has to
        cope with ``None`` rather than assume a default that was never
        observed.
        """
        room = self.room
        return getattr(room, "palette", None) if room is not None else None

    def lighting(self):
        room = self.room
        return getattr(room, "lighting", None) if room is not None else None
