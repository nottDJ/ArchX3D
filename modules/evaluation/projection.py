"""
ArchX3D — Viewpoint projection
==============================
The pinhole camera a stored :class:`vision.schema.ViewPoint` describes, in
both directions: world point to image point, and image point to the world
plane it must have come from.

Why this exists
---------------
The layout axis has to answer "how far is the coffee table from where the
photograph says it should be", in metres, without detecting anything in the
render. It can, because two things are already stored:

* ``SceneObject.bbox_2d`` — where the vision pass *saw* the object in the
  reference image, in normalised image coordinates;
* ``ViewPoint`` — the camera fitted to that image.

Back-projecting the box's bottom edge onto the floor gives the position the
photograph implies. Comparing that with the object's position in the graph
measures how far the placement solver moved it — collision resolution,
relationship enforcement and wall snapping all nudge objects, and this is the
only way to see by how much.

That residual is exactly the actionable quantity. It is not "the vision model
was wrong": the graph position and the image evidence were both derived from
the same photograph, so any distance between them was introduced downstream.

Conventions, which must match ``blender.camera`` exactly
--------------------------------------------------------
A Blender camera rests looking down its local -Z. ``blender.camera`` orients
it with Euler ``(90° + pitch, 0, yaw)`` in XYZ order, so the world rotation is
``Rz(yaw) @ Rx(90° + pitch)`` and the forward direction works out to::

    forward = (-sin(yaw)·cos(pitch), cos(yaw)·cos(pitch), sin(pitch))

At yaw 0 and pitch 0 that is ``(0, 1, 0)`` — looking along +Y, which is what
the schema says yaw 0 means. Getting this wrong produces projections that look
plausible and are silently mirrored, so the derivation is spelled out and
pinned by tests rather than trusted.

Image coordinates are normalised with the origin at the **top left**, matching
``BBox2D``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

Vector3 = Tuple[float, float, float]


def _rotation(yaw_deg: float, pitch_deg: float):
    """The camera's world basis: ``(right, up, forward)``, all unit vectors."""
    yaw = math.radians(yaw_deg)
    tilt = math.radians(90.0 + pitch_deg)

    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    cos_t, sin_t = math.cos(tilt), math.sin(tilt)

    # Rz(yaw) @ Rx(tilt), applied to the camera's local axes.
    right = (cos_y, sin_y, 0.0)
    up = (-sin_y * cos_t, cos_y * cos_t, sin_t)
    forward = (sin_y * sin_t, -cos_y * sin_t, cos_t)
    # Local -Z is the view direction, so negate the third column.
    forward = (-forward[0], -forward[1], -forward[2])
    return right, up, forward


def _dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


