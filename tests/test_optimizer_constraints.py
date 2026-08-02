"""
Tests for the guardrails, and for the code that writes to a scene graph.

The constraint tests are the ones that matter most in this package. Everything
else in the optimiser can be wrong and cost an iteration; a constraint failure
costs the user's DXF geometry, or their locked objects, or a room whose
furniture has left it — and the loop would happily *accept* such a change if it
happened to raise the score, which is precisely the failure mode these rules
exist to make impossible.

The mutation tests pin the other half: that a handler changes what its action
declared and nothing else, and that it reports honestly when it changes
nothing.
"""

from __future__ import annotations

import copy
import json

import pytest

from optimizer import constraints, mutations
from planner.action_graph import Action, ActionType


def action(action_type=ActionType.LIGHTING_ADJUSTMENT, target="room:room_a",
           **kwargs):
    settings = dict(id=f"{action_type}:x", type=action_type, target=target)
    settings.update(kwargs)
    return Action(**settings)


# ---------------------------------------------------------------------------
# What may never be touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["wall", "opening", "door", "window",
                                  "geometry", "architecture"])
def test_no_action_may_target_dxf_derived_geometry(preview_graph, kind):
    report = constraints.check_action(action(target=f"{kind}:w1"), preview_graph)

    assert not report.ok
    assert report.violations[0].kind == "immutable"
    assert "DXF" in report.violations[0].detail


