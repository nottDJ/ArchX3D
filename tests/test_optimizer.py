"""
Integration tests for the optimisation loop.

Blender is replaced by a fake executor whose scores are a *function of the
graph*, not a canned sequence. That distinction matters: a fake returning
scripted numbers would pass whether or not the loop actually applied and
reverted anything, whereas one that reads the graph can only be satisfied by a
loop that really mutated it — and really put it back.

What is pinned here is the contract the whole phase rests on: a change is kept
only if the score measurably rose, everything else is restored exactly, and the
run can say afterwards what it tried and why each thing survived or did not.
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import make_evaluation
from optimizer import Optimizer, OptimizerConfig, StoppingPolicy
from optimizer.optimizer import ExecutionResult
from planner import Planner
from planner.action_graph import Action, ActionPlan, ActionType
from planner.dependencies import build as build_graph


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class GraphScoringExecutor:
    """Scores whatever graph it is handed, so rollback cannot be faked.

    Each rule is a predicate over the graph and the gain it is worth. A loop
    that applied a change without reverting it would keep scoring the changed
    graph, and the assertions about restored state would fail — which is
    exactly the property under test.
    """

    def __init__(self, rules=(), baseline=0.60, fail_on=()):
        self.rules = list(rules)
        self.baseline = baseline
        self.fail_on = set(fail_on)
        self.calls = []
        self.rebuilds = []

    def __call__(self, graph, rebuild=True):
        self.calls.append(rebuild)
        self.rebuilds.append(rebuild)
        if len(self.calls) in self.fail_on:
            return ExecutionResult(ok=False, error="synthetic execution failure")
        score = self.baseline + sum(
            gain for predicate, gain in self.rules if predicate(graph)
        )
        return ExecutionResult(evaluation=make_evaluation([], score=score),
                               ok=True, render_ms=1200, evaluate_ms=40)


def lighting_changed(graph):
    room = graph.room_by_id("room_a")
    return bool(room and room.lighting and abs(room.lighting.ambient - 0.5) > 0.01)


def sofa_moved(graph):
    return abs(graph.object_by_id("sofa_1").position.x - 2.0) > 0.01


def one_action(action_type=ActionType.LIGHTING_ADJUSTMENT, **kwargs):
    settings = dict(
        id=f"{action_type}:room_a", type=action_type, target="room:room_a",
        parameters={"ambient": 0.8}, rooms=["room_a"], expected_gain=0.1,
        confidence=0.8, priority=0.5, axes=["lighting"],
        trigger_findings=["lighting|LightingEnvironment|exposure|room_a|"],
        trigger_summaries=["Render is darker than the reference"],
    )
    settings.update(kwargs)
    return Action(**settings)


def plan_of(*actions) -> ActionPlan:
    plan = ActionPlan(baseline_score=0.60)
    graph = build_graph(list(actions))
    plan.graph = graph
    plan.considered = list(actions)
    plan.ordered = graph.topological_order()
    return plan


def run(graph, plan, executor, **policy):
    config = OptimizerConfig(stopping=StoppingPolicy(**policy), verbose=False)
    optimizer = Optimizer(graph, executor, config)
    history = optimizer.run(plan, baseline=make_evaluation([], score=executor.baseline))
    return optimizer, history


# ---------------------------------------------------------------------------
# Accepting and rejecting
# ---------------------------------------------------------------------------


def test_an_action_that_helps_is_kept(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    optimizer, history = run(preview_graph, plan_of(one_action()), executor)

    assert len(history.accepted) == 1
    assert history.final_score == pytest.approx(0.68)
    assert preview_graph.room_by_id("room_a").lighting.ambient == 0.8


def test_an_action_that_does_not_help_is_taken_back(preview_graph):
    before = preview_graph.room_by_id("room_a").lighting.ambient
    executor = GraphScoringExecutor([(lighting_changed, 0.0)])
    optimizer, history = run(preview_graph, plan_of(one_action()), executor)

    assert history.accepted == []
    assert preview_graph.room_by_id("room_a").lighting.ambient == before
    assert "no improvement" in history.attempts[0].rollback_reason


def test_an_action_that_makes_things_worse_is_taken_back(preview_graph):
    before = preview_graph.room_by_id("room_a").lighting.ambient
    executor = GraphScoringExecutor([(lighting_changed, -0.05)])
    optimizer, history = run(preview_graph, plan_of(one_action()), executor)

    assert history.final_score == pytest.approx(0.60)
    assert preview_graph.room_by_id("room_a").lighting.ambient == before


def test_a_gain_below_epsilon_is_not_an_improvement(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.0005)])
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor,
                              epsilon=0.002)
    assert history.accepted == []


def test_the_baseline_advances_after_an_acceptance(preview_graph):
    """The second action is judged against the first's result, not the start."""
    executor = GraphScoringExecutor([(lighting_changed, 0.08), (sofa_moved, 0.03)])
    move = one_action(ActionType.FURNITURE_TRANSLATION, id="move",
                      target="object:sofa_1", objects=["sofa_1"], rooms=["room_a"],
                      parameters={"dx": 0.5}, priority=0.1, axes=["layout"])
    _optimizer, history = run(preview_graph, plan_of(one_action(), move), executor)

    assert [a.actual_gain for a in history.attempts] == [
        pytest.approx(0.08), pytest.approx(0.03)
    ]
    assert history.final_score == pytest.approx(0.71)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_a_forbidden_action_is_blocked_before_any_render(preview_graph):
    """A rejection that costs no iteration is the cheapest kind there is."""
    preview_graph.object_by_id("sofa_1").locked = True
    executor = GraphScoringExecutor([(sofa_moved, 0.5)])
    move = one_action(ActionType.FURNITURE_TRANSLATION, id="move",
                      target="object:sofa_1", objects=["sofa_1"],
                      parameters={"dx": 0.5})

    _optimizer, history = run(preview_graph, plan_of(move), executor)

    assert history.attempts[0].outcome == "blocked"
    assert history.attempts[0].violations
    assert executor.calls == []          # nothing was rendered
    assert preview_graph.object_by_id("sofa_1").position.x == 2.0


