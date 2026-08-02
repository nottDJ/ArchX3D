"""
End-to-end tests for the planner, and for the whole refinement pass.

The planner is judged on four things here: it produces the actions the spec's
example calls for, it never touches the scene graph, it produces the same plan
twice, and it says out loud what it decided not to do. The last matters as
much as the first — a plan that silently omits half the findings looks exactly
like a plan that solved them.
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import make_evaluation, make_finding
from evaluation.schema import Subsystem
from planner import Planner, PlannerConfig, plan as plan_fn, write_report
from planner.action_graph import ActionType


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def test_the_specs_example_produces_one_action(preview_graph, lighting_findings):
    """Too warm, too dark, wrong shadows — one LightingEnvironment update."""
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings))

    lighting = plan.of_type(ActionType.LIGHTING_ADJUSTMENT)
    assert len(lighting) == 1
    assert len(lighting[0].trigger_findings) == 3
    assert plan.diagnostics["findings_merged"] == 3


def test_a_plan_carries_its_baseline_and_expectation(preview_graph, lighting_findings):
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings, score=0.62))

    assert plan.baseline_score == 0.62
    assert 0 < plan.expected_total_gain <= 1.0 - 0.62 + 1e-9


def test_a_plan_orders_by_priority_within_its_dependencies(preview_graph):
    findings = [
        make_finding(severity=0.7),
        make_finding(axis="layout", code="displacement",
                     subsystem=Subsystem.SCENE_GRAPH_TRANSFORM,
                     summary="sofa sits 62 cm from where the reference places it",
                     objects=["sofa_1"], severity=0.3,
                     evidence={"implied": [2.6, 1.5], "actual": [2.0, 1.0]}),
    ]
    plan = Planner(preview_graph).plan(make_evaluation(findings))
    priorities = [a.priority for a in plan.ordered]
    assert priorities == sorted(priorities, reverse=True)


def test_the_planner_never_touches_the_graph(preview_graph, lighting_findings):
    """Reading it to compute absolute values is the whole of its access."""
    before = json.dumps(preview_graph.to_dict(), sort_keys=True)
    Planner(preview_graph).plan(make_evaluation(lighting_findings))
    assert json.dumps(preview_graph.to_dict(), sort_keys=True) == before


def test_an_evaluation_with_no_findings_produces_an_empty_plan(preview_graph):
    plan = Planner(preview_graph).plan(make_evaluation([]))

    assert len(plan) == 0
    assert any("no findings" in note for note in plan.notes)


def test_actions_carry_everything_the_spec_requires(preview_graph, lighting_findings):
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings))
    action = plan.ordered[0].to_dict()

    for key in ("trigger_findings", "expected_gain", "cost", "priority",
                "rooms", "objects", "materials", "rationale", "summary"):
        assert key in action, key
    assert action["rationale"]


# ---------------------------------------------------------------------------
# Filtering, and saying so
# ---------------------------------------------------------------------------


def test_an_unverifiable_action_is_dropped_with_a_reason(preview_graph,
                                                          lighting_findings):
    """Nothing measured the lighting axis, so the loop could not judge it."""
    evaluation = make_evaluation(lighting_findings, unmeasured=("lighting",))
    plan = Planner(preview_graph).plan(evaluation)

    assert plan.of_type(ActionType.LIGHTING_ADJUSTMENT) == []
    dropped = plan.excluded
    assert dropped and "could not be verified" in dropped[0].excluded_reason


def test_unverifiable_actions_can_be_allowed_deliberately(preview_graph,
                                                           lighting_findings):
    evaluation = make_evaluation(lighting_findings, unmeasured=("lighting",))
    config = PlannerConfig(include_unverifiable=True, min_expected_gain=0.0)
    plan = Planner(preview_graph, config).plan(evaluation)
    assert plan.of_type(ActionType.LIGHTING_ADJUSTMENT)


def test_an_action_type_can_be_switched_off(preview_graph, lighting_findings):
    config = PlannerConfig(allowed_types=(ActionType.MATERIAL_ADJUSTMENT,))
    plan = Planner(preview_graph, config).plan(make_evaluation(lighting_findings))

    assert plan.ordered == []
    assert "not enabled" in plan.excluded[0].excluded_reason


def test_a_plan_is_capped_and_says_where_it_stopped(preview_graph):
    findings = [
        make_finding(axis="layout", code="displacement",
                     subsystem=Subsystem.SCENE_GRAPH_TRANSFORM,
                     summary=f"object {index} is misplaced",
                     objects=[object_id], severity=0.6,
                     evidence={"implied": [3.0, 1.5], "actual": [2.0, 1.0]})
        for index, object_id in enumerate(("sofa_1", "table_1"))
    ]
    plan = Planner(preview_graph, PlannerConfig(max_actions=1)).plan(
        make_evaluation(findings)
    )
    assert len(plan.ordered) == 1
    assert any("plan limit" in a.excluded_reason for a in plan.excluded)


def test_unactionable_findings_are_named_in_the_diagnostics(preview_graph):
    findings = [make_finding(subsystem=Subsystem.GEOMETRY, axis="layout")]
    plan = Planner(preview_graph).plan(make_evaluation(findings))

    unactionable = plan.diagnostics["unactionable"]
    assert unactionable
    assert "immutable" in unactionable[0]["reason"]


def test_the_diagnostics_show_how_every_estimate_was_reached(preview_graph,
                                                              lighting_findings):
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings))
    estimate = plan.diagnostics["estimates"][plan.ordered[0].id]

    assert {"evidence_strength", "efficacy_prior", "axis_headroom",
            "cost_cycles", "risk", "priority"} <= set(estimate)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_is_written_before_anything_executes(preview_graph,
                                                         lighting_findings, tmp_path):
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings))
    path = write_report(plan, str(tmp_path / "planner_report.json"))

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    assert payload["planner_version"]
    assert payload["actions"]
    assert "graph" in payload and "diagnostics" in payload
    # It is a proposal, so nothing in it claims an outcome.
    assert "actual_gain" not in json.dumps(payload)


def test_the_report_records_what_was_dropped(preview_graph, lighting_findings,
                                              tmp_path):
    config = PlannerConfig(allowed_types=(ActionType.MATERIAL_ADJUSTMENT,))
    plan = Planner(preview_graph, config).plan(make_evaluation(lighting_findings))
    path = write_report(plan, str(tmp_path / "planner_report.json"))

    payload = json.load(open(path, encoding="utf-8"))
    assert payload["excluded"]
    assert payload["excluded"][0]["excluded_reason"]


def test_the_report_explains_the_dependency_graph(preview_graph):
    findings = [
        make_finding(axis="layout", code="systematic_offset",
                     subsystem=Subsystem.CAMERA_FIT,
                     summary="Every object in this view is offset by about 45 cm",
                     evidence={"offset_m": [0.3, 0.3], "coherence": 0.9}),
        make_finding(axis="layout", code="displacement",
                     subsystem=Subsystem.SCENE_GRAPH_TRANSFORM,
                     summary="sofa sits 62 cm from where the reference places it",
                     objects=["sofa_1"],
                     evidence={"implied": [2.6, 1.5], "actual": [2.0, 1.0]}),
    ]
    plan = Planner(preview_graph).plan(make_evaluation(findings))
    assert any("before" in line for line in plan.diagnostics["dependencies"])


# ---------------------------------------------------------------------------
# The whole pass
# ---------------------------------------------------------------------------


def test_refine_plans_then_optimises(preview_graph, lighting_findings, tmp_path):
    from optimizer.optimizer import ExecutionResult
    from optimizer.pipeline import refine

    def executor(graph, rebuild=True):
        room = graph.room_by_id("room_a")
        gain = 0.08 if abs(room.lighting.ambient - 0.5) > 0.01 else 0.0
        return ExecutionResult(evaluation=make_evaluation([], score=0.62 + gain),
                               ok=True)

    result = refine(preview_graph, make_evaluation(lighting_findings, score=0.62),
                    executor, str(tmp_path))

    assert result.plan.ordered
    assert result.history.accepted
    assert result.metrics.total_gain == pytest.approx(0.08)
    assert {os.path.basename(p) for p in result.documents} == {
        "planner_report.json", "optimization_history.json", "metrics.json"}


def test_refine_writes_a_plan_even_when_it_runs_nothing(preview_graph, tmp_path):
    """The proposal is worth keeping whether or not anything followed it."""
    from optimizer.pipeline import refine

    def executor(graph, rebuild=True):
        raise AssertionError("should not be called")

    result = refine(preview_graph, make_evaluation([]), executor, str(tmp_path))

    assert result.history is None
    assert os.path.exists(os.path.join(str(tmp_path), "planner_report.json"))
    assert any("no actions" in note for note in result.notes)


def test_the_module_level_entry_point_works(preview_graph, lighting_findings):
    plan = plan_fn(make_evaluation(lighting_findings), preview_graph)
    assert plan.ordered
