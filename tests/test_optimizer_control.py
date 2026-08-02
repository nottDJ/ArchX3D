"""
Tests for rollback, stopping, history and metrics.

These four decide what the loop does *around* an action: whether it can be
taken back, when to stop trying, what gets written down, and what the run says
about itself afterwards. None of them needs a render, and the stopping rules in
particular are worth isolating — "it did nine iterations" is not an answer to
"why did it stop at nine", and only a rule that can be tested alone can be
relied on to give one.
"""

from __future__ import annotations

import json

import pytest

from optimizer import rollback, stopping
from optimizer.history import ACCEPTED, BLOCKED, REJECTED, Attempt, History
from optimizer.metrics import Metrics
from optimizer.stopping import RunState, StoppingPolicy


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_a_snapshot_restores_the_graph_exactly(preview_graph):
    before = json.dumps(preview_graph.to_dict(), sort_keys=True)
    snapshot = rollback.take(preview_graph)

    preview_graph.object_by_id("sofa_1").position.x += 5.0
    preview_graph.rooms[0].lighting.ambient = 0.99
    preview_graph.objects.pop()

    assert rollback.restore(preview_graph, snapshot)
    assert json.dumps(preview_graph.to_dict(), sort_keys=True) == before


def test_restoring_happens_in_place(preview_graph):
    """The optimiser, the planner and the caller all hold this reference."""
    snapshot = rollback.take(preview_graph)
    original = preview_graph
    preview_graph.objects.clear()

    rollback.restore(preview_graph, snapshot)
    assert preview_graph is original
    assert preview_graph.objects


def test_a_restore_is_the_identity_when_nothing_changed(preview_graph):
    snapshot = rollback.take(preview_graph)
    rollback.restore(preview_graph, snapshot)
    assert not rollback.changed(preview_graph, snapshot)


def test_changed_detects_a_mutation(preview_graph):
    snapshot = rollback.take(preview_graph)
    preview_graph.object_by_id("sofa_1").rotation_z = 42.0
    assert rollback.changed(preview_graph, snapshot)


def test_a_snapshot_carries_the_immutable_digests(preview_graph):
    snapshot = rollback.take(preview_graph)
    assert "walls" in snapshot.immutables
    assert "locked_objects" in snapshot.immutables


def test_restoring_an_empty_snapshot_does_nothing(preview_graph):
    assert not rollback.restore(preview_graph, rollback.Snapshot())


def test_the_ledger_tallies_rollbacks_by_reason():
    ledger = rollback.RollbackLedger()
    ledger.record("a", "no improvement (-0.01)", True)
    ledger.record("b", "no improvement (-0.02)", True)
    ledger.record("c", "constraint: locked_object", True)

    assert ledger.count == 3
    assert ledger.reasons()["constraint"] == 1
    assert ledger.reasons()["no improvement (-0.01)"] == 1


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


def state(**kwargs):
    settings = dict(iterations=1, score=0.7, baseline_score=0.6,
                    actions_remaining=5)
    settings.update(kwargs)
    return RunState(**settings)


def test_a_run_stops_when_it_reaches_the_target():
    decision = stopping.should_stop(state(score=0.9),
                                    StoppingPolicy(target_score=0.85))
    assert decision.stop
    assert decision.reason == stopping.TARGET_REACHED


def test_a_run_stops_at_its_iteration_budget():
    decision = stopping.should_stop(state(iterations=12),
                                    StoppingPolicy(max_iterations=12))
    assert decision.stop
    assert decision.reason == stopping.MAX_ITERATIONS


def test_hitting_the_budget_while_improving_says_so():
    """The difference between 'finished' and 'ran out of time'."""
    decision = stopping.should_stop(
        state(iterations=6, accepted_gains=[0.05]),
        StoppingPolicy(max_iterations=6),
    )
    assert "larger budget may help" in decision.detail


def test_a_run_stops_after_repeated_rejections():
    decision = stopping.should_stop(state(consecutive_rejections=3),
                                    StoppingPolicy(max_consecutive_rejections=3))
    assert decision.stop
    assert decision.reason == stopping.NO_GAIN


