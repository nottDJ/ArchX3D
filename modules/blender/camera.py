"""
ArchX3D — Cameras
=================
Builds Blender cameras from the scene graph's stored :class:`ViewPoint`
records, plus the orbiting camera used for the walkthrough animation.

Why the stored pose matters
---------------------------
``grounding.estimate_camera`` fitted a pinhole camera to each reference
photograph in order to back-project furniture onto the floor. That pose is now
persisted. Rebuilding it here — rather than estimating a fresh one — is what
makes a render comparable with the photograph it came from: the two are the
same view, so any difference between them is a difference in the
reconstruction rather than in where the camera stood.

Nothing in this module estimates anything. If a viewpoint is missing, no
comparison camera is created for that image.

Coordinate conventions, and why they need care
----------------------------------------------
Three conventions meet here and none of them agree:

* **ArchX3D plan space** — ``yaw`` is a heading in degrees, 0 = looking along
  +Y, matching ``SceneObject.rotation_z``. ``pitch_deg`` is positive upward.
* **Blender cameras** — at rest a camera looks down its local **-Z** with +Y
  up. A camera looking horizontally along +Y therefore needs a 90° rotation
  about X *before* any heading is applied.
* **Field of view** — the graph stores a *vertical* FOV. Blender's default
  sensor fit is horizontal (``AUTO`` resolves to the larger dimension), so the
  fit must be pinned to vertical or a 16:9 render silently crops to a much
  narrower view than the photograph had.

Getting any of these wrong produces a render that looks plausible in isolation
and is useless for comparison, which is the failure mode worth guarding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import bpy

#: Focal clipping. 1 cm near plane so a camera standing inside a room does not
#: clip the wall behind it; 500 m far plane covers any plausible building.
CLIP_START = 0.01
CLIP_END = 500.0

#: Fallback vertical FOV when a viewpoint records none, degrees.
DEFAULT_VERTICAL_FOV = 55.0


@dataclass
class CameraSet:
    """Everything built, so the caller can render from each in turn."""

    walkthrough: Optional["bpy.types.Object"] = None
    #: Comparison cameras keyed by the image id they reproduce.
    by_image: Dict[str, "bpy.types.Object"] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.walkthrough is not None:
            parts.append("walkthrough orbit")
        if self.by_image:
            parts.append(f"{len(self.by_image)} reference viewpoint(s)")
        return ", ".join(parts) or "no cameras"


# ---------------------------------------------------------------------------
# Reference viewpoints
# ---------------------------------------------------------------------------


def build_viewpoint_cameras(graph) -> Dict[str, "bpy.types.Object"]:
    """One camera per stored viewpoint, reproducing it exactly.

    These are not made the active scene camera. They exist so a preview can be
    rendered from each and compared against its reference image.
    """
    cameras: Dict[str, "bpy.types.Object"] = {}

    for viewpoint in getattr(graph, "viewpoints", []) or []:
        camera = build_camera_from_viewpoint(viewpoint)
        if camera is not None:
            cameras[viewpoint.image_id] = camera

    return cameras


def build_camera_from_viewpoint(viewpoint) -> Optional["bpy.types.Object"]:
    """Recreate one fitted camera pose."""
    bpy.ops.object.camera_add(
        location=(viewpoint.position.x, viewpoint.position.y, viewpoint.position.z)
    )
    camera = bpy.context.object
    camera.name = f"Ref_{viewpoint.image_id}"
    camera.rotation_euler = viewpoint_rotation(viewpoint.yaw, viewpoint.pitch_deg)

    data = camera.data
    data.clip_start = CLIP_START
    data.clip_end = CLIP_END

    # Pin the fit to vertical: the graph stores a vertical FOV, and leaving the
    # fit on AUTO would reinterpret it as horizontal on any landscape render.
    data.sensor_fit = "VERTICAL"
    fov = viewpoint.vertical_fov_deg or DEFAULT_VERTICAL_FOV
    data.angle_y = math.radians(max(5.0, min(150.0, fov)))

    camera["archx3d_image_id"] = viewpoint.image_id
    camera["archx3d_room_id"] = viewpoint.room_id
    camera["archx3d_source_image"] = viewpoint.source_image
    camera["archx3d_aspect"] = round(viewpoint.aspect, 4)
    camera["archx3d_confidence"] = round(viewpoint.confidence, 3)
    return camera


def viewpoint_rotation(yaw_deg: float, pitch_deg: float):
    """Blender Euler angles for an ArchX3D heading and pitch.

    Derivation, since this is the part that silently goes wrong:

    A Blender camera at rest looks along its local -Z. Rotating by ``t`` about
    X sends that direction to ``(0, sin t, -cos t)``. At ``t = 90°`` it is
    ``(0, 1, 0)`` — horizontal, looking along +Y, which is ArchX3D's yaw 0. At
    ``t = 90° + p`` the Z component becomes ``sin p``, so a positive pitch
    looks upward, matching the schema.

    The Z rotation then applies the plan heading, in the same sense as
    ``SceneObject.rotation_z``.
    """
    return (
        math.radians(90.0 + pitch_deg),
        0.0,
        math.radians(yaw_deg),
    )


def render_resolution_for(viewpoint, base_width: int = 1280) -> "tuple[int, int]":
    """Pixel dimensions matching a viewpoint's aspect ratio.

    Rendering a 4:3 photograph's viewpoint into a 16:9 frame would add scene at
    the sides that the photograph never contained, which the layout axis of the
    similarity engine would then score as a difference.
    """
    aspect = viewpoint.aspect if viewpoint.aspect and viewpoint.aspect > 0 else 16 / 9
    height = int(round(base_width / aspect))
    return base_width, max(2, height)


# ---------------------------------------------------------------------------
# Walkthrough
# ---------------------------------------------------------------------------


def build_walkthrough_camera(center_x: float, center_y: float, max_dim: float,
                             config: Optional[Dict] = None) -> "bpy.types.Object":
    """The orbiting camera used for the animated flythrough.

    Unchanged in behaviour from the original generator — this is the product's
    established output and the appearance work has no reason to alter it.
    """
    config = config or {}
    render_config = config.get("render", {})
    wall_height = config.get("wall_height", 3.0)
    scene = bpy.context.scene

    distance = max_dim * 1.5
    bpy.ops.object.camera_add(
        location=(center_x, center_y - distance, wall_height * 2.5)
    )
    camera = bpy.context.active_object
    camera.name = "MainCamera"
    camera.data.lens = 28.0
    camera.data.clip_start = CLIP_START
    camera.data.clip_end = CLIP_END

    bpy.ops.object.empty_add(
        type="PLAIN_AXES", location=(center_x, center_y, wall_height * 0.4)
    )
    target = bpy.context.active_object
    target.name = "CameraTarget"

    track = camera.constraints.new(type="TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(center_x, center_y, 0.0))
    pivot = bpy.context.active_object
    pivot.name = "CameraOrbitPivot"
    camera.parent = pivot
    camera.matrix_parent_inverse = pivot.matrix_world.inverted()

    frames = render_config.get("frames", 120)
    scene.frame_start = 1
    scene.frame_end = frames
    scene.render.fps = render_config.get("fps", 24)

    pivot.rotation_euler = (0.0, 0.0, 0.0)
    pivot.keyframe_insert(data_path="rotation_euler", frame=1)
    pivot.rotation_euler = (0.0, 0.0, 2 * math.pi)
    pivot.keyframe_insert(data_path="rotation_euler", frame=frames + 1)
    _linearise(pivot)

    scene.camera = camera
    return camera


def _linearise(obj) -> None:
    """Constant-speed orbit: no ease-in or ease-out on the turntable."""
    animation = obj.animation_data
    action = animation.action if animation else None
    if action is None:
        return

    curves = list(getattr(action, "fcurves", []) or [])
    # Blender 4.4+ moves fcurves into layered slots; fall back to walking them.
    if not curves:
        for layer in getattr(action, "layers", []) or []:
            for strip in getattr(layer, "strips", []) or []:
                for bag in getattr(strip, "channelbags", []) or []:
                    curves.extend(getattr(bag, "fcurves", []) or [])

    for curve in curves:
        for keyframe in curve.keyframe_points:
            keyframe.interpolation = "LINEAR"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build(graph, center_x: float, center_y: float, max_dim: float,
          config: Optional[Dict] = None) -> CameraSet:
    """Build the walkthrough camera and every reference viewpoint camera."""
    result = CameraSet()
    result.walkthrough = build_walkthrough_camera(center_x, center_y, max_dim, config)

    if graph is not None:
        result.by_image = build_viewpoint_cameras(graph)
        if not result.by_image:
            result.notes.append(
                "no viewpoints in the graph; comparison renders unavailable"
            )

    return result
