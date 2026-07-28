"""
Tests for incremental validation — the stage between review and generation.

The contract under test has three parts:

1. The checks are *deterministic*: no model, no network, same answer twice.
2. The default is **report-only** — an edited graph is never silently
   rearranged on its way to the renderer.
3. When correction is asked for, the user still outranks it: a locked object is
   never moved, and a hand-edited one is spared unless the caller explicitly
   waives that.
"""

from __future__ import annotations

import pytest

from vision import recheck, review
from vision.schema import (
    Dimensions,
    Opening,
    Room,
    SceneGraph,
    SceneObject,
    Vec3,
    Wall,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _object(object_id: str, category: str, x: float, y: float, **kwargs) -> SceneObject:
    return SceneObject(
        id=object_id,
        category=category,
        room_id=kwargs.pop("room_id", "r1"),
        position=Vec3(x, y, kwargs.pop("z", 0.0)),
        dimensions=kwargs.pop("dimensions", Dimensions(1.0, 0.6, 0.8)),
        confidence=kwargs.pop("confidence", 0.9),
        support=kwargs.pop("support", "floor"),
        **kwargs,
    )


@pytest.fixture
def room() -> Room:
    return Room(
        id="r1",
        room_type="living_room",
        polygon=[(0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0)],
        bounds_min=(0.0, 0.0),
        bounds_max=(6.0, 5.0),
        area=30.0,
        ceiling_height=3.0,
        wall_ids=["w1", "w2", "w3", "w4"],
    )


@pytest.fixture
def walls():
    return [
        Wall(id="w1", start=(0.0, 0.0), end=(6.0, 0.0)),
        Wall(id="w2", start=(6.0, 0.0), end=(6.0, 5.0)),
        Wall(id="w3", start=(6.0, 5.0), end=(0.0, 5.0)),
        Wall(id="w4", start=(0.0, 5.0), end=(0.0, 0.0)),
    ]


@pytest.fixture
def graph(room, walls) -> SceneGraph:
    """A room with a door in the south wall and one sofa well clear of it."""
    door = Opening(id="d1", kind="door", room_id="r1", position=Vec3(3.0, 0.0, 0.0), width=0.9)
    sofa = _object("sofa", "sofa", 1.2, 3.5, dimensions=Dimensions(2.0, 0.9, 0.8))
    return SceneGraph(rooms=[room], walls=walls, objects=[sofa], openings=[door])


def positions(graph: SceneGraph):
    return {o.id: (round(o.position.x, 4), round(o.position.y, 4)) for o in graph.objects}


# ---------------------------------------------------------------------------
# Report-only guarantee
# ---------------------------------------------------------------------------


def test_recheck_does_not_mutate_by_default(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    before = positions(graph)

    report = recheck.recheck(graph)

    assert positions(graph) == before
    assert report.to_dict()["correctable"] > 0


def test_recheck_is_deterministic(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))

    first = recheck.recheck(graph).to_dict()
    second = recheck.recheck(graph).to_dict()

    assert first == second


def test_a_clean_scene_reports_no_errors(graph):
    assert recheck.recheck(graph).errors == []


# ---------------------------------------------------------------------------
# Correction and its limits
# ---------------------------------------------------------------------------


def test_corrections_apply_when_asked(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    before = positions(graph)

    report = recheck.recheck(graph, apply_corrections=True)

    assert report.to_dict()["applied"] > 0
    assert positions(graph) != before


def test_both_halves_of_a_collision_are_reported(graph):
    """Collision resolution moves both objects; neither may go unreported."""
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))

    report = recheck.recheck(graph)
    subjects = {issue.subject for issue in report.issues if issue.suggestion}

    assert {"sofa", "chair"} <= subjects


def test_a_hand_edited_object_is_protected_from_correction(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    edited, _ = review.apply_edits(
        graph, {"object_overrides": {"chair": {"position": {"x": 1.4, "y": 3.6}}}}
    )
    before = positions(edited)["chair"]

    report = recheck.recheck(edited, apply_corrections=True)

    assert positions(edited)["chair"] == before
    assert "chair" in report.protected
    # Its partner is still free to give way.
    assert positions(edited)["sofa"] != before


def test_user_edits_can_be_overridden_explicitly(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    edited, _ = review.apply_edits(
        graph, {"object_overrides": {"chair": {"position": {"x": 1.4, "y": 3.6}}}}
    )
    before = positions(edited)["chair"]

    recheck.recheck(edited, apply_corrections=True, respect_user_edits=False)

    assert positions(edited)["chair"] != before


def test_a_lock_outranks_even_an_explicit_override(graph):
    """Locking is the strongest statement a user can make about a placement."""
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    locked, _ = review.apply_edits(
        graph, {"object_overrides": {"chair": {"locked": True}}}
    )
    before = positions(locked)["chair"]

    recheck.recheck(locked, apply_corrections=True, respect_user_edits=False)

    assert positions(locked)["chair"] == before


# ---------------------------------------------------------------------------
# Habitability checks
# ---------------------------------------------------------------------------


def test_an_object_in_the_doorway_is_an_error(graph):
    blocked, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"position": {"x": 3.0, "y": 0.5}}}}
    )

    report = recheck.recheck(blocked)
    kinds = {issue.kind for issue in report.issues}

    assert "blocked_door" in kinds
    assert any(issue.severity == "error" for issue in report.issues
               if issue.kind == "blocked_door")