def test_a_change_that_breaks_an_invariant_is_rolled_back(preview_graph):
    """Even a change that would have scored well: a violated constraint must
    never reach a render, because a render that looks better gets accepted."""
    executor = GraphScoringExecutor([(sofa_moved, 0.5)])
    move = one_action(ActionType.FURNITURE_TRANSLATION, id="move",
                      target="object:sofa_1", objects=["sofa_1"], rooms=["room_a"],
                      parameters={"dx": 99.0})

    _optimizer, history = run(preview_graph, plan_of(move), executor)

    assert history.attempts[0].outcome == "blocked"
    assert any(v["rule"] == "room_containment"
               for v in history.attempts[0].violations)
    assert preview_graph.object_by_id("sofa_1").position.x == 2.0
    assert executor.calls == []


def test_dxf_geometry_survives_a_whole_run(preview_graph):
    """The guarantee the phase is judged on."""
    walls_before = json.dumps([w.to_dict() for w in preview_graph.walls],
                              sort_keys=True)
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    run(preview_graph, plan_of(one_action()), executor)

    assert json.dumps([w.to_dict() for w in preview_graph.walls],
                      sort_keys=True) == walls_before


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_actions_run_in_dependency_order_not_rank_order(preview_graph):
    camera = one_action(ActionType.CAMERA_CORRECTION, id="cam",
                        target="viewpoint:img_a1", rooms=["room_a"],
                        parameters={"dx": 0.1}, priority=0.1, axes=["layout"])
    move = one_action(ActionType.FURNITURE_TRANSLATION, id="move",
                      target="object:sofa_1", objects=["sofa_1"], rooms=["room_a"],
                      parameters={"dx": 0.3}, priority=0.9, axes=["layout"])

    executor = GraphScoringExecutor([(lighting_changed, 0.0)])
    _optimizer, history = run(preview_graph, plan_of(camera, move), executor)
    assert [a.action_id for a in history.attempts] == ["cam", "move"]


def test_a_camera_action_needs_no_rebuild(preview_graph):
    """It is roughly three seconds against forty, which is why it is tracked."""
    camera = one_action(ActionType.CAMERA_CORRECTION, id="cam",
                        target="viewpoint:img_a1", parameters={"dx": 0.1})
    executor = GraphScoringExecutor()
    run(preview_graph, plan_of(camera), executor)

    assert executor.rebuilds == [False]


def test_a_lighting_action_does_need_a_rebuild(preview_graph):
    executor = GraphScoringExecutor()
    run(preview_graph, plan_of(one_action()), executor)
    assert executor.rebuilds == [True]


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


def test_a_run_stops_at_its_iteration_budget(preview_graph):
    actions = [one_action(ActionType.FURNITURE_TRANSLATION, id=f"move{i}",
                          target="object:sofa_1", objects=["sofa_1"],
                          rooms=["room_a"], parameters={"dx": 0.01 * (i + 1)},
                          priority=1.0 - i * 0.1)
               for i in range(6)]
    # Each targets the same object, so only the first survives exclusion;
    # give them distinct targets instead.
    for index, action in enumerate(actions):
        action.target = f"object:sofa_1#{index}"

    executor = GraphScoringExecutor([(sofa_moved, 0.01)])
    _optimizer, history = run(preview_graph, plan_of(*actions), executor,
                              max_iterations=2)

    assert len(history.attempts) == 2
    assert history.stop_reason == "max_iterations"