def test_a_locked_object_may_not_be_touched(preview_graph):
    """A lock is the user's statement of ground truth."""
    preview_graph.object_by_id("sofa_1").locked = True
    report = constraints.check_action(
        action(ActionType.FURNITURE_TRANSLATION, "object:sofa_1", objects=["sofa_1"]),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "locked_object"


def test_an_unlocked_object_may_be_touched(preview_graph):
    report = constraints.check_action(
        action(ActionType.FURNITURE_TRANSLATION, "object:sofa_1", objects=["sofa_1"]),
        preview_graph,
    )
    assert report.ok


def test_an_action_naming_an_absent_object_is_rejected(preview_graph):
    report = constraints.check_action(
        action(ActionType.FURNITURE_TRANSLATION, "object:ghost", objects=["ghost"]),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "unknown_object"


def test_a_material_outside_the_taxonomy_is_rejected_before_it_is_applied(preview_graph):
    report = constraints.check_action(
        action(ActionType.MATERIAL_ADJUSTMENT, "material:M_x",
               parameters={"species": "unobtainium"}),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "material_taxonomy"


def test_a_material_inside_the_taxonomy_passes(preview_graph):
    report = constraints.check_action(
        action(ActionType.MATERIAL_ADJUSTMENT, "material:M_x",
               parameters={"species": "light_oak"}),
        preview_graph,
    )
    assert report.ok


def test_an_unknown_style_is_rejected(preview_graph):
    report = constraints.check_action(
        action(ActionType.STYLE_REFINEMENT, "room:room_a",
               parameters={"style": "steampunk-brutalist"}),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "style_taxonomy"


def test_an_asset_from_the_wrong_category_is_rejected(preview_graph):
    preview_graph.object_by_id("sofa_1").category = "sofa"
    report = constraints.check_action(
        action(ActionType.ASSET_REPLACEMENT, "object:sofa_1", objects=["sofa_1"],
               parameters={"asset": "table_rect_tapered"}),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "asset_category"


def test_a_degenerate_object_may_not_be_admitted(preview_graph):
    from vision.schema import Dimensions, SceneObject, Vec3

    preview_graph.objects.append(SceneObject(
        id="ghost_1", category="plant", room_id="room_a", position=Vec3(1, 1, 0),
        dimensions=Dimensions(0.0, 0.0, 0.0), uncertain=True,
    ))
    report = constraints.check_action(
        action(ActionType.DECOR_DENSITY, "room:room_a",
               parameters={"admit": ["ghost_1"]}),
        preview_graph,
    )
    assert not report.ok
    assert report.violations[0].rule == "degenerate_dimensions"


# ---------------------------------------------------------------------------
# Invariants, checked after the fact
# ---------------------------------------------------------------------------


def test_a_clean_graph_satisfies_every_invariant(preview_graph):
    assert constraints.check_graph(preview_graph).ok


def test_modifying_a_wall_is_detected_after_the_fact(preview_graph):
    """The check that catches what a pre-check cannot anticipate."""
    baseline = constraints.immutable_snapshot(preview_graph)
    preview_graph.walls[0].height = 9.0

    report = constraints.check_graph(preview_graph, baseline)
    assert not report.ok
    assert report.violations[0].rule == "immutable_modified"
    assert "wall geometry" in report.violations[0].detail


def test_modifying_a_locked_object_is_detected_after_the_fact(preview_graph):
    preview_graph.object_by_id("sofa_1").locked = True
    baseline = constraints.immutable_snapshot(preview_graph)
    preview_graph.object_by_id("sofa_1").position.x += 1.0

    report = constraints.check_graph(preview_graph, baseline)
    assert not report.ok
    assert "locked" in report.violations[0].detail


def test_moving_an_unlocked_object_trips_no_immutable_rule(preview_graph):
    baseline = constraints.immutable_snapshot(preview_graph)
    preview_graph.object_by_id("sofa_1").position.x += 0.2
    assert constraints.check_graph(preview_graph, baseline).ok


def test_an_object_leaving_its_room_is_detected(preview_graph):
    preview_graph.object_by_id("sofa_1").position.x = 99.0
    report = constraints.check_graph(preview_graph)

    assert not report.ok
    assert report.violations[0].rule == "room_containment"


def test_furniture_against_a_wall_is_not_an_escape(preview_graph):
    """Room bounds are drawn around a segmented polygon; a sofa on the line is
    a sofa against a wall, not a sofa in the corridor."""
    room = preview_graph.room_by_id("room_a")
    preview_graph.object_by_id("sofa_1").position.x = room.bounds_max[0] + 0.1
    assert constraints.check_graph(preview_graph).ok


def test_a_material_outside_the_taxonomy_is_detected_in_the_graph(preview_graph):
    preview_graph.object_by_id("sofa_1").material = "unobtainium"
    report = constraints.check_graph(preview_graph)

    assert not report.ok
    assert report.violations[0].rule == "material_taxonomy"


def test_a_floor_material_on_a_wall_is_detected(preview_graph):
    """Carpet is a real material and a nonsensical wall finish."""
    from vision.schema import Finish

    preview_graph.rooms[0].wall_finish = Finish(material="carpet")
    report = constraints.check_graph(preview_graph)

    assert not report.ok
    assert report.violations[0].rule == "material_surface"


def test_a_material_valid_for_several_surfaces_is_accepted(preview_graph):
    from vision.schema import Finish

    preview_graph.rooms[0].wall_finish = Finish(material="polished_concrete")
    assert constraints.check_graph(preview_graph).ok


def test_an_unresolvable_style_is_detected(preview_graph):
    preview_graph.rooms[0].style = "steampunk-brutalist"
    report = constraints.check_graph(preview_graph)
    assert not report.ok
    assert report.violations[0].rule == "style_taxonomy"


def test_scoping_the_check_skips_untouched_rooms(preview_graph):
    preview_graph.object_by_id("table_1").material = "unobtainium"   # room_b

    assert constraints.check_graph(preview_graph, scope=["room_a"]).ok
    assert not constraints.check_graph(preview_graph, scope=["room_b"]).ok


def test_a_report_explains_itself(preview_graph):
    preview_graph.object_by_id("sofa_1").position.x = 99.0
    preview_graph.object_by_id("sofa_1").material = "unobtainium"
    report = constraints.check_graph(preview_graph)

    assert "constraints violated" in report.reason()
    assert len(report.to_dict()["violations"]) == 2


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_a_lighting_action_sets_the_planned_absolutes(preview_graph):
    result = mutations.apply(
        action(ActionType.LIGHTING_ADJUSTMENT, "room:room_a", rooms=["room_a"],
               parameters={"ambient": 0.8, "color_temperature_k": 3900.0}),
        preview_graph,
    )
    environment = preview_graph.room_by_id("room_a").lighting

    assert result.applied
    assert environment.ambient == 0.8
    assert environment.color_temperature_k == 3900.0
    # And the graph says these are no longer observations.
    assert environment.source == "optimised"


def test_a_lighting_action_clamps_to_the_fields_range(preview_graph):
    mutations.apply(
        action(ActionType.LIGHTING_ADJUSTMENT, "room:room_a", rooms=["room_a"],
               parameters={"ambient": 99.0}),
        preview_graph,
    )
    assert preview_graph.room_by_id("room_a").lighting.ambient == 1.0


def test_a_translation_moves_by_the_planned_delta(preview_graph):
    before = preview_graph.object_by_id("sofa_1").position.x
    mutations.apply(
        action(ActionType.FURNITURE_TRANSLATION, "object:sofa_1", objects=["sofa_1"],
               parameters={"dx": 0.4, "dy": -0.2}),
        preview_graph,
    )
    assert preview_graph.object_by_id("sofa_1").position.x == pytest.approx(before + 0.4)


def test_a_mutation_leaves_a_trace_on_the_object(preview_graph):
    """The graph outlives the run; a later reader deserves to know which
    values a person observed and which an optimiser chose."""
    mutations.apply(
        action(ActionType.FURNITURE_TRANSLATION, "object:sofa_1", objects=["sofa_1"],
               parameters={"dx": 0.4}),
        preview_graph,
    )
    assert any("optimiser" in flag
               for flag in preview_graph.object_by_id("sofa_1").flags)


def test_a_rotation_satisfies_the_relationship_that_prompted_it(preview_graph):
    from vision.schema import Relationship

    preview_graph.relationships.append(
        Relationship(subject="sofa_1", predicate="faces", object="table_1",
                     confidence=0.9, satisfied=False)
    )
    mutations.apply(
        action(ActionType.FURNITURE_ROTATION, "object:sofa_1", objects=["sofa_1"],
               parameters={"rotation_z": 90.0, "toward": "table_1"}),
        preview_graph,
    )
    assert preview_graph.object_by_id("sofa_1").rotation_z == 90.0
    assert preview_graph.relationships[-1].satisfied


def test_a_saturation_scale_holds_the_hue(preview_graph):
    """Doing this in RGB would change the hue, turning an intensity finding
    into a colour one."""
    from blender import colour as colour_mod

    finish = preview_graph.rooms[0].floor_finish
    before_hue = colour_mod.hls(finish.color_hex)[0]

    mutations.apply(
        action(ActionType.MATERIAL_ADJUSTMENT, "material:M_x", rooms=["room_a"],
               parameters={"saturation_scale": 1.6, "surfaces": ["floor"]}),
        preview_graph,
    )
    after = colour_mod.hls(preview_graph.rooms[0].floor_finish.color_hex)
    assert after[0] == pytest.approx(before_hue, abs=0.02)
    assert after[2] > colour_mod.hls("#D8B98C")[2]


def test_a_decor_admission_flips_only_the_withheld_flag(preview_graph):
    from vision.schema import Dimensions, SceneObject, Vec3

    preview_graph.objects.append(SceneObject(
        id="plant_1", category="plant", room_id="room_a", position=Vec3(1, 1, 0),
        dimensions=Dimensions(0.4, 0.4, 1.2), uncertain=True,
    ))
    result = mutations.apply(
        action(ActionType.DECOR_DENSITY, "room:room_a",
               parameters={"admit": ["plant_1"]}),
        preview_graph,
    )
    assert result.applied
    assert preview_graph.object_by_id("plant_1").uncertain is False
    # Nothing was created or destroyed.
    assert len(preview_graph.objects) == 3


def test_a_camera_correction_moves_the_viewpoint(preview_graph):
    before = preview_graph.viewpoints[0].position.x
    mutations.apply(
        action(ActionType.CAMERA_CORRECTION, "viewpoint:img_a1",
               parameters={"dx": -0.3, "dy": 0.1}),
        preview_graph,
    )
    assert preview_graph.viewpoints[0].position.x == pytest.approx(before - 0.3)


def test_a_style_change_records_that_it_was_adopted_not_observed(preview_graph):
    preview_graph.rooms[1].style = "unknown"
    mutations.apply(
        action(ActionType.STYLE_REFINEMENT, "room:room_b", rooms=["room_b"],
               parameters={"style": "modern", "style_confidence": 0.9}),
        preview_graph,
    )
    room = preview_graph.room_by_id("room_b")
    assert room.style == "modern"
    # Capped: adopted from elsewhere is not the same as observed here, and
    # asset matching weights this.
    assert room.style_confidence <= 0.7


def test_a_handler_reports_honestly_when_it_changes_nothing(preview_graph):
    environment = preview_graph.rooms[0].lighting
    result = mutations.apply(
        action(ActionType.LIGHTING_ADJUSTMENT, "room:room_a", rooms=["room_a"],
               parameters={"ambient": environment.ambient}),
        preview_graph,
    )
    assert not result.applied
    assert result.reason


def test_a_handler_records_every_field_it_changed(preview_graph):
    result = mutations.apply(
        action(ActionType.LIGHTING_ADJUSTMENT, "room:room_a", rooms=["room_a"],
               parameters={"ambient": 0.77, "shadow_softness": 0.2}),
        preview_graph,
    )
    fields = {name for _subject, name, _before, _after in result.changes}
    assert {"lighting.ambient", "lighting.shadow_softness"} <= fields
    assert all(len(entry) == 4 for entry in result.changes)


def test_an_unknown_action_type_changes_nothing(preview_graph):
    before = json.dumps(preview_graph.to_dict(), sort_keys=True)
    result = mutations.apply(action("teleport_the_sofa"), preview_graph)

    assert not result.applied
    assert "no handler" in result.reason
    assert json.dumps(preview_graph.to_dict(), sort_keys=True) == before


def test_a_handler_touches_only_what_its_action_declared(preview_graph):
    """A handler reaching past its declaration would defeat the pre-check,
    which validates against exactly those declarations."""
    before = copy.deepcopy(preview_graph.object_by_id("table_1").to_dict())
    mutations.apply(
        action(ActionType.FURNITURE_TRANSLATION, "object:sofa_1", objects=["sofa_1"],
               parameters={"dx": 0.5}),
        preview_graph,
    )
    assert preview_graph.object_by_id("table_1").to_dict() == before