def test_a_clear_doorway_produces_no_door_issue(graph):
    assert all(issue.kind != "blocked_door" for issue in recheck.recheck(graph).issues)


def test_a_gap_too_narrow_to_walk_through_is_flagged(graph, room, walls):
    """Two wardrobes 30 cm apart look passable in plan and are not."""
    scene = SceneGraph(rooms=[room], walls=walls, objects=[
        _object("a", "wardrobe", 2.0, 2.0, dimensions=Dimensions(1.0, 0.6, 2.0)),
        _object("b", "wardrobe", 2.0, 2.9, dimensions=Dimensions(1.0, 0.6, 2.0)),
    ])

    kinds = {issue.kind for issue in recheck.recheck(scene).issues}
    assert "tight_circulation" in kinds


def test_a_comfortable_gap_is_not_flagged(graph, room, walls):
    scene = SceneGraph(rooms=[room], walls=walls, objects=[
        _object("a", "wardrobe", 2.0, 1.5, dimensions=Dimensions(1.0, 0.6, 2.0)),
        _object("b", "wardrobe", 2.0, 3.5, dimensions=Dimensions(1.0, 0.6, 2.0)),
    ])

    kinds = {issue.kind for issue in recheck.recheck(scene).issues}
    assert "tight_circulation" not in kinds


def test_a_rug_is_walked_over_not_around(room, walls):
    """A rug between two chairs is not a circulation barrier."""
    scene = SceneGraph(rooms=[room], walls=walls, objects=[
        _object("a", "armchair", 2.0, 2.0),
        _object("rug", "rug", 2.0, 2.5, dimensions=Dimensions(2.0, 1.4, 0.02)),
        _object("b", "armchair", 2.0, 3.0),
    ])

    conflicts = [
        issue for issue in recheck.recheck(scene).issues
        if issue.kind == "tight_circulation"
        and "rug" in (issue.subject, issue.target)
    ]
    assert conflicts == []


def test_walling_off_the_room_is_flagged_as_unreachable(room, walls):
    """A row of wardrobes across the room cuts the far half off from the door."""
    door = Opening(id="d1", kind="door", room_id="r1", position=Vec3(3.0, 0.0, 0.0), width=0.9)
    barrier = [
        _object(f"w{i}", "wardrobe", 0.4 + i * 0.8, 2.5,
                dimensions=Dimensions(0.8, 0.6, 2.0))
        for i in range(8)
    ]
    scene = SceneGraph(rooms=[room], walls=walls, objects=barrier, openings=[door])

    kinds = {issue.kind for issue in recheck.recheck(scene).issues}
    assert "unreachable_floor" in kinds


def test_an_open_room_is_fully_reachable(graph):
    kinds = {issue.kind for issue in recheck.recheck(graph).issues}
    assert "unreachable_floor" not in kinds


def test_reachability_is_skipped_without_a_door(room, walls):
    """No recorded door means nothing to be reachable from, not a failure."""
    scene = SceneGraph(rooms=[room], walls=walls, objects=[_object("a", "sofa", 2.0, 2.0)])

    kinds = {issue.kind for issue in recheck.recheck(scene).issues}
    assert "unreachable_floor" not in kinds


def test_rooms_are_checked_independently(room, walls):
    """A problem in one room must not be attributed to another."""
    second = Room(
        id="r2", room_type="bedroom",
        polygon=[(7.0, 0.0), (12.0, 0.0), (12.0, 5.0), (7.0, 5.0)],
        bounds_min=(7.0, 0.0), bounds_max=(12.0, 5.0), area=25.0,
    )
    door = Opening(id="d1", kind="door", room_id="r1", position=Vec3(3.0, 0.0, 0.0), width=0.9)
    scene = SceneGraph(
        rooms=[room, second], walls=walls, openings=[door],
        objects=[
            _object("blocker", "wardrobe", 3.0, 0.5, dimensions=Dimensions(1.2, 0.6, 2.0)),
            _object("bed", "bed", 9.0, 2.5, room_id="r2", dimensions=Dimensions(1.6, 2.0, 0.6)),
        ],
    )

    report = recheck.recheck(scene)
    blocked = [i for i in report.issues if i.kind == "blocked_door"]

    assert blocked and all(issue.room_id == "r1" for issue in blocked)


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_serialises_for_the_api(graph):
    graph.objects.append(_object("chair", "armchair", 1.4, 3.6, confidence=0.4))
    payload = recheck.recheck(graph).to_dict()

    assert set(payload) >= {
        "total_issues", "errors", "warnings", "applied", "correctable",
        "by_kind", "protected_objects", "issues",
    }
    for issue in payload["issues"]:
        assert set(issue) >= {"kind", "severity", "subject", "detail", "room_id", "applied"}


def test_an_empty_graph_is_handled(graph):
    assert recheck.recheck(SceneGraph()).to_dict()["total_issues"] == 0