def test_a_run_stops_after_repeated_rejections(preview_graph):
    actions = [one_action(id=f"a{i}", target=f"room:room_a#{i}",
                          priority=1.0 - i * 0.1, parameters={"ambient": 0.6 + i * 0.05})
               for i in range(6)]
    executor = GraphScoringExecutor([(lighting_changed, 0.0)])
    _optimizer, history = run(preview_graph, plan_of(*actions), executor,
                              max_consecutive_rejections=3, max_iterations=20)

    assert history.stop_reason == "no_gain"
    assert len(history.attempts) == 3


def test_a_run_stops_once_it_reaches_the_target(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.35)])
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor,
                              target_score=0.9)
    assert history.stop_reason == "target_reached"


def test_a_run_stops_when_the_plan_runs_out(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor,
                              max_iterations=20)
    assert history.stop_reason == "plan_exhausted"


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_an_execution_failure_rolls_back_rather_than_crashing(preview_graph):
    before = preview_graph.room_by_id("room_a").lighting.ambient
    executor = GraphScoringExecutor([(lighting_changed, 0.08)], fail_on={1})
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor)

    assert history.attempts[0].outcome == "failed"
    assert preview_graph.room_by_id("room_a").lighting.ambient == before


def test_an_executor_that_raises_is_treated_as_a_failure(preview_graph):
    def explode(graph, rebuild=True):
        raise RuntimeError("blender is on fire")

    config = OptimizerConfig(stopping=StoppingPolicy(), verbose=False)
    optimizer = Optimizer(preview_graph, explode, config)
    history = optimizer.run(plan_of(one_action()),
                            baseline=make_evaluation([], score=0.6))

    assert history.attempts[0].outcome == "failed"
    assert preview_graph.room_by_id("room_a").lighting.ambient == 0.5


def test_an_action_that_changes_nothing_is_recorded_as_skipped(preview_graph):
    current = preview_graph.room_by_id("room_a").lighting.ambient
    executor = GraphScoringExecutor()
    _optimizer, history = run(preview_graph, plan_of(
        one_action(parameters={"ambient": current})), executor)

    assert history.attempts[0].outcome == "skipped"
    assert executor.calls == []


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------


def test_every_attempt_records_what_the_spec_requires(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor)
    entry = history.attempts[0].to_dict()

    assert entry["trigger_findings"]          # what prompted it
    assert entry["expected_gain"] > 0          # what was predicted
    assert entry["actual_gain"] > 0            # what happened
    assert entry["affected"]["rooms"] == ["room_a"]
    assert entry["changes"]                    # field-level audit trail


def test_a_rejected_attempt_records_its_rollback_reason(preview_graph):
    executor = GraphScoringExecutor([(lighting_changed, 0.0)])
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor)
    entry = history.attempts[0].to_dict()

    assert entry["outcome"] == "rejected"
    assert "no improvement" in entry["rollback_reason"]
    assert entry["expected_gain"] > 0          # and what was expected of it


def test_the_documents_are_written(preview_graph, tmp_path):
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    config = OptimizerConfig(stopping=StoppingPolicy(), verbose=False,
                             output_dir=str(tmp_path))
    optimizer = Optimizer(preview_graph, executor, config)
    optimizer.run(plan_of(one_action()), baseline=make_evaluation([], score=0.6))

    written = optimizer.write_documents()
    assert {os.path.basename(p) for p in written} == {
        "optimization_history.json", "metrics.json"}
    for path in written:
        with open(path, encoding="utf-8") as handle:
            assert json.load(handle)


def test_metrics_record_the_calibration_error(preview_graph):
    """The planner promised 0.1 and delivered 0.02; the run should say so."""
    executor = GraphScoringExecutor([(lighting_changed, 0.02)])
    optimizer, _history = run(preview_graph, plan_of(one_action()), executor)
    calibration = optimizer.metrics().calibration

    assert calibration["overall"]["expected"] == pytest.approx(0.1)
    assert calibration["overall"]["actual"] == pytest.approx(0.02)
    assert calibration["overall"]["error"] > 0


