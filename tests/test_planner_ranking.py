"""
Tests for ranking and for the dependency graph.

Ranking is a heuristic and is tested as one: the assertions are about
*ordering* and about the shape of the estimate, never about a specific
predicted gain. The optimiser measures the real thing, so a test that pinned
an estimate to three decimals would be testing an opinion.

The dependency tests are stricter, because ordering is not a heuristic. An
action measured against a state a later action is about to invalidate produces
a verdict about a world that will not exist, and every rule below names the
code that makes it true.
"""

from __future__ import annotations

from conftest import make_evaluation, make_finding
from planner import dependencies, ranking
from planner.action_graph import Action, ActionGraph, ActionType, sort_key
from planner.findings import FindingSet


def action(action_id="a", action_type=ActionType.LIGHTING_ADJUSTMENT,
           target="room:room_a", **kwargs):
    settings = dict(id=action_id, type=action_type, target=target,
                    confidence=0.8, axes=["lighting"])
    settings.update(kwargs)
    return Action(**settings)


def finding_set(**kwargs):
    return FindingSet.from_evaluation(make_evaluation([make_finding()], **kwargs))


# ---------------------------------------------------------------------------
# Gain
# ---------------------------------------------------------------------------


def test_stronger_evidence_estimates_a_larger_gain():
    weak = ranking.estimate_gain(action(confidence=0.2), finding_set())
    strong = ranking.estimate_gain(action(confidence=0.9), finding_set())
    assert strong > weak


def test_an_axis_with_no_headroom_offers_no_gain():
    """An axis already at 0.99 cannot deliver a large improvement."""
    saturated = finding_set(axis_scores={"lighting": 0.99})
    assert ranking.estimate_gain(action(), saturated) < 0.02


def test_an_unverifiable_action_is_penalised_not_dropped():
    """It may be right; the loop just could not tell."""
    measured = ranking.estimate_gain(action(), finding_set())
    unmeasured = ranking.estimate_gain(action(), finding_set(unmeasured=("lighting",)))
    assert unmeasured < measured
    assert ranking._unverifiable(action(), finding_set(unmeasured=("lighting",)))


def test_an_action_with_no_axis_falls_back_to_the_buildings_headroom():
    gain = ranking.estimate_gain(action(axes=[]), finding_set())
    assert gain > 0


def test_gain_never_leaves_zero_to_one():
    extreme = action(confidence=1.0, axes=["lighting"])
    assert 0.0 <= ranking.estimate_gain(extreme, finding_set(
        axis_scores={"lighting": 0.0})) <= 1.0


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_a_camera_correction_costs_a_fraction_of_a_rebuild():
    """It needs no Blender rebuild, and that is most of an iteration."""
    camera = action(action_type=ActionType.CAMERA_CORRECTION)
    lighting = action(action_type=ActionType.LIGHTING_ADJUSTMENT)
    assert ranking.estimate_cost(camera) < ranking.estimate_cost(lighting) / 3


def test_touching_more_objects_costs_more():
    one = action(objects=["a"])
    many = action(objects=[f"o{i}" for i in range(10)])
    assert ranking.estimate_cost(many) > ranking.estimate_cost(one)


def test_risk_rises_with_uncertainty_and_breadth():
    assert ranking.risk(action(confidence=0.2)) > ranking.risk(action(confidence=0.9))
    assert ranking.risk(action(objects=list("abcdefgh"))) > ranking.risk(action())


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def test_a_cheaper_action_outranks_an_equal_but_costlier_one():
    cheap = action("cheap", action_type=ActionType.CAMERA_CORRECTION, axes=["layout"])
    dear = action("dear", action_type=ActionType.LIGHTING_ADJUSTMENT, axes=["layout"])
    ranking.rank([cheap, dear], finding_set())
    assert cheap.priority > dear.priority


def test_ranking_is_a_total_order():
    """Two identical actions must still order the same way every run."""
    first = action("b", confidence=0.5)
    second = action("a", confidence=0.5)
    ranking.rank([first, second], finding_set())
    assert sorted([first, second], key=sort_key)[0].id == "a"


def test_plan_gain_discounts_actions_claiming_the_same_axis():
    """Five actions cannot each deliver the lighting axis's whole headroom."""
    actions = [action(f"a{i}", confidence=0.9) for i in range(5)]
    fs = finding_set()
    ranking.rank(actions, fs)

    total = ranking.plan_gain(actions, fs)
    assert total < sum(a.expected_gain for a in actions)


