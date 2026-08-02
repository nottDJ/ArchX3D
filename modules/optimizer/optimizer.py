"""
ArchX3D — The optimisation loop
===============================
Executes a plan, one action at a time, keeping only what measurably helped.

    ┌─► next ready action
    │         ↓
    │   validate (pre)          ── forbidden? record and skip, no render spent
    │         ↓
    │   snapshot + apply
    │         ↓
    │   validate (post)         ── broke an invariant? roll back
    │         ↓
    │   rebuild + preview render
    │         ↓
    │   evaluate
    │         ↓
    │   gain > epsilon?  ── no ─► roll back, record why
    │         ↓ yes
    └── accept, new baseline    ── then check the stopping conditions

Measure, do not predict
-----------------------
The planner's expected gain orders the queue and nothing else. Every action is
judged on what it actually did to the score, measured by the same evaluation
engine on the same deterministic renders. An action that helps is kept whether
or not it was expected to; one that does not is taken back however promising it
looked.

That is what makes the loop safe to run unattended: the worst case for a bad
estimate is a wasted iteration, not a degraded reconstruction.

What it never does
------------------
No model of any kind is called. No DXF geometry, wall, opening or locked object
is touched — :mod:`optimizer.constraints` checks before and after every action,
and a violation rolls the change back before it can reach a render. And the
loop mutates one graph in memory; writing it out is the caller's decision,
taken after reading what happened.

Execution is injected
---------------------
Rebuilding a ``.blend``, rendering previews and evaluating them costs tens of
seconds. The loop does not know how any of it happens: it calls an *executor*,
which production wires to the generator, the preview pipeline and the
evaluation engine, and which tests replace with a function. Every behaviour
here — ordering, validation, acceptance, rollback, stopping — is therefore
testable in milliseconds, which is the only reason it is tested at all.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from planner import dependencies
from planner.action_graph import Action, ActionPlan

from . import constraints, metrics as metrics_mod, mutations, rollback, stopping
from .history import (
    ACCEPTED, BLOCKED, FAILED, REJECTED, SKIPPED, Attempt, History,
)
from .stopping import RunState, StoppingPolicy

OPTIMIZER_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """What one rebuild-render-evaluate cycle produced."""

    evaluation: Any = None
    ok: bool = True
    error: str = ""
    render_ms: int = 0
    evaluate_ms: int = 0

    @property
    def score(self) -> float:
        return float(getattr(self.evaluation, "score", 0.0) or 0.0)


#: Anything that can turn a scene graph into an evaluation of it. The seam
#: between the loop's logic and the forty seconds of Blender behind it.
Executor = Callable[[Any, bool], ExecutionResult]


@dataclass
class OptimizerConfig:
    """Policy and paths for one run."""

    stopping: StoppingPolicy = field(default_factory=StoppingPolicy)
    output_dir: str = ""
    #: Write the graph back when the run improves it. Off by default: an
    #: optimiser that rewrites the project's scene graph as a side effect of
    #: being run is one nobody can safely experiment with.
    write_graph: bool = False
    graph_path: str = ""
    verbose: bool = True

    #: Re-plan from a fresh evaluation once the current plan is exhausted.
    #: Each replan costs nothing extra — the evaluation it needs was already
    #: computed to judge the last action.
    replan: bool = True
    max_plans: int = 3


# ---------------------------------------------------------------------------
# The optimiser
# ---------------------------------------------------------------------------


class Optimizer:
    """Runs a plan against a scene graph, keeping what helps."""

    def __init__(self, graph, executor: Executor,
                 config: Optional[OptimizerConfig] = None,
                 planner_factory: Optional[Callable[[Any], ActionPlan]] = None) -> None:
        self.graph = graph
        self.executor = executor
        self.config = config or OptimizerConfig()
        #: Given an evaluation, produce a plan. Supplied by the caller so the
        #: loop never imports the planner's construction logic — it consumes
        #: plans, and where they come from is not its business.
        self.planner_factory = planner_factory

        self.history = History()
        self.ledger = rollback.RollbackLedger()
        self.state = RunState()
        self._timings = {"render_ms": 0, "evaluate_ms": 0}

    # -- the loop -----------------------------------------------------------

    def run(self, plan: ActionPlan, baseline: Any = None) -> History:
        """Execute a plan. Returns the history of everything attempted."""
        started = time.perf_counter()
        self.history.started_at = _now()
        self.history.plan_summary = {
            "actions": len(plan.ordered),
            "expected_total_gain": plan.expected_total_gain,
            "excluded": len(plan.excluded),
        }

        if baseline is None:
            baseline = self._evaluate(rebuild=True)
            if baseline is None:
                self.history.stop_reason = FAILED
                self.history.stop_detail = "the baseline evaluation failed"
                return self.history

        self.baseline_evaluation = baseline
        self.final_evaluation = baseline
        self.state.baseline_score = self.state.score = float(baseline.score)
        self.history.baseline_score = self.history.final_score = float(baseline.score)
        self.state.trajectory.append(round(float(baseline.score), 6))

        plans_run = 0
        decision = stopping.StopDecision(False)

        while plans_run < max(1, self.config.max_plans):
            decision = self._run_plan(plan)
            plans_run += 1
            if decision.stop and decision.reason != stopping.PLAN_EXHAUSTED:
                break
            if not (self.config.replan and self.planner_factory is not None):
                break
            plan = self.planner_factory(self.final_evaluation)
            if not plan.ordered:
                decision = stopping.StopDecision(
                    True, stopping.PLAN_EXHAUSTED,
                    "a fresh plan proposed no further actions",
                )
                break
            self._log(f"replanning: {plan.summary()}")

        self.history.stop_reason = decision.reason or stopping.PLAN_EXHAUSTED
        self.history.stop_detail = decision.detail
        self.history.final_score = self.state.score
        self.history.duration_ms = int((time.perf_counter() - started) * 1000)
        self._log(stopping.describe(decision, self.state))

        if self.config.write_graph and self.state.total_gain > 0:
            self._write_graph()
        return self.history

    def _run_plan(self, plan: ActionPlan) -> stopping.StopDecision:
        """Walk one plan's actions in dependency order."""
        completed: List[str] = []
        attempted = set(self.history.attempted_ids())

        while True:
            ready = dependencies.ready(plan.graph, completed, attempted)
            self.state.actions_remaining = len(ready)

            decision = stopping.should_stop(self.state, self.config.stopping)
            if decision.stop:
                return decision
            if not ready:
                return stopping.StopDecision(
                    True, stopping.PLAN_EXHAUSTED,
                    "every action in the plan has been attempted",
                )

            action = ready[0]
            attempted.add(action.id)
            self._attempt(action)
            # A predecessor counts as settled whether it was kept or taken
            # back: the question its dependents ask is "has this been decided",
            # and a rejected camera correction decides the camera.
            completed.append(action.id)

    # -- one action ---------------------------------------------------------

    def _attempt(self, action: Action) -> Attempt:
        """Validate, apply, measure, and keep or undo."""
        started = time.perf_counter()
        attempt = Attempt(
            iteration=self.state.iterations + 1,
            action_id=action.id,
            action_type=action.type,
            target=action.target,
            outcome=REJECTED,
            parameters=dict(action.parameters),
            expected_gain=action.expected_gain,
            score_before=self.state.score,
            score_after=self.state.score,
            trigger_findings=list(action.trigger_findings),
            trigger_summaries=list(action.trigger_summaries),
            rooms=list(action.rooms),
            objects=list(action.objects),
            materials=list(action.materials),
        )

        # -- forbidden before anything is spent ----------------------------
        pre = constraints.check_action(action, self.graph)
        if not pre.ok:
            attempt.outcome = BLOCKED
            attempt.rollback_reason = f"constraint: {pre.reason()}"
            attempt.violations = [v.to_dict() for v in pre.violations]
            attempt.duration_ms = _ms(started)
            self._record(attempt, accepted=False)
            self._log(f"[blocked] {action.id}: {pre.reason()}")
            return attempt

        snapshot = rollback.take(self.graph, action.id, self.state.score)

        # -- apply ----------------------------------------------------------
        mutation = mutations.apply(action, self.graph)
        attempt.changes = mutation.to_dict()["changes"]
        if not mutation.applied:
            attempt.outcome = SKIPPED
            attempt.rollback_reason = f"no change: {mutation.reason}"
            attempt.duration_ms = _ms(started)
            self._record(attempt, accepted=False)
            self._log(f"[skipped] {action.id}: {mutation.reason}")
            return attempt

        # -- forbidden after the fact --------------------------------------
        post = constraints.check_graph(self.graph, snapshot.immutables,
                                       scope=action.rooms or None)
        if not post.ok:
            rollback.restore(self.graph, snapshot)
            self.ledger.record(action.id, f"constraint: {post.reason()}", True)
            attempt.outcome = BLOCKED
            attempt.rollback_reason = f"constraint: {post.reason()}"
            attempt.violations = [v.to_dict() for v in post.violations]
            attempt.duration_ms = _ms(started)
            self._record(attempt, accepted=False)
            self._log(f"[blocked] {action.id}: {post.reason()}")
            return attempt

        # -- measure --------------------------------------------------------
        evaluation = self._evaluate(rebuild=action.requires_rebuild)
        if evaluation is None:
            rollback.restore(self.graph, snapshot)
            self.ledger.record(action.id, "execution failed", True)
            attempt.outcome = FAILED
            attempt.rollback_reason = "the rebuild, render or evaluation failed"
            attempt.duration_ms = _ms(started)
            self._record(attempt, accepted=False)
            self._log(f"[failed] {action.id}: execution error")
            return attempt

        gain = float(evaluation.score) - self.state.score
        attempt.actual_gain = gain
        attempt.score_after = float(evaluation.score)
        attempt.axis_deltas = _axis_deltas(self.final_evaluation, evaluation)

        # -- comparable? ----------------------------------------------------
        # A score is a weighted mean over the axes that could be measured, so
        # two scores drawn from different axis sets are not comparable. An
        # action that breaks the evaluation — a camera moved into a wall, a
        # render that failed — makes the remaining axes look like the whole
        # picture, and the mean of the survivors can be *higher*. Accepting
        # that would be improving the score by destroying the measurement,
        # which is the one failure mode a self-verifying loop cannot afford.
        lost = _lost_axes(self.final_evaluation, evaluation)
        if lost:
            rollback.restore(self.graph, snapshot)
            self.ledger.record(action.id, f"measurement lost: {', '.join(lost)}", True)
            attempt.outcome = REJECTED
            attempt.rollback_reason = (
                f"not comparable: the evaluation could no longer measure "
                f"{', '.join(lost)}, so the apparent "
                f"{gain:+.4f} is a change in what was measured rather than in "
                f"the reconstruction"
            )
            attempt.score_after = self.state.score
            attempt.actual_gain = 0.0
            attempt.duration_ms = _ms(started)
            self._record(attempt, accepted=False)
            self._log(f"[rejected] {action.id}: measurement lost "
                      f"({', '.join(lost)})")
            return attempt

        # -- keep or undo ---------------------------------------------------
        if stopping.accepts(gain, self.config.stopping):
            attempt.outcome = ACCEPTED
            attempt.duration_ms = _ms(started)
            self.final_evaluation = evaluation
            self._record(attempt, accepted=True, score=float(evaluation.score),
                         gain=gain)
            self._log(f"[accepted] {action.id}: {gain:+.4f} "
                      f"(expected {action.expected_gain:+.4f}) — "
                      f"{mutation.summary()}")
            return attempt

        rollback.restore(self.graph, snapshot)
        self.ledger.record(action.id, f"no improvement ({gain:+.5f})", True,
                           {"expected_gain": round(action.expected_gain, 5)})
        attempt.outcome = REJECTED
        attempt.rollback_reason = (
            f"no improvement: the score moved {gain:+.5f}, which is not above "
            f"the {self.config.stopping.epsilon} epsilon"
        )
        attempt.score_after = self.state.score
        attempt.duration_ms = _ms(started)
        self._record(attempt, accepted=False)
        self._log(f"[rejected] {action.id}: {gain:+.4f}")
        return attempt

    # -- plumbing -----------------------------------------------------------

    def _evaluate(self, rebuild: bool) -> Any:
        """Run one cycle through the injected executor."""
        try:
            result = self.executor(self.graph, rebuild)
        except Exception as exc:  # noqa: BLE001 - an execution failure is data
            self._log(f"execution raised {type(exc).__name__}: {exc}")
            return None
        if result is None or not getattr(result, "ok", False):
            if result is not None and result.error:
                self._log(f"execution failed: {result.error}")
            return None
        self._timings["render_ms"] += getattr(result, "render_ms", 0)
        self._timings["evaluate_ms"] += getattr(result, "evaluate_ms", 0)
        return result.evaluation

    def _record(self, attempt: Attempt, accepted: bool,
                score: Optional[float] = None, gain: float = 0.0) -> None:
        self.history.add(attempt)
        self.state.record(score if score is not None else self.state.score,
                          accepted, gain)
        self.history.final_score = self.state.score

    def _write_graph(self) -> None:
        path = self.config.graph_path
        if not path:
            self._log("write_graph is on but no graph_path was configured")
            return
        try:
            self.graph.save(path)
            self._log(f"scene graph written to {path}")
        except OSError as exc:
            self._log(f"could not write the scene graph: {exc}")

    def _log(self, message: str) -> None:
        if self.config.verbose:
            print(f"[OPTIMIZE] {message}")

    # -- outputs ------------------------------------------------------------

    def metrics(self) -> metrics_mod.Metrics:
        """Assemble ``metrics.json`` from the run."""
        return metrics_mod.build(
            self.history,
            getattr(self, "baseline_evaluation", None),
            getattr(self, "final_evaluation", None),
            self.state,
            self.history.stop_reason,
            self._timings,
        )

    def write_documents(self, output_dir: Optional[str] = None) -> List[str]:
        """Write ``optimization_history.json`` and ``metrics.json``."""
        directory = output_dir or self.config.output_dir
        if not directory:
            return []
        os.makedirs(directory, exist_ok=True)
        written = [
            self.history.save(os.path.join(directory, "optimization_history.json")),
            self.metrics().save(os.path.join(directory, "metrics.json")),
        ]
        return written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _axis_deltas(before, after) -> Dict[str, float]:
    """Per-axis movement between two evaluations.

    What actually changed, rather than what the action aimed at. An action that
    improved the lighting axis and quietly cost the material axis is a trade,
    and the history should show both halves of it.
    """
    def scores(evaluation) -> Dict[str, float]:
        building = getattr(evaluation, "building", None)
        return {
            axis: float(score.score)
            for axis, score in (getattr(building, "axes", {}) or {}).items()
            if getattr(score, "measured", False)
        }

    start, end = scores(before), scores(after)
    return {axis: round(end[axis] - value, 5)
            for axis, value in start.items() if axis in end}


def _lost_axes(before, after) -> List[str]:
    """Axes the previous evaluation measured and this one could not.

    Gaining an axis is fine — more of the picture is visible than before, and
    the score is simply better informed. Losing one is not, because the score
    then averages a different and smaller set.
    """
    def measured(evaluation) -> set:
        building = getattr(evaluation, "building", None)
        return {
            axis for axis, score in (getattr(building, "axes", {}) or {}).items()
            if getattr(score, "measured", False)
        }

    if before is None or after is None:
        return []
    return sorted(measured(before) - measured(after))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