def test_one_rejection_does_not_end_a_run():
    """The queue is ordered by an estimate; one bad guess says little."""
    decision = stopping.should_stop(state(consecutive_rejections=1),
                                    StoppingPolicy(max_consecutive_rejections=3))
    assert not decision.stop


def test_a_run_stops_when_improvements_shrink_to_nothing():
    decision = stopping.should_stop(
        state(accepted_gains=[0.0005, 0.0003, 0.0002]),
        StoppingPolicy(epsilon=0.002, plateau_window=3),
    )
    assert decision.stop
    assert decision.reason == stopping.BELOW_EPSILON


def test_real_progress_does_not_look_like_a_plateau():
    decision = stopping.should_stop(
        state(accepted_gains=[0.01, 0.02, 0.03]),
        StoppingPolicy(epsilon=0.002, plateau_window=3),
    )
    assert not decision.stop


def test_a_plateau_is_judged_on_accepted_actions_not_iterations():
    """A run alternating useful changes with rejections is still working."""
    decision = stopping.should_stop(
        state(iterations=9, rejected=6, accepted_gains=[0.02, 0.03]),
        StoppingPolicy(epsilon=0.002, plateau_window=3, max_iterations=20),
    )
    assert not decision.stop


def test_a_run_stops_when_the_plan_runs_out():
    decision = stopping.should_stop(state(actions_remaining=0), StoppingPolicy())
    assert decision.stop
    assert decision.reason == stopping.PLAN_EXHAUSTED


def test_the_target_is_checked_before_the_budget():
    """Reaching the goal on the last permitted iteration is a success."""
    decision = stopping.should_stop(
        state(iterations=5, score=0.99),
        StoppingPolicy(target_score=0.9, max_iterations=5),
    )
    assert decision.reason == stopping.TARGET_REACHED


def test_a_gain_at_exactly_epsilon_is_not_an_improvement():
    """The graph is simpler without a change that achieved nothing."""
    policy = StoppingPolicy(epsilon=0.002)
    assert not stopping.accepts(0.002, policy)
    assert stopping.accepts(0.0021, policy)
    assert not stopping.accepts(0.0, policy)


def test_run_state_tracks_its_own_trajectory():
    run = RunState(baseline_score=0.5, score=0.5)
    run.record(0.6, accepted=True, gain=0.1)
    run.record(0.6, accepted=False)
    run.record(0.65, accepted=True, gain=0.05)

    assert run.iterations == 3
    assert run.accepted == 2 and run.rejected == 1
    assert run.consecutive_rejections == 0     # reset by the acceptance
    assert run.trajectory == [0.6, 0.6, 0.65]
    assert run.total_gain == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def attempt(action_id="a", outcome=ACCEPTED, expected=0.1, actual=0.05,
            action_type="lighting_adjustment", **kwargs):
    return Attempt(iteration=1, action_id=action_id, action_type=action_type,
                   target="room:room_a", outcome=outcome,
                   expected_gain=expected, actual_gain=actual, **kwargs)


def test_a_history_separates_what_was_kept_from_what_was_not():
    history = History(baseline_score=0.6, final_score=0.7)
    history.add(attempt("a", ACCEPTED))
    history.add(attempt("b", REJECTED, actual=-0.01))
    history.add(attempt("c", BLOCKED, actual=0.0))

    assert len(history.accepted) == 1
    assert len(history.rejected) == 2
    assert history.total_gain == pytest.approx(0.1)


def test_a_rejected_attempt_records_why():
    """The rejections are the half of the file worth keeping."""
    entry = attempt("b", REJECTED, actual=-0.01)
    entry.rollback_reason = "no improvement: the score moved -0.01000"
    assert "no improvement" in entry.to_dict()["rollback_reason"]


def test_an_attempt_records_expected_against_actual():
    entry = attempt(expected=0.20, actual=0.05)
    assert entry.estimate_error == pytest.approx(0.15)
    assert entry.to_dict()["estimate_error"] == pytest.approx(0.15)


def test_an_attempt_is_replayable_from_its_record():
    entry = attempt()
    entry.parameters = {"ambient": 0.8}
    restored = Attempt.from_dict(entry.to_dict())
    assert restored.parameters == {"ambient": 0.8}
    assert restored.action_type == entry.action_type
    assert restored.target == entry.target