@dataclass
class Camera:
    """A viewpoint as a projective camera.

    Built by :meth:`from_viewpoint`; nothing here estimates anything, in
    keeping with the rest of the pipeline. If a viewpoint is absent there is
    no camera and the layout axis says so.
    """

    position: Vector3
    right: Vector3
    up: Vector3
    forward: Vector3
    #: Half-angle tangents, which is the only form the projection needs.
    tan_half_vertical: float
    tan_half_horizontal: float

    @staticmethod
    def from_viewpoint(viewpoint) -> "Camera":
        right, up, forward = _rotation(viewpoint.yaw, viewpoint.pitch_deg)
        fov = max(5.0, min(150.0, viewpoint.vertical_fov_deg or 55.0))
        tan_v = math.tan(math.radians(fov) / 2.0)
        aspect = viewpoint.aspect if viewpoint.aspect and viewpoint.aspect > 0 else 16 / 9
        return Camera(
            position=(viewpoint.position.x, viewpoint.position.y, viewpoint.position.z),
            right=right,
            up=up,
            forward=forward,
            tan_half_vertical=tan_v,
            tan_half_horizontal=tan_v * aspect,
        )

    # -- world -> image -----------------------------------------------------

    def project(self, point: Vector3) -> Optional[Tuple[float, float, float]]:
        """``(u, v, depth)`` in normalised image coordinates, or ``None``.

        ``None`` when the point is behind the camera — a projection of a point
        behind the lens is a real number that means nothing, and returning it
        would put phantom objects in the frame.
        """
        offset = (
            point[0] - self.position[0],
            point[1] - self.position[1],
            point[2] - self.position[2],
        )
        depth = _dot(offset, self.forward)
        if depth <= 1e-6:
            return None

        x = _dot(offset, self.right) / (depth * self.tan_half_horizontal)
        y = _dot(offset, self.up) / (depth * self.tan_half_vertical)
        return ((x + 1.0) / 2.0, (1.0 - y) / 2.0, depth)

    def in_frame(self, point: Vector3, margin: float = 0.0) -> bool:
        """Whether a world point lands inside the rendered frame."""
        projected = self.project(point)
        if projected is None:
            return False
        u, v, _ = projected
        return -margin <= u <= 1.0 + margin and -margin <= v <= 1.0 + margin

    # -- image -> world -----------------------------------------------------

    def ray(self, u: float, v: float) -> Vector3:
        """The unit world direction through a normalised image point."""
        x = (u * 2.0 - 1.0) * self.tan_half_horizontal
        y = (1.0 - v * 2.0) * self.tan_half_vertical
        direction = (
            self.forward[0] + self.right[0] * x + self.up[0] * y,
            self.forward[1] + self.right[1] * x + self.up[1] * y,
            self.forward[2] + self.right[2] * x + self.up[2] * y,
        )
        length = math.sqrt(sum(c * c for c in direction)) or 1.0
        return (direction[0] / length, direction[1] / length, direction[2] / length)

    def on_plane(self, u: float, v: float, height: float = 0.0) -> Optional[Vector3]:
        """Where the ray through ``(u, v)`` meets the horizontal plane ``z=height``.

        The floor is the plane worth intersecting: furniture stands on it, so
        the bottom edge of a detection box is a floor contact point, and that
        is the one image feature whose depth is recoverable without any depth
        information at all.

        ``None`` when the ray runs parallel to the plane or away from it —
        which is what happens for a box whose bottom edge is above the horizon,
        and is a fact about the detection rather than an error.
        """
        direction = self.ray(u, v)
        if abs(direction[2]) < 1e-6:
            return None
        distance = (height - self.position[2]) / direction[2]
        if distance <= 0:
            return None
        return (
            self.position[0] + direction[0] * distance,
            self.position[1] + direction[1] * distance,
            height,
        )

    # -- convenience --------------------------------------------------------

    def ground_position(self, bbox, height: float = 0.0) -> Optional[Vector3]:
        """The floor point a detection box implies, from its bottom edge centre.

        The bottom edge rather than the box centre: the centre floats at an
        unknown height above the floor, and back-projecting it would need the
        object's height, which is the thing under evaluation. The bottom edge
        is where the object meets the floor, so it is the one part of the box
        whose world position follows from the camera alone.
        """
        if bbox is None:
            return None
        u = (bbox.x0 + bbox.x1) / 2.0
        return self.on_plane(u, bbox.y1, height)

    def pixel_scale_at(self, depth: float, width: int, height: int) -> float:
        """Metres per pixel at a given depth, for reporting image offsets.

        Used to phrase an image-space discrepancy in physical units when the
        analytic route is unavailable. Depends on depth, which is why it takes
        one rather than assuming a distance.
        """
        if depth <= 0 or height <= 0:
            return 0.0
        return (2.0 * depth * self.tan_half_vertical) / float(height)


def planar_distance(a: Vector3, b: Vector3) -> float:
    """Distance in the floor plane, ignoring height.

    Layout is a plan-view property: a table that is 40 cm too far from the
    window is misplaced, while one whose recorded height differs by 40 cm is a
    dimension error the object axis owns.
    """
    return math.dist((a[0], a[1]), (b[0], b[1]))
