"""
Tests for direct manipulation of objects in the review step.

Covers the transform half of ``vision.review.apply_edits`` — moving, rotating,
resizing and locking — together with the rules that keep a hand-edited scene
buildable: containment, dimension limits, support cascades, and the fact that
automatic collision correction must never move something the user pinned.

The recurring principle under test is that the user outranks the pipeline, but
only up to the point where the generator could not build the result: an
impossible edit is refused outright, a merely questionable one is accepted and
reported back.
"""

from __future__ import annotations

import pytest

from vision import review
from vision.grounding import RoomFrame
from vision.schema import Dimensions, Room, SceneGraph, SceneObject, Vec3
from vision.validate import validate_and_correct


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _object(object_id: str, category: str, x: float, y: float, **kwargs) -> SceneObject:
    dimensions = kwargs.pop("dimensions", Dimensions(1.0, 0.6, 0.8))
    return SceneObject(
        id=object_id,
        category=category,
        room_id=kwargs.pop("room_id", "r1"),
        position=Vec3(x, y, kwargs.pop("z", 0.0)),
        dimensions=dimensions,
        confidence=kwargs.pop("confidence", 0.9),
        support=kwargs.pop("support", "floor"),
        **kwargs,
    )


@pytest.fixture
def graph():
    """A 6 x 5 m living room with a sofa, a table, and a lamp on the table."""
    room = Room(
        id="r1",
        room_type="living_room",
        polygon=[(0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0)],
        bounds_min=(0.0, 0.0),
        bounds_max=(6.0, 5.0),
        area=30.0,
    )
    sofa = _object("sofa", "sofa", 1.0, 1.0, dimensions=Dimensions(2.0, 0.9, 0.8))
    table = _object(
        "table", "coffee_table", 4.0, 4.0, dimensions=Dimensions(1.2, 0.6, 0.45)
    )
    lamp = _object(
        "lamp",
        "table_lamp",
        4.5,
        4.0,
        z=0.45,
        dimensions=Dimensions(0.2, 0.2, 0.4),
        support="on_object",
        support_id="table",
    )
    return SceneGraph(rooms=[room], objects=[sofa, table, lamp])


# ---------------------------------------------------------------------------
# Moving, rotating, resizing
# ---------------------------------------------------------------------------


def test_move_updates_position_and_leaves_the_original_untouched(graph):
    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 2.5, "y": 3.0}}}}
    )

    moved = updated.object_by_id("sofa")
    assert (moved.position.x, moved.position.y) == (2.5, 3.0)
    assert report.transformed == ["sofa"]
    assert "position_set_by_user" in moved.flags
    # The graph on disk must never be mutated by an edit applied to a copy.
    assert graph.object_by_id("sofa").position.x == 1.0


def test_position_accepts_both_dict_and_pair_forms(graph):
    from_dict, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 2.0, "y": 2.0}}}}
    )
    from_pair, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": [2.0, 2.0]}}}
    )
    assert from_dict.object_by_id("sofa").position.x == (
        from_pair.object_by_id("sofa").position.x
    )


def test_rotation_is_normalised_into_a_single_turn(graph):
    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"rotation_z": 450.0}}}
    )
    assert updated.object_by_id("sofa").rotation_z == 90.0


def test_resize_accepts_a_partial_patch(graph):
    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"dimensions": {"width": 2.4}}}}
    )
    dimensions = updated.object_by_id("sofa").dimensions
    assert dimensions.width == 2.4
    # Unmentioned axes keep their detected values rather than resetting.
    assert dimensions.depth == 0.9
    assert dimensions.height == 0.8


def test_transforming_an_object_confirms_it(graph):
    """A user who positions something by hand has vouched for it."""
    graph.object_by_id("sofa").uncertain = True

    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 2.0, "y": 2.0}}}}
    )

    assert updated.object_by_id("sofa").uncertain is False
    assert report.kept == ["sofa"]


def test_combined_move_and_resize_is_judged_on_the_result(graph):
    """A resize that only fits because of a simultaneous move must be allowed."""
    updated, report = review.apply_edits(
        graph,
        {
            "object_overrides": {
                "sofa": {
                    "position": {"x": 3.0, "y": 2.5},
                    "dimensions": {"width": 3.6, "depth": 1.2},
                }
            }
        },
    )

    assert report.rejected == []
    assert updated.object_by_id("sofa").dimensions.width == 3.6


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_a_move_outside_the_room_is_refused(graph):
    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 99.0, "y": 99.0}}}}
    )

    assert report.transformed == []
    assert any("outside its room" in entry for entry in report.rejected)
    # Refusal must be total: the object keeps its original placement.
    assert updated.object_by_id("sofa").position.x == 1.0


