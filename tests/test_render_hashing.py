"""
Tests for scene hashing — the invalidation model of the render pipeline.

These are the tests that matter most in this package. Everything else is
plumbing; the hashes decide whether a refinement loop takes half a second or
half a minute, and whether an evaluation image is stale. Both failure modes
are silent, which is exactly why they are pinned here.

The shape of every test is the same: take a graph, change one thing, and
assert on *which* digests moved. A test that only checked "the hash changed"
would pass just as happily for a hash that changes on everything, which is the
degenerate cache this design exists to avoid.
"""

from __future__ import annotations

import copy

import pytest

from render import cache
from render.renderer import RenderSettings


@pytest.fixture
def hashes(preview_graph):
    return cache.compute(preview_graph, settings_fingerprint="fixed")


def rehash(graph):
    return cache.compute(graph, settings_fingerprint="fixed")


# ---------------------------------------------------------------------------
# Determinism of the hash itself
# ---------------------------------------------------------------------------


def test_identical_graphs_hash_identically(preview_graph, hashes):
    again = rehash(copy.deepcopy(preview_graph))
    assert again.scene == hashes.scene
    assert again.rooms == hashes.rooms


def test_record_order_does_not_affect_the_hash(preview_graph, hashes):
    """The vision pipeline analyses images concurrently, so graph order varies.

    If order leaked into the hash, an unchanged scene would re-render on every
    second run and the cache would never hit in practice.
    """
    shuffled = copy.deepcopy(preview_graph)
    shuffled.objects.reverse()
    shuffled.lights.reverse()
    shuffled.walls.reverse()
    assert rehash(shuffled).rooms == hashes.rooms


def test_float_noise_below_a_micrometre_is_ignored(preview_graph, hashes):
    """Round-tripping the graph through JSON must not invalidate anything."""
    nudged = copy.deepcopy(preview_graph)
    nudged.objects[0].position.x += 1e-9
    assert rehash(nudged).rooms == hashes.rooms


def test_a_moved_object_does_change_its_room(preview_graph, hashes):
    moved = copy.deepcopy(preview_graph)
    moved.objects[0].position.x += 0.25
    assert rehash(moved).rooms["room_a"] != hashes.rooms["room_a"]


# ---------------------------------------------------------------------------
# Regression: a material change reaches only the room that owns it
# ---------------------------------------------------------------------------


def test_material_change_is_confined_to_its_room(preview_graph, hashes):
    changed = copy.deepcopy(preview_graph)
    changed.objects[0].material = "leather"

    after = rehash(changed)
    assert after.rooms["room_a"] != hashes.rooms["room_a"]
    assert after.rooms["room_b"] == hashes.rooms["room_b"]
    assert after.scene == hashes.scene


def test_room_finish_change_is_confined_to_its_room(preview_graph, hashes):
    changed = copy.deepcopy(preview_graph)
    changed.rooms[0].wall_finish.color_hex = "#334455"

    after = rehash(changed)
    assert after.rooms["room_a"] != hashes.rooms["room_a"]
    assert after.rooms["room_b"] == hashes.rooms["room_b"]


def test_palette_change_is_confined_to_its_room(preview_graph, hashes):
    changed = copy.deepcopy(preview_graph)
    changed.rooms[1].palette.accent = "#FF0000"

    after = rehash(changed)
    assert after.rooms["room_b"] != hashes.rooms["room_b"]
    assert after.rooms["room_a"] == hashes.rooms["room_a"]


# ---------------------------------------------------------------------------
# Regression: a lighting change reaches only the affected room
# ---------------------------------------------------------------------------


def test_luminaire_change_is_confined_to_its_room(preview_graph, hashes):
    changed = copy.deepcopy(preview_graph)
    changed.lights[0].power_w = 120.0

    after = rehash(changed)
    assert after.rooms["room_a"] != hashes.rooms["room_a"]
    assert after.rooms["room_b"] == hashes.rooms["room_b"]
    assert after.scene == hashes.scene


