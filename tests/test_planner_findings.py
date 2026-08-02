"""
Tests for the planner's boundary and its root-cause grouping.

Two things are under test here, and the first is a *rule* rather than a
behaviour: findings stop at :mod:`planner.findings`. Everything downstream
works in actions. The tests below pin the adapter that enforces that, and then
the grouping that makes it worthwhile — three complaints about one room's
lighting becoming one lighting action rather than three.
"""

from __future__ import annotations

import pytest

from conftest import make_evaluation, make_finding
from evaluation.schema import Subsystem
from planner import grouping
from planner.action_graph import ActionType
from planner.findings import FindingSet


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_an_evaluation_adapts_into_a_finding_set():
    result = make_evaluation([make_finding()], score=0.62)
    finding_set = FindingSet.from_evaluation(result)

    assert len(finding_set) == 1
    assert finding_set.baseline_score == 0.62
    assert finding_set.axis_scores["lighting"] == 0.50


def test_the_adapter_copies_rather_than_references():
    """The planner must not be able to edit the evaluation's output."""
    original = make_finding()
    finding_set = FindingSet.from_evaluation(make_evaluation([original]))

    with pytest.raises(Exception):
        finding_set.findings[0].severity = 0.9      # frozen
    assert original.severity == 0.5


def test_finding_weight_combines_severity_and_confidence():
    finding_set = FindingSet.from_evaluation(
        make_evaluation([make_finding(severity=0.8, confidence=0.5)])
    )
    assert finding_set.findings[0].weight == pytest.approx(0.4)


def test_a_findings_key_is_stable_across_rephrasing():
    """The history compares runs on this, so wording must not change it."""
    first = FindingSet.from_evaluation(make_evaluation([
        make_finding(summary="Render is darker than the reference")
    ])).findings[0]
    second = FindingSet.from_evaluation(make_evaluation([
        make_finding(summary="The render came out dark")
    ])).findings[0]
    assert first.key == second.key


def test_a_findings_key_separates_different_objects():
    keys = {
        FindingSet.from_evaluation(make_evaluation([
            make_finding(subsystem=Subsystem.SCENE_GRAPH_TRANSFORM, objects=[name])
        ])).findings[0].key
        for name in ("sofa_1", "table_1")
    }
    assert len(keys) == 2


def test_scope_narrows_to_the_thing_a_finding_is_about():
    def scope(**kwargs):
        return FindingSet.from_evaluation(
            make_evaluation([make_finding(**kwargs)])
        ).findings[0].scope

    assert scope(objects=["sofa_1"]) == "object:sofa_1"
    assert scope(materials=["M_oak"]) == "material:M_oak"
    assert scope() == "room:room_a"


def test_axis_headroom_is_what_an_axis_could_still_gain():
    finding_set = FindingSet.from_evaluation(
        make_evaluation([make_finding()], axis_scores={"lighting": 0.9})
    )
    assert finding_set.axis_headroom("lighting") == pytest.approx(0.1)


def test_an_unmeasured_axis_has_no_headroom_rather_than_all_of_it():
    """An action nothing can verify is not an opportunity."""
    finding_set = FindingSet.from_evaluation(
        make_evaluation([make_finding()], unmeasured=("material",))
    )
    assert finding_set.axis_headroom("material") == 0.0
    assert "material" in finding_set.unmeasured_axes


def test_an_evaluation_with_no_findings_adapts_to_an_empty_set():
    finding_set = FindingSet.from_evaluation(make_evaluation([]))
    assert len(finding_set) == 0
    assert finding_set.total_weight() == 0.0


# ---------------------------------------------------------------------------
# Root causes
# ---------------------------------------------------------------------------


def test_three_lighting_findings_become_one_root_cause(lighting_findings):
    """The spec's example, at the grouping stage."""
    finding_set = FindingSet.from_evaluation(make_evaluation(lighting_findings))
    groups = grouping.group(finding_set)

    assert len(groups) == 1
    assert len(groups[0].findings) == 3
    assert groups[0].action_type == ActionType.LIGHTING_ADJUSTMENT


def test_the_same_subsystem_in_two_rooms_stays_two_causes():
    findings = [make_finding(room="room_a"), make_finding(room="room_b")]
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation(findings)))
    assert len(groups) == 2


def test_different_subsystems_in_one_room_stay_separate():
    findings = [
        make_finding(subsystem=Subsystem.LIGHTING_ENVIRONMENT),
        make_finding(subsystem=Subsystem.COLOUR_PALETTE, axis="colour"),
    ]
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation(findings)))
    assert {g.action_type for g in groups} == {
        ActionType.LIGHTING_ADJUSTMENT, ActionType.PALETTE_ADJUSTMENT,
    }