def test_history_tallies_what_each_action_type_delivered():
    history = History()
    history.add(attempt("a", ACCEPTED, actual=0.05, action_type="lighting_adjustment"))
    history.add(attempt("b", REJECTED, actual=-0.01, action_type="lighting_adjustment"))
    history.add(attempt("c", ACCEPTED, actual=0.02, action_type="material_adjustment"))

    tally = history.by_type()
    assert tally["lighting_adjustment"]["attempted"] == 2
    assert tally["lighting_adjustment"]["acceptance_rate"] == 0.5
    assert tally["material_adjustment"]["total_gain"] == pytest.approx(0.02)


def test_history_groups_rejection_reasons():
    history = History()
    for index in range(3):
        entry = attempt(f"a{index}", REJECTED)
        entry.rollback_reason = "no improvement: the score moved -0.001"
        history.add(entry)

    assert history.rejection_reasons()["no improvement"] == 3


def test_a_history_round_trips_through_json(tmp_path):
    history = History(baseline_score=0.6, final_score=0.72,
                      stop_reason=stopping.NO_GAIN)
    history.add(attempt())
    path = history.save(str(tmp_path / "optimization_history.json"))

    reloaded = History.load(path)
    assert reloaded.final_score == pytest.approx(0.72)
    assert reloaded.stop_reason == stopping.NO_GAIN
    assert len(reloaded.attempts) == 1


def test_a_missing_history_loads_as_empty(tmp_path):
    assert History.load(str(tmp_path / "absent.json")).attempts == []


def test_a_corrupt_history_loads_as_empty(tmp_path):
    path = tmp_path / "optimization_history.json"
    path.write_text("}{", encoding="utf-8")
    assert History.load(str(path)).attempts == []


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_report_the_axes_that_moved():
    metrics = Metrics(
        baseline_score=0.6, final_score=0.7,
        axis_before={"lighting": 0.4, "colour": 0.8},
        axis_after={"lighting": 0.7, "colour": 0.75},
    )
    deltas = metrics.axis_deltas()
    assert deltas["lighting"] == pytest.approx(0.3)
    assert deltas["colour"] == pytest.approx(-0.05)


def test_metrics_name_axes_that_ended_worse():
    """A positive total can hide a trade nobody asked for."""
    metrics = Metrics(
        baseline_score=0.6, final_score=0.7,
        axis_before={"lighting": 0.4, "material": 0.8},
        axis_after={"lighting": 0.8, "material": 0.6},
    )
    assert metrics.regressions() == {"material": pytest.approx(-0.2)}


def test_metrics_report_the_acceptance_rate():
    metrics = Metrics(accepted=1, rejected=3, iterations=4)
    assert metrics.acceptance_rate == 0.25


def test_metrics_serialise_everything_a_reader_needs(tmp_path):
    metrics = Metrics(baseline_score=0.6, final_score=0.7, iterations=3,
                      accepted=2, rejected=1, trajectory=[0.6, 0.65, 0.7],
                      stop_reason=stopping.PLAN_EXHAUSTED)
    payload = json.loads(open(metrics.save(str(tmp_path / "metrics.json")),
                              encoding="utf-8").read())

    assert payload["score"]["total_gain"] == pytest.approx(0.1)
    assert payload["score"]["trajectory"] == [0.6, 0.65, 0.7]
    assert payload["run"]["stop_reason"] == stopping.PLAN_EXHAUSTED
    assert "calibration" in payload and "attribution" in payload


def test_a_run_that_gained_nothing_says_so():
    from optimizer.metrics import _notes

    metrics = Metrics(baseline_score=0.6, final_score=0.6, iterations=4)
    notes = _notes(metrics, History())
    assert any("no better than it started" in note for note in notes)


def test_an_optimistic_planner_is_called_out():
    from optimizer.metrics import _notes

    history = History()
    history.add(attempt("a", ACCEPTED, expected=0.30, actual=0.02))
    history.add(attempt("b", ACCEPTED, expected=0.20, actual=0.01))

    metrics = Metrics(baseline_score=0.6, final_score=0.63, iterations=2,
                      accepted=2)
    metrics.calibration = {"overall": {"ratio": 0.06}}
    notes = _notes(metrics, history)
    assert any("optimistic" in note for note in notes)