def test_lighting_environment_change_is_confined_to_its_room(preview_graph, hashes):
    changed = copy.deepcopy(preview_graph)
    changed.rooms[1].lighting.time_of_day = "night"
    changed.rooms[1].lighting.ambient = 0.1

    after = rehash(changed)
    assert after.rooms["room_b"] != hashes.rooms["room_b"]
    assert after.rooms["room_a"] == hashes.rooms["room_a"]


# ---------------------------------------------------------------------------
# Building-wide changes
# ---------------------------------------------------------------------------


def test_graph_level_floor_finish_invalidates_the_building(preview_graph, hashes):
    """The floor is shared, so a change to it is legitimately global."""
    changed = copy.deepcopy(preview_graph)
    changed.floor.color_hex = "#111111"
    assert rehash(changed).scene != hashes.scene


def test_unattributed_object_invalidates_the_building(preview_graph, hashes):
    """An object with no room could appear in any view, so it must be global.

    Conservative on purpose: a needless re-render costs milliseconds, a stale
    evaluation image costs a wrong score.
    """
    changed = copy.deepcopy(preview_graph)
    changed.objects[0].room_id = ""

    after = rehash(changed)
    assert after.scene != hashes.scene
    assert after.rooms["room_a"] != hashes.rooms["room_a"]  # it left this room


def test_adding_a_room_invalidates_the_building(preview_graph, hashes):
    from vision.schema import Room

    changed = copy.deepcopy(preview_graph)
    changed.rooms.append(Room(id="room_c", area=8.0))
    assert rehash(changed).scene != hashes.scene


def test_geometry_file_is_part_of_the_scene_hash(preview_graph, tmp_path):
    """A re-extracted DXF changes every wall, so it must invalidate everything."""
    geometry = tmp_path / "geometry.json"
    geometry.write_text('{"walls": []}', encoding="utf-8")
    before = cache.compute(preview_graph, geometry_path=str(geometry))

    geometry.write_text('{"walls": [1]}', encoding="utf-8")
    after = cache.compute(preview_graph, geometry_path=str(geometry))

    assert before.scene != after.scene


def test_missing_geometry_hashes_stably(preview_graph):
    """An absent geometry file is a state, not an error, and must be stable."""
    a = cache.compute(preview_graph, geometry_path="does/not/exist.json")
    b = cache.compute(preview_graph, geometry_path="does/not/exist.json")
    assert a.scene == b.scene


def test_render_settings_invalidate_everything(preview_graph):
    """Changing the sample count changes every pixel of every preview."""
    low = cache.compute(preview_graph, settings_fingerprint=RenderSettings(samples=8).fingerprint())
    high = cache.compute(preview_graph, settings_fingerprint=RenderSettings(samples=64).fingerprint())
    assert low.scene != high.scene


# ---------------------------------------------------------------------------
# Regression: a camera change reaches only its own viewpoint
# ---------------------------------------------------------------------------


def test_camera_hash_ignores_everything_but_pose_and_framing(preview_graph):
    viewpoint = preview_graph.viewpoints[0]
    before = cache.camera_hash(viewpoint, 640, 360)

    viewpoint.confidence = 0.1
    viewpoint.source_image = "renamed.jpg"
    assert cache.camera_hash(viewpoint, 640, 360) == before


@pytest.mark.parametrize("field, value", [
    ("yaw", 90.0),
    ("pitch_deg", 12.0),
    ("vertical_fov_deg", 70.0),
])
def test_camera_pose_changes_the_camera_hash(preview_graph, field, value):
    viewpoint = preview_graph.viewpoints[0]
    before = cache.camera_hash(viewpoint, 640, 360)
    setattr(viewpoint, field, value)
    assert cache.camera_hash(viewpoint, 640, 360) != before


