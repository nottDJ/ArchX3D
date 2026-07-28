"""
Tests for the viewpoint camera.

These conventions are the ones that go wrong silently. A mirrored yaw or an
inverted pitch produces projections that look entirely plausible and put every
object on the wrong side of the room, and the only symptom is a layout axis
confidently reporting metres of displacement that are not there. So the
conventions are pinned against the documented meaning of the schema rather
than against whatever the code happens to do.

The contract, from ``vision.schema.ViewPoint`` and ``blender.camera``:

* yaw 0 looks along **+Y**, and increases counter-clockwise;
* positive pitch looks **up**;
* image coordinates are normalised with the origin at the **top left**.
"""

from __future__ import annotations

import math

import pytest

from evaluation.projection import Camera, planar_distance
from vision.schema import Vec3, ViewPoint


def viewpoint(x=0.0, y=0.0, z=1.6, yaw=0.0, pitch=0.0, fov=60.0, aspect=16 / 9):
    return ViewPoint(image_id="v", room_id="r", position=Vec3(x, y, z),
                     yaw=yaw, pitch_deg=pitch, vertical_fov_deg=fov, aspect=aspect)


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


def test_yaw_zero_looks_along_positive_y():
    camera = Camera.from_viewpoint(viewpoint())
    assert camera.forward[0] == pytest.approx(0.0, abs=1e-9)
    assert camera.forward[1] == pytest.approx(1.0, abs=1e-9)
    assert camera.forward[2] == pytest.approx(0.0, abs=1e-9)


def test_yaw_ninety_looks_along_negative_x():
    """Counter-clockwise, matching ``SceneObject.rotation_z``."""
    camera = Camera.from_viewpoint(viewpoint(yaw=90.0))
    assert camera.forward[0] == pytest.approx(-1.0, abs=1e-9)
    assert camera.forward[1] == pytest.approx(0.0, abs=1e-9)


def test_positive_pitch_looks_upward():
    camera = Camera.from_viewpoint(viewpoint(pitch=30.0))
    assert camera.forward[2] == pytest.approx(math.sin(math.radians(30.0)), abs=1e-9)


def test_the_basis_is_orthonormal():
    camera = Camera.from_viewpoint(viewpoint(yaw=37.0, pitch=-12.0))
    for axis in (camera.right, camera.up, camera.forward):
        assert math.sqrt(sum(c * c for c in axis)) == pytest.approx(1.0, abs=1e-9)
    for a, b in ((camera.right, camera.up), (camera.up, camera.forward),
                 (camera.forward, camera.right)):
        assert sum(x * y for x, y in zip(a, b)) == pytest.approx(0.0, abs=1e-9)


def test_up_is_world_up_when_looking_horizontally():
    camera = Camera.from_viewpoint(viewpoint(yaw=45.0))
    assert camera.up[2] == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_a_point_straight_ahead_lands_at_the_centre():
    camera = Camera.from_viewpoint(viewpoint())
    u, v, depth = camera.project((0.0, 5.0, 1.6))
    assert (u, v) == (pytest.approx(0.5), pytest.approx(0.5))
    assert depth == pytest.approx(5.0)


def test_a_point_to_the_right_lands_right_of_centre():
    camera = Camera.from_viewpoint(viewpoint())
    u, _v, _d = camera.project((1.0, 5.0, 1.6))
    assert u > 0.5


def test_a_point_above_the_camera_lands_above_centre():
    """Image v grows downward, so 'above' means a smaller v."""
    camera = Camera.from_viewpoint(viewpoint())
    _u, v, _d = camera.project((0.0, 5.0, 2.6))
    assert v < 0.5


def test_a_point_behind_the_camera_does_not_project():
    """Returning a number for a point behind the lens would put phantom
    objects in the frame."""
    camera = Camera.from_viewpoint(viewpoint())
    assert camera.project((0.0, -5.0, 1.6)) is None


def test_the_frame_edge_falls_where_the_field_of_view_says():
    camera = Camera.from_viewpoint(viewpoint(fov=60.0, aspect=1.0))
    edge = math.tan(math.radians(30.0)) * 5.0          # half-FOV at 5 m
    u, _v, _d = camera.project((edge, 5.0, 1.6))
    assert u == pytest.approx(1.0, abs=1e-6)


def test_in_frame_rejects_what_falls_outside():
    camera = Camera.from_viewpoint(viewpoint(fov=60.0, aspect=1.0))
    assert camera.in_frame((0.0, 5.0, 1.6))
    assert not camera.in_frame((20.0, 5.0, 1.6))
    assert not camera.in_frame((0.0, -5.0, 1.6))


# ---------------------------------------------------------------------------
# Back-projection
# ---------------------------------------------------------------------------


def test_projection_and_back_projection_are_inverses_on_the_floor():
    """The round trip the layout axis depends on."""
    camera = Camera.from_viewpoint(viewpoint(x=1.0, y=-2.0, yaw=25.0, pitch=-10.0))
    original = (2.5, 3.0, 0.0)

    u, v, _depth = camera.project(original)
    recovered = camera.on_plane(u, v, height=0.0)

    assert recovered is not None
    assert planar_distance(recovered, original) == pytest.approx(0.0, abs=1e-6)


def test_a_ray_that_never_meets_the_floor_returns_nothing():
    """A box whose bottom edge is above the horizon is a fact about the
    detection, not an error to be papered over."""
    camera = Camera.from_viewpoint(viewpoint(pitch=20.0))
    assert camera.on_plane(0.5, 0.0, height=0.0) is None


def test_ground_position_uses_the_box_bottom_not_its_centre():
    """The bottom edge is the only part whose world position follows from the
    camera alone; the centre floats at an unknown height."""
    from vision.schema import BBox2D

    camera = Camera.from_viewpoint(viewpoint(pitch=-20.0))
    box = BBox2D(x0=0.4, y0=0.4, x1=0.6, y1=0.9)

    from_box = camera.ground_position(box)
    from_edge = camera.on_plane(0.5, 0.9, height=0.0)
    assert from_box == from_edge


def test_ground_position_of_a_missing_box_is_nothing():
    camera = Camera.from_viewpoint(viewpoint())
    assert camera.ground_position(None) is None


def test_a_nearer_object_back_projects_lower_in_the_frame():
    camera = Camera.from_viewpoint(viewpoint(pitch=-10.0))
    near = camera.project((0.0, 2.0, 0.0))
    far = camera.project((0.0, 8.0, 0.0))
    assert near[1] > far[1]


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


def test_pixel_scale_grows_with_distance():
    camera = Camera.from_viewpoint(viewpoint(fov=60.0))
    assert camera.pixel_scale_at(6.0, 640, 360) > camera.pixel_scale_at(2.0, 640, 360)


def test_planar_distance_ignores_height():
    assert planar_distance((0, 0, 0), (3, 4, 99)) == pytest.approx(5.0)


def test_a_missing_field_of_view_falls_back_rather_than_dividing_by_zero():
    camera = Camera.from_viewpoint(viewpoint(fov=0.0, aspect=0.0))
    assert camera.tan_half_vertical > 0
    assert camera.tan_half_horizontal > 0