def test_plan_gain_is_capped_by_the_headroom_that_exists():
    actions = [action(f"a{i}", confidence=1.0, axes=["lighting"]) for i in range(8)]
    fs = finding_set(score=0.9)
    ranking.rank(actions, fs)
    assert ranking.plan_gain(actions, fs) <= 1.0 - 0.9 + 1e-9


def test_the_estimate_shows_its_working():
    a = action()
    fs = finding_set()
    ranking.rank([a], fs)
    explained = ranking.explain(a, fs)
    assert {"evidence_strength", "efficacy_prior", "axis_headroom", "cost_cycles",
            "risk", "priority"} <= set(explained)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_a_camera_correction_runs_before_the_furniture_it_measures():
    camera = action("cam", ActionType.CAMERA_CORRECTION, "viewpoint:img_a1",
                    rooms=["room_a"], priority=0.1)
    move = action("move", ActionType.FURNITURE_TRANSLATION, "object:sofa_1",
                  rooms=["room_a"], objects=["sofa_1"], priority=0.9)

    graph = dependencies.build([camera, move])
    order = [a.id for a in graph.topological_order()]
    # Even though the move ranks higher, the camera goes first.
    assert order == ["cam", "move"]


def test_ordering_binds_only_where_the_scope_overlaps():
    """A camera in the kitchen has no claim on the living room's furniture."""
    camera = action("cam", ActionType.CAMERA_CORRECTION, "viewpoint:img_b1",
                    rooms=["room_b"], priority=0.1)
    move = action("move", ActionType.FURNITURE_TRANSLATION, "object:sofa_1",
                  rooms=["room_a"], objects=["sofa_1"], priority=0.9)

    graph = dependencies.build([camera, move])
    assert graph.predecessors("move") == []
    assert [a.id for a in graph.topological_order()] == ["move", "cam"]


def test_style_settles_before_the_materials_it_re_derives():
    style = action("style", ActionType.STYLE_REFINEMENT, "room:room_a",
                   rooms=["room_a"], priority=0.1)
    material = action("mat", ActionType.MATERIAL_ADJUSTMENT, "material:M_oak",
                      rooms=["room_a"], materials=["M_oak"], priority=0.9)

    graph = dependencies.build([style, material])
    assert graph.predecessors("mat") == ["style"]


def test_the_palette_settles_before_the_materials_it_tints():
    palette = action("pal", ActionType.PALETTE_ADJUSTMENT, "room:room_a",
                     rooms=["room_a"], priority=0.1)
    material = action("mat", ActionType.MATERIAL_ADJUSTMENT, "material:M_oak",
                      rooms=["room_a"], priority=0.9)

    graph = dependencies.build([palette, material])
    assert [a.id for a in graph.topological_order()] == ["pal", "mat"]


def test_rank_orders_actions_that_are_all_ready():
    """With nothing to order them, priority decides."""
    first = action("a", target="room:room_a", rooms=["room_a"], priority=0.2)
    second = action("b", target="room:room_b", rooms=["room_b"], priority=0.8)
    graph = dependencies.build([first, second])
    assert [a.id for a in graph.topological_order()] == ["b", "a"]


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


def test_two_ways_to_fix_one_asset_are_mutually_exclusive():
    replace = action("rep", ActionType.ASSET_REPLACEMENT, "object:sofa_1",
                     objects=["sofa_1"], priority=0.9)
    swap = action("swap", ActionType.ASSET_VARIANT_SWAP, "object:sofa_1",
                  objects=["sofa_1"], priority=0.2)

    graph = dependencies.build([replace, swap])
    assert not replace.excluded
    assert swap.excluded
    assert "superseded by rep" in swap.excluded_reason
    assert graph.exclusions


def test_asset_replacement_and_rescaling_are_one_hypothesis_at_a_time():
    replace = action("rep", ActionType.ASSET_REPLACEMENT, "object:sofa_1",
                     objects=["sofa_1"], priority=0.9)
    rescale = action("scale", ActionType.FURNITURE_SCALE, "object:sofa_1",
                     objects=["sofa_1"], priority=0.3)

    dependencies.build([replace, rescale])
    assert rescale.excluded
    assert "unattributable" in rescale.excluded_reason