def test_camera_position_changes_the_camera_hash(preview_graph):
    viewpoint = preview_graph.viewpoints[0]
    before = cache.camera_hash(viewpoint, 640, 360)
    viewpoint.position.z += 0.3
    assert cache.camera_hash(viewpoint, 640, 360) != before


def test_resolution_is_part_of_the_camera_hash(preview_graph):
    viewpoint = preview_graph.viewpoints[0]
    assert cache.camera_hash(viewpoint, 640, 360) != cache.camera_hash(viewpoint, 1280, 720)


def test_moving_one_camera_leaves_the_scene_and_room_hashes_alone(preview_graph, hashes):
    """The point of the whole split: a camera edit is not a scene edit."""
    changed = copy.deepcopy(preview_graph)
    changed.viewpoints[0].yaw = 45.0

    after = rehash(changed)
    assert after.scene == hashes.scene
    assert after.rooms == hashes.rooms


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_render_key_combines_all_three_digests():
    base = cache.render_key("scene", "room", "camera", "preview/a/viewpoint_01.png")
    assert base != cache.render_key("scene2", "room", "camera", "preview/a/viewpoint_01.png")
    assert base != cache.render_key("scene", "room2", "camera", "preview/a/viewpoint_01.png")
    assert base != cache.render_key("scene", "room", "camera2", "preview/a/viewpoint_01.png")


def test_render_key_tracks_the_output_path(preview_graph):
    """Renumbering a room's viewpoints must not leave a stale file in place."""
    a = cache.render_key("s", "r", "c", "preview/room_a/viewpoint_01.png")
    b = cache.render_key("s", "r", "c", "preview/room_a/viewpoint_02.png")
    assert a != b


def test_render_key_is_platform_independent():
    """A manifest written on Windows must key the same on a Linux farm node."""
    assert (cache.render_key("s", "r", "c", "preview\\room_a\\viewpoint_01.png")
            == cache.render_key("s", "r", "c", "preview/room_a/viewpoint_01.png"))


def test_unknown_room_falls_back_to_the_scene_hash(hashes):
    assert hashes.for_room("no_such_room") == hashes.scene


# ---------------------------------------------------------------------------
# Neighbour folding (opt-in)
# ---------------------------------------------------------------------------


def test_neighbour_folding_propagates_one_hop(preview_graph):
    """With it on, repainting a room reaches the rooms that can see into it."""
    preview_graph.rooms[0].connected_to = ["room_b"]
    preview_graph.rooms[1].connected_to = ["room_a"]

    before = cache.with_neighbours(rehash(preview_graph), preview_graph)

    changed = copy.deepcopy(preview_graph)
    changed.objects[1].material = "marble"          # an object in room_b
    after = cache.with_neighbours(rehash(changed), changed)

    assert after.rooms["room_b"] != before.rooms["room_b"]
    assert after.rooms["room_a"] != before.rooms["room_a"]


def test_neighbour_folding_is_off_by_default(preview_graph):
    preview_graph.rooms[0].connected_to = ["room_b"]
    before = rehash(preview_graph)

    changed = copy.deepcopy(preview_graph)
    changed.objects[1].material = "marble"          # an object in room_b
    assert rehash(changed).rooms["room_a"] == before.rooms["room_a"]


# ---------------------------------------------------------------------------
# Canonicalisation edge cases
# ---------------------------------------------------------------------------


def test_canonical_form_is_key_order_independent():
    assert cache.canonical({"a": 1, "b": 2}) == cache.canonical({"b": 2, "a": 1})


def test_non_finite_floats_do_not_break_a_digest():
    """VLM output reaches the graph with NaNs in it; a hash must survive one."""
    assert cache.digest({"x": float("nan")}) == cache.digest({"x": float("nan")})
    assert cache.digest({"x": float("inf")}) != cache.digest({"x": 0.0})


def test_negative_zero_hashes_as_zero():
    assert cache.digest({"x": -0.0}) == cache.digest({"x": 0.0})