def test_geometry_findings_are_never_actioned():
    """The DXF is upstream of everything the optimiser may touch."""
    findings = [make_finding(subsystem=Subsystem.GEOMETRY, axis="layout")]
    finding_set = FindingSet.from_evaluation(make_evaluation(findings))

    assert grouping.group(finding_set) == []
    reasons = dict((f.summary, reason)
                   for f, reason in grouping.unactionable(finding_set))
    assert "immutable" in list(reasons.values())[0]


def test_render_settings_findings_are_never_actioned():
    """An optimiser that tuned its own scoring instrument would be cheating."""
    findings = [make_finding(subsystem=Subsystem.RENDER_SETTINGS)]
    finding_set = FindingSet.from_evaluation(make_evaluation(findings))

    assert grouping.group(finding_set) == []
    assert grouping.unactionable(finding_set)


def test_a_group_reports_the_weight_of_its_evidence(lighting_findings):
    groups = grouping.group(
        FindingSet.from_evaluation(make_evaluation(lighting_findings))
    )
    # 0.7 + 0.5 + 0.4, each times 0.8 confidence, capped at 1.
    assert groups[0].weight == pytest.approx(min(1.0, 1.6 * 0.8))


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def test_one_lighting_action_carries_every_complaint(preview_graph, lighting_findings):
    """The spec's example, at the action stage: three findings, one action."""
    groups = grouping.group(
        FindingSet.from_evaluation(make_evaluation(lighting_findings))
    )
    actions = grouping.synthesise(groups[0], preview_graph)

    assert len(actions) == 1
    action = actions[0]
    assert action.type == ActionType.LIGHTING_ADJUSTMENT
    assert action.target == "room:room_a"
    assert len(action.trigger_findings) == 3
    # All three complaints turned into parameters, not just the first.
    assert {"ambient", "color_temperature_k", "shadow_softness"} <= set(action.parameters)


def test_a_darker_render_raises_ambient(preview_graph):
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(evidence={"reference_luminance": 0.42, "render_luminance": 0.19})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    assert action.parameters["ambient"] > preview_graph.rooms[0].lighting.ambient


def test_a_brighter_render_lowers_ambient(preview_graph):
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(summary="Render is brighter than the reference",
                     evidence={"reference_luminance": 0.19, "render_luminance": 0.42})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    assert action.parameters["ambient"] < preview_graph.rooms[0].lighting.ambient


def test_a_warmer_render_raises_colour_temperature(preview_graph):
    """Higher kelvin is cooler light, so a warm render needs a higher number."""
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(summary="Render's light is warmer than the reference",
                     evidence={"warmth_difference": 0.12})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    before = preview_graph.rooms[0].lighting.color_temperature_k
    assert action.parameters["color_temperature_k"] > before


def test_moves_are_damped_rather_than_taken_whole(preview_graph):
    """A measurement is a direction, not a solved equation."""
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.SCENE_GRAPH_TRANSFORM, axis="layout",
                     objects=["sofa_1"],
                     evidence={"implied": [3.0, 1.0], "actual": [2.0, 1.0]})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    assert action.parameters["dx"] == pytest.approx(grouping.DAMPING, abs=0.01)


def test_a_translation_beyond_the_limit_is_capped(preview_graph):
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.SCENE_GRAPH_TRANSFORM, axis="layout",
                     objects=["sofa_1"],
                     evidence={"implied": [99.0, 1.0], "actual": [2.0, 1.0]})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    assert abs(action.parameters["dx"]) <= grouping.TRANSLATION_LIMIT + 1e-6


def test_a_camera_correction_moves_opposite_to_the_measured_offset(preview_graph):
    """Objects appearing shifted one way means the camera is shifted the other."""
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.CAMERA_FIT, axis="layout",
                     evidence={"offset_m": [0.4, -0.2], "coherence": 0.9})
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]
    assert action.type == ActionType.CAMERA_CORRECTION
    assert action.parameters["dx"] < 0
    assert action.parameters["dy"] > 0


def test_a_material_finding_becomes_a_saturation_correction(preview_graph):
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.MATERIAL_SPECIES, axis="material",
                     summary="wood light appears too desaturated",
                     materials=["M_wood_light_D8B98C"], room="room_a",
                     evidence={"ratio": 0.5, "coverage": 0.3})
    ])))
    actions = grouping.synthesise(groups[0], preview_graph)
    assert actions
    # The render was half as saturated, so the correction pushes it up.
    assert actions[0].parameters["saturation_scale"] > 1.0