def test_two_actions_of_one_type_on_one_target_collapse_to_the_better():
    first = action("a", target="room:room_a", priority=0.9, rooms=["room_a"])
    second = action("b", target="room:room_a", priority=0.4, rooms=["room_a"])

    dependencies.build([first, second])
    assert not first.excluded and second.excluded


def test_the_same_type_on_different_targets_is_not_a_contradiction():
    first = action("a", target="room:room_a", rooms=["room_a"])
    second = action("b", target="room:room_b", rooms=["room_b"])
    dependencies.build([first, second])
    assert not first.excluded and not second.excluded


def test_an_excluded_action_gains_no_dependencies():
    """A dependency on something that will not run would block its dependent."""
    replace = action("rep", ActionType.ASSET_REPLACEMENT, "object:sofa_1",
                     objects=["sofa_1"], rooms=["room_a"], priority=0.9)
    swap = action("swap", ActionType.ASSET_VARIANT_SWAP, "object:sofa_1",
                  objects=["sofa_1"], rooms=["room_a"], priority=0.2)
    scale = action("scale", ActionType.FURNITURE_SCALE, "object:sofa_1",
                   objects=["sofa_1"], rooms=["room_a"], priority=0.1)

    graph = dependencies.build([replace, swap, scale])
    for edge_target in graph.edges.values():
        assert "swap" not in edge_target


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def test_a_cycle_is_broken_rather_than_raised():
    """An unexecutable plan is worse than a slightly mis-ordered one."""
    graph = ActionGraph()
    first, second = action("a", priority=0.9), action("b", priority=0.5)
    graph.add(first)
    graph.add(second)
    graph.link("a", "b", "rule one")
    graph.link("b", "a", "rule two")

    removed = graph.break_cycles()
    assert removed
    assert len(graph.topological_order()) == 2


def test_breaking_a_cycle_is_deterministic():
    def build():
        graph = ActionGraph()
        graph.add(action("a", priority=0.9))
        graph.add(action("b", priority=0.5))
        graph.add(action("c", priority=0.1))
        graph.link("a", "b", "one")
        graph.link("b", "c", "two")
        graph.link("c", "a", "three")
        graph.break_cycles()
        return [x.id for x in graph.topological_order()], graph.broken_edges

    assert build() == build()


def test_every_action_survives_a_broken_cycle():
    """Dropping an action would silently never try it."""
    graph = ActionGraph()
    for name in "abc":
        graph.add(action(name))
    graph.link("a", "b", "one")
    graph.link("b", "c", "two")
    graph.link("c", "a", "three")
    graph.break_cycles()
    assert {a.id for a in graph.topological_order()} == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_an_action_is_not_ready_until_its_predecessors_have_run():
    camera = action("cam", ActionType.CAMERA_CORRECTION, "viewpoint:img_a1",
                    rooms=["room_a"])
    move = action("move", ActionType.FURNITURE_TRANSLATION, "object:sofa_1",
                  rooms=["room_a"], objects=["sofa_1"])
    graph = dependencies.build([camera, move])

    assert [a.id for a in dependencies.ready(graph, completed=[])] == ["cam"]
    assert [a.id for a in dependencies.ready(graph, completed=["cam"],
                                             attempted=["cam"])] == ["move"]


def test_a_rejected_predecessor_still_unblocks_its_dependents():
    """The question is whether the matter is settled, not whether it helped."""
    camera = action("cam", ActionType.CAMERA_CORRECTION, "viewpoint:img_a1",
                    rooms=["room_a"])
    move = action("move", ActionType.FURNITURE_TRANSLATION, "object:sofa_1",
                  rooms=["room_a"], objects=["sofa_1"])
    graph = dependencies.build([camera, move])

    ready = dependencies.ready(graph, completed=["cam"], attempted=["cam"])
    assert [a.id for a in ready] == ["move"]


def test_dependencies_can_be_described_in_words():
    camera = action("cam", ActionType.CAMERA_CORRECTION, "viewpoint:img_a1",
                    rooms=["room_a"])
    move = action("move", ActionType.FURNITURE_TRANSLATION, "object:sofa_1",
                  rooms=["room_a"], objects=["sofa_1"])
    lines = dependencies.describe(dependencies.build([camera, move]))
    assert any("cam before move" in line for line in lines)