@pytest.mark.parametrize("width", [0.0, 0.01, 25.0, float("inf")])
def test_unbuildable_dimensions_are_refused(graph, width):
    _, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"dimensions": {"width": width}}}}
    )
    assert report.transformed == []
    assert report.rejected


def test_an_object_larger_than_its_room_is_refused(graph):
    _, report = review.apply_edits(
        graph,
        {"object_overrides": {"sofa": {"dimensions": {"width": 6.0, "depth": 4.0}}}},
    )
    assert any("exceeds" in entry for entry in report.rejected)


def test_a_rejected_transform_leaves_no_partial_change(graph):
    """Position must not stick when the dimensions in the same patch are illegal."""
    updated, report = review.apply_edits(
        graph,
        {
            "object_overrides": {
                "sofa": {
                    "position": {"x": 3.0, "y": 3.0},
                    "dimensions": {"width": 99.0},
                }
            }
        },
    )

    assert report.rejected
    assert updated.object_by_id("sofa").position.x == 1.0
    assert updated.object_by_id("sofa").dimensions.width == 2.0


def test_a_deliberate_overlap_is_allowed_but_reported(graph):
    """The user outranks the validator, and is told what they did."""
    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 4.0, "y": 4.0}}}}
    )

    assert report.transformed == ["sofa"]
    assert any("overlaps" in warning for warning in report.warnings)
    assert updated.object_by_id("sofa").position.x == 4.0


def test_wall_mounted_objects_are_not_held_to_the_room_polygon(graph):
    """A wall unit sits on the boundary by definition."""
    graph.objects.append(
        _object("tv", "tv", 3.0, 3.0, support="wall", dimensions=Dimensions(1.2, 0.1, 0.7))
    )

    _, report = review.apply_edits(
        graph, {"object_overrides": {"tv": {"position": {"x": 6.0, "y": 2.5}}}}
    )
    assert report.transformed == ["tv"]


# ---------------------------------------------------------------------------
# Support cascades
# ---------------------------------------------------------------------------


def test_moving_a_table_carries_what_rests_on_it(graph):
    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"table": {"position": {"x": 2.0, "y": 2.0}}}}
    )

    lamp = updated.object_by_id("lamp")
    # The lamp began 0.5 m to the +x side of the table and must stay there.
    assert lamp.position.x == pytest.approx(2.5)
    assert lamp.position.y == pytest.approx(2.0)


@pytest.mark.parametrize(
    "degrees, expected",
    [(90.0, (4.0, 4.5)), (180.0, (3.5, 4.0)), (270.0, (4.0, 3.5))],
)
def test_rotating_a_table_orbits_what_rests_on_it(graph, degrees, expected):
    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"table": {"rotation_z": degrees}}}
    )

    lamp = updated.object_by_id("lamp")
    assert lamp.position.x == pytest.approx(expected[0], abs=1e-6)
    assert lamp.position.y == pytest.approx(expected[1], abs=1e-6)
    assert lamp.rotation_z == pytest.approx(degrees)


def test_a_stack_follows_to_its_full_depth(graph):
    """A cup on a tray on a table must survive moving the table."""
    graph.objects.append(
        _object(
            "tray", "tray", 4.0, 4.0, z=0.45,
            dimensions=Dimensions(0.4, 0.3, 0.05),
            support="on_object", support_id="table",
        )
    )
    graph.objects.append(
        _object(
            "cup", "cup", 4.0, 4.0, z=0.50,
            dimensions=Dimensions(0.1, 0.1, 0.12),
            support="on_object", support_id="tray",
        )
    )

    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"table": {"position": {"x": 1.5, "y": 1.5}}}}
    )

    assert updated.object_by_id("tray").position.x == pytest.approx(1.5)
    assert updated.object_by_id("cup").position.x == pytest.approx(1.5)


def test_children_are_re_seated_on_the_parents_surface(graph):
    """Raising a table must lift the lamp with it, not leave it inside the top."""
    before = graph.object_by_id("lamp").position.z

    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"table": {"dimensions": {"height": 0.75}}}}
    )

    assert updated.object_by_id("lamp").position.z > before


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def test_locking_is_reported_and_persisted(graph):
    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"locked": True}}}
    )

    assert report.lock_changed == ["sofa"]
    assert updated.object_by_id("sofa").locked is True