def test_a_colour_cast_on_a_material_becomes_a_tint(preview_graph):
    """A material can be the wrong colour rather than the wrong intensity."""
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.SURFACE_FINISH, axis="colour",
                     summary="wood light is 22 dE from the reference",
                     materials=["M_wood_light_D8B98C"], room="room_a",
                     evidence={"reference_mean": "#C8A272", "render_mean": "#A79A8E"})
    ])))
    actions = grouping.synthesise(groups[0], preview_graph)
    assert actions
    assert actions[0].parameters["tint_toward"] == "#C8A272"
    assert 0 < actions[0].parameters["tint_blend"] < 1


def test_synthesis_produces_nothing_when_the_graph_cannot_support_it(preview_graph):
    """A room with no lighting environment cannot have one adjusted."""
    preview_graph.rooms[0].lighting = None
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([make_finding()])))
    assert grouping.synthesise(groups[0], preview_graph) == []


def test_synthesis_never_mutates_the_graph(preview_graph, lighting_findings):
    """The planner reads the graph. Only the optimiser writes to it."""
    import json

    before = json.dumps(preview_graph.to_dict(), sort_keys=True)
    groups = grouping.group(
        FindingSet.from_evaluation(make_evaluation(lighting_findings))
    )
    for group in groups:
        grouping.synthesise(group, preview_graph)
    assert json.dumps(preview_graph.to_dict(), sort_keys=True) == before


def test_a_stand_in_asset_yields_two_exclusive_hypotheses(preview_graph):
    """Wrong asset or wrong proportions — both are proposed, one is tried."""
    obj = preview_graph.object_by_id("sofa_1")
    obj.category = "sofa"
    obj.asset = "sofa_boxy"
    obj.asset_score = 0.2
    obj.dimensions.width = 4.0        # far from any variant's proportions

    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.ASSET_PLACEMENT, axis="objects",
                     summary="Sofa built from a stand-in asset", objects=["sofa_1"])
    ])))
    actions = grouping.synthesise(groups[0], preview_graph)
    types = {a.type for a in actions}
    assert ActionType.FURNITURE_SCALE in types
    assert types & {ActionType.ASSET_REPLACEMENT, ActionType.ASSET_VARIANT_SWAP}


def test_a_withheld_object_becomes_a_decor_admission(preview_graph):
    from vision.schema import Dimensions, SceneObject, Vec3

    preview_graph.objects.append(SceneObject(
        id="plant_1", category="plant", room_id="room_a",
        position=Vec3(1.0, 1.0, 0.0), dimensions=Dimensions(0.4, 0.4, 1.2),
        confidence=0.44, uncertain=True,
    ))
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.OBJECT_DETECTION, axis="objects",
                     summary="Plant omitted", objects=["plant_1"])
    ])))
    action = grouping.synthesise(groups[0], preview_graph)[0]

    assert action.type == ActionType.DECOR_DENSITY
    assert action.parameters["admit"] == ["plant_1"]


def test_nothing_is_admitted_that_was_never_withheld(preview_graph):
    """The optimiser admits detections; it does not invent them."""
    groups = grouping.group(FindingSet.from_evaluation(make_evaluation([
        make_finding(subsystem=Subsystem.OBJECT_DETECTION, axis="objects",
                     summary="Sofa omitted", objects=["sofa_1"])
    ])))
    # sofa_1 is already built, so there is nothing to admit.
    assert grouping.synthesise(groups[0], preview_graph) == []


def test_a_styleless_room_adopts_the_buildings_style(preview_graph):
    preview_graph.rooms[1].style = "unknown"
    finding_set = FindingSet.from_evaluation(make_evaluation([
        make_finding(axis="colour", subsystem=Subsystem.SURFACE_FINISH,
                     room="room_b", viewpoint="")
    ]))
    actions = grouping.style_actions(finding_set, preview_graph)

    assert len(actions) == 1
    assert actions[0].type == ActionType.STYLE_REFINEMENT
    assert actions[0].parameters["style"] == "modern"     # from room_a


def test_no_style_is_invented_when_the_building_has_none(preview_graph):
    for room in preview_graph.rooms:
        room.style = "unknown"
    finding_set = FindingSet.from_evaluation(make_evaluation([
        make_finding(axis="colour", subsystem=Subsystem.SURFACE_FINISH,
                     room="room_b", viewpoint="")
    ]))
    assert grouping.style_actions(finding_set, preview_graph) == []