def test_the_scene_graph_is_only_written_when_asked(preview_graph, tmp_path):
    path = tmp_path / "scene_graph.json"
    executor = GraphScoringExecutor([(lighting_changed, 0.08)])
    config = OptimizerConfig(stopping=StoppingPolicy(), verbose=False,
                             write_graph=False, graph_path=str(path))
    Optimizer(preview_graph, executor, config).run(
        plan_of(one_action()), baseline=make_evaluation([], score=0.6))
    assert not path.exists()

    config.write_graph = True
    Optimizer(preview_graph, executor, config).run(
        plan_of(one_action(parameters={"ambient": 0.9})),
        baseline=make_evaluation([], score=0.6))
    assert path.exists()


# ---------------------------------------------------------------------------
# Determinism and the planner boundary
# ---------------------------------------------------------------------------


def test_two_runs_over_the_same_inputs_agree(preview_graph):
    import copy

    def once():
        graph = copy.deepcopy(preview_graph)
        executor = GraphScoringExecutor([(lighting_changed, 0.08)])
        _optimizer, history = run(graph, plan_of(one_action()), executor)
        payload = history.to_dict()
        payload.pop("started_at")
        payload.pop("duration_ms")
        for attempt in payload["attempts"]:
            attempt.pop("timestamp")
            attempt.pop("duration_ms")
        return payload

    assert once() == once()


def test_the_planner_produces_the_same_plan_twice(preview_graph, lighting_findings):
    evaluation = make_evaluation(lighting_findings)
    first = Planner(preview_graph).plan(evaluation).to_dict()
    second = Planner(preview_graph).plan(evaluation).to_dict()

    for payload in (first, second):
        payload["diagnostics"].pop("duration_ms")
    assert first == second


def test_the_optimiser_never_sees_a_finding(preview_graph, lighting_findings):
    """The boundary the phase is built around.

    An action carries finding *keys* for the report; nothing in the optimiser
    reads a Finding object, and this asserts the plan it consumes contains no
    route to one.
    """
    plan = Planner(preview_graph).plan(make_evaluation(lighting_findings))
    payload = json.dumps(plan.to_dict())

    assert "trigger_findings" in payload
    for action in plan.ordered:
        assert all(isinstance(key, str) for key in action.trigger_findings)
        assert all(isinstance(text, str) for text in action.trigger_summaries)


def test_an_empty_plan_runs_nothing(preview_graph):
    executor = GraphScoringExecutor()
    _optimizer, history = run(preview_graph, ActionPlan(), executor)

    assert history.attempts == []
    assert history.stop_reason == "plan_exhausted"
    assert executor.calls == []


# ---------------------------------------------------------------------------
# Improving the score by breaking the measurement
# ---------------------------------------------------------------------------


class BlindingExecutor:
    """Returns a *higher* score measured over *fewer* axes.

    Exactly what a camera moved into a wall produces: the previews fail, the
    pixel axes go unmeasured, and the mean of whatever survives can be higher
    than the honest score it replaced.
    """

    baseline = 0.60

    def __init__(self):
        self.calls = []

    def __call__(self, graph, rebuild=True):
        self.calls.append(rebuild)
        return ExecutionResult(
            evaluation=make_evaluation([], score=1.0,
                                       unmeasured=("colour", "material",
                                                   "lighting", "layout")),
            ok=True)


def test_a_higher_score_over_fewer_axes_is_rejected(preview_graph):
    """The one failure mode a self-verifying loop cannot afford.

    Two scores drawn from different axis sets are not comparable, so a run
    that lost four of five axes and reported 1.00 has not improved anything —
    it has stopped looking.
    """
    executor = BlindingExecutor()
    before = preview_graph.room_by_id("room_a").lighting.ambient
    _optimizer, history = run(preview_graph, plan_of(one_action()), executor)

    assert history.accepted == []
    assert history.final_score == pytest.approx(0.60)
    assert "not comparable" in history.attempts[0].rollback_reason
    assert "colour" in history.attempts[0].rollback_reason
    assert preview_graph.room_by_id("room_a").lighting.ambient == before


def test_gaining_an_axis_is_not_penalised(preview_graph):
    """More of the picture than before is a better-informed score, not a
    suspect one."""
    class RevealingExecutor:
        baseline = 0.60

        def __call__(self, graph, rebuild=True):
            return ExecutionResult(
                evaluation=make_evaluation([], score=0.68), ok=True)

    config = OptimizerConfig(stopping=StoppingPolicy(), verbose=False)
    optimizer = Optimizer(preview_graph, RevealingExecutor(), config)
    history = optimizer.run(
        plan_of(one_action()),
        # The baseline could not measure the material axis; the action's
        # evaluation can. Nothing was lost, so nothing is suspect.
        baseline=make_evaluation([], score=0.60, unmeasured=("material",)),
    )
    assert len(history.accepted) == 1