def test_a_locked_object_refuses_transforms(graph):
    locked, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"locked": True}}}
    )

    updated, report = review.apply_edits(
        locked, {"object_overrides": {"sofa": {"position": {"x": 3.0, "y": 3.0}}}}
    )

    assert report.transformed == []
    assert any("locked" in entry for entry in report.rejected)
    assert updated.object_by_id("sofa").position.x == 1.0


def test_unlocking_and_moving_in_one_edit_is_allowed(graph):
    """The UI offers "unlock and drag" as a single gesture."""
    locked, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"locked": True}}}
    )

    updated, report = review.apply_edits(
        locked,
        {"object_overrides": {"sofa": {"locked": False, "position": {"x": 3.0, "y": 3.0}}}},
    )

    assert report.rejected == []
    assert report.transformed == ["sofa"]
    assert updated.object_by_id("sofa").locked is False


def test_locked_objects_survive_automatic_collision_correction(graph):
    """The whole point of the lock: the validator must not undo the user."""
    locked, _ = review.apply_edits(
        graph,
        {
            "object_overrides": {
                # Park the sofa on top of the table, then pin it there.
                "sofa": {"position": {"x": 4.0, "y": 4.0}, "locked": True}
            }
        },
    )
    placed = locked.object_by_id("sofa")
    room = locked.rooms[0]
    frame = RoomFrame(
        polygon=room.polygon,
        bounds_min=room.bounds_min,
        bounds_max=room.bounds_max,
        ceiling_height=room.ceiling_height,
    )

    validate_and_correct(locked, frame)

    assert locked.object_by_id("sofa").position.x == pytest.approx(placed.position.x)
    assert locked.object_by_id("sofa").position.y == pytest.approx(placed.position.y)
    # The unpinned partner is the one that gives way.
    assert locked.object_by_id("table").position.x != pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_an_unrecognised_override_key_is_reported(graph):
    """A UI typo must surface rather than silently dropping the user's edit."""
    _, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"positon": {"x": 1.0, "y": 1.0}}}}
    )
    assert any("positon" in entry for entry in report.rejected)


@pytest.mark.parametrize(
    "patch",
    [
        {"position": "over there"},
        {"position": {"x": "left"}},
        {"rotation_z": "sideways"},
        {"dimensions": 3},
    ],
)
def test_malformed_transform_values_are_reported_not_crashed(graph, patch):
    _, report = review.apply_edits(graph, {"object_overrides": {"sofa": patch}})
    assert report.transformed == []
    assert report.rejected


def test_review_payload_exposes_lock_state_and_vocabulary(graph):
    locked, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"locked": True}}}
    )
    payload = review.build_review(locked)

    sofa = next(
        o for room in payload["rooms"] for o in room["objects"] if o["id"] == "sofa"
    )
    assert sofa["locked"] is True
    assert sofa["support"] == "floor"
    # The UI's dropdowns are driven by the server's own vocabularies.
    assert "living_room" in payload["vocabulary"]["room_types"]
    assert any(
        entry["category"] == "sofa" for entry in payload["vocabulary"]["categories"]
    )


def test_a_user_created_overlap_warns_until_it_is_fixed(graph):
    """The warning is recomputed from the graph, so it clears when resolved."""
    overlapping, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 4.0, "y": 4.0}}}}
    )
    assert any(
        "overlaps" in warning for warning in review.build_review(overlapping)["warnings"]
    )

    resolved, _ = review.apply_edits(
        overlapping, {"object_overrides": {"sofa": {"position": {"x": 1.0, "y": 1.0}}}}
    )
    assert not any(
        "overlaps" in warning for warning in review.build_review(resolved)["warnings"]
    )


def test_transforms_survive_a_save_and_reload(graph, tmp_path):
    """Generation reads the graph back off disk, so the edit must persist."""
    updated, _ = review.apply_edits(
        graph,
        {
            "object_overrides": {
                "sofa": {
                    "position": {"x": 2.5, "y": 3.0},
                    "rotation_z": 45.0,
                    "locked": True,
                }
            }
        },
    )

    path = tmp_path / "scene_graph.json"
    updated.save(str(path))
    reloaded = SceneGraph.load(str(path))

    sofa = reloaded.object_by_id("sofa")
    assert sofa.position.x == pytest.approx(2.5)
    assert sofa.rotation_z == pytest.approx(45.0)
    assert sofa.locked is True
