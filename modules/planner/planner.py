"""
ArchX3D — The planner
=====================
Turns one evaluation into one executable plan.

    EvaluationResult ──► FindingSet ──► groups ──► actions ──► graph ──► ActionPlan
                         (findings)   (grouping) (synthesis) (dependencies)

Six responsibilities, in order:

1. **Adapt** the evaluation into a :class:`planner.findings.FindingSet`. The
   only place raw findings are read.
2. **Group** findings that share a root cause, so three lighting complaints
   about one room become one lighting action rather than three.
3. **Synthesise** each group into a concrete action with absolute parameter
   values, read (never written) from the scene graph.
4. **Estimate** the gain each action is likely to deliver and what trying it
   will cost.
5. **Resolve** contradictions and ordering constraints into a DAG.
6. **Rank** and linearise into an execution order.

What the planner will not do
----------------------------
It does not mutate the scene graph — it reads it to compute absolute values,
and that is all. It does not call a vision model, or any model: every number
in a plan is derived arithmetically from the evaluation and the graph. And it
does not act; producing a plan changes nothing, which is what makes a plan
safe to inspect before running.

Determinism
-----------
Same evaluation, same graph, same plan — byte for byte, including ordering and
ids. Groups are keyed and sorted, ranking ties break on id, and the topological
walk is deterministic. A plan that shuffled between runs could not be
regression-tested, and a refinement loop nobody can regression-test is a
refinement loop nobody should run.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import dependencies, grouping, ranking
from .action_graph import Action, ActionPlan, ActionType
from .findings import FindingSet

PLANNER_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PlannerConfig:
    """Policy for one planning pass."""

    #: Hard ceiling on plan length. Not a quality judgement — the optimiser has
    #: its own stopping rules — but a plan of two hundred actions is a report
    #: nobody reads, and the tail of it is always the least promising.
    max_actions: int = 24

    #: Actions estimated below this are dropped as not worth an iteration.
    #: Deliberately small: the estimate is only an ordering heuristic, so the
    #: floor exists to remove noise, not to make decisions.
    min_expected_gain: float = 0.002

    #: Action types this run may propose. Narrowing it is how a caller says
    #: "lighting only" without editing the planner.
    allowed_types: Sequence[str] = ActionType.ALL

    #: Propose actions whose axes the evaluation could not measure. Off by
    #: default: the optimiser accepts or rejects on measured improvement, so an
    #: unverifiable action's verdict would be noise.
    include_unverifiable: bool = False

    def permits(self, action: Action) -> bool:
        return action.type in self.allowed_types


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class Planner:
    """Builds an :class:`ActionPlan` from an evaluation and a scene graph."""

    def __init__(self, graph, config: Optional[PlannerConfig] = None) -> None:
        #: Read-only. The planner computes absolute parameter values from the
        #: current state and never writes back.
        self.graph = graph
        self.config = config or PlannerConfig()

    # -- the pass -----------------------------------------------------------

    def plan(self, evaluation) -> ActionPlan:
        """Evaluation in, plan out."""
        started = time.perf_counter()
        finding_set = FindingSet.from_evaluation(evaluation)
        return self.plan_from(finding_set, started=started)

    def plan_from(self, finding_set: FindingSet,
                  started: Optional[float] = None) -> ActionPlan:
        """Plan from an already-adapted finding set.

        Split out so the planner can be tested and driven without constructing
        a full :class:`EvaluationResult`, and so a caller that already holds a
        finding set does not pay to rebuild it.
        """
        started = started if started is not None else time.perf_counter()
        plan = ActionPlan(baseline_score=finding_set.baseline_score)

        groups = grouping.group(finding_set)
        evidence: Dict[str, float] = {}
        candidates: List[Action] = []

        for group in groups:
            for action in grouping.synthesise(group, self.graph):
                evidence[action.id] = group.weight
                candidates.append(action)

        candidates.extend(grouping.style_actions(finding_set, self.graph))

        candidates = self._deduplicate(candidates, plan)
        ranking.rank(candidates, finding_set, evidence)

        kept = self._filter(candidates, finding_set, plan)
        graph = dependencies.build(kept)
        plan.graph = graph
        plan.considered = list(candidates)

        ordered = graph.topological_order()
        if len(ordered) > self.config.max_actions:
            for action in ordered[self.config.max_actions:]:
                action.excluded = True
                action.excluded_reason = (
                    f"beyond the {self.config.max_actions}-action plan limit"
                )
            ordered = ordered[:self.config.max_actions]
        plan.ordered = ordered

        plan.expected_total_gain = ranking.plan_gain(ordered, finding_set)
        plan.diagnostics = self._diagnostics(finding_set, groups, plan, started)
        plan.notes = self._notes(finding_set, plan)
        return plan

    # -- filtering ----------------------------------------------------------

    def _deduplicate(self, candidates: List[Action], plan: ActionPlan) -> List[Action]:
        """Collapse actions that arrived twice with the same id.

        Two findings in different rooms can synthesise the same building-wide
        action; the grouping key would separate them but the action they
        produce is one action. The first wins and the second's triggers are
        folded into it, so the report still shows every finding that argued
        for the change.
        """
        by_id: Dict[str, Action] = {}
        for action in candidates:
            existing = by_id.get(action.id)
            if existing is None:
                by_id[action.id] = action
                continue
            for key, summary in zip(action.trigger_findings, action.trigger_summaries):
                if key not in existing.trigger_findings:
                    existing.trigger_findings.append(key)
                    existing.trigger_summaries.append(summary)
            existing.confidence = max(existing.confidence, action.confidence)
            existing.axes = sorted(set(existing.axes) | set(action.axes))
            existing.rooms = sorted(set(existing.rooms) | set(action.rooms))
        return [by_id[key] for key in sorted(by_id)]

    def _filter(self, candidates: List[Action], finding_set: FindingSet,
                plan: ActionPlan) -> List[Action]:
        """Drop actions this configuration will not run, with reasons."""
        kept: List[Action] = []
        for action in candidates:
            reason = self._rejection(action, finding_set)
            if reason:
                action.excluded = True
                action.excluded_reason = reason
                continue
            kept.append(action)
        return kept

    def _rejection(self, action: Action, finding_set: FindingSet) -> str:
        if not self.config.permits(action):
            return f"{action.type} is not enabled for this run"
        if not self.config.include_unverifiable and ranking._unverifiable(
            action, finding_set
        ):
            return ("targets only axes the evaluation could not measure, so "
                    "the result could not be verified")
        if action.expected_gain < self.config.min_expected_gain:
            return (f"expected gain {action.expected_gain:.4f} is below the "
                    f"{self.config.min_expected_gain} floor")
        return ""

    # -- reporting ----------------------------------------------------------

    def _diagnostics(self, finding_set: FindingSet, groups, plan: ActionPlan,
                     started: float) -> Dict[str, Any]:
        merged = [g for g in groups if len(g.findings) > 1]
        return {
            "planner_version": PLANNER_VERSION,
            "findings_considered": len(finding_set),
            "root_causes": len(groups),
            "findings_merged": sum(len(g.findings) for g in merged),
            "merged_groups": [
                {
                    "subsystem": g.subsystem,
                    "scope": g.scope,
                    "action_type": g.action_type,
                    "findings": [f.summary for f in g.findings],
                }
                for g in merged
            ],
            "unactionable": [
                {"finding": f.summary, "subsystem": f.subsystem, "reason": reason}
                for f, reason in grouping.unactionable(finding_set)
            ],
            "estimates": {
                action.id: ranking.explain(action, finding_set)
                for action in plan.ordered
            },
            "dependencies": dependencies.describe(plan.graph),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def _notes(self, finding_set: FindingSet, plan: ActionPlan) -> List[str]:
        notes: List[str] = []
        if not finding_set.findings:
            notes.append("the evaluation reported no findings; nothing to plan")
        if finding_set.unmeasured_axes:
            notes.append(
                "axes the evaluation could not measure, so no action targeting "
                f"them was proposed: {', '.join(finding_set.unmeasured_axes)}"
            )
        if plan.excluded:
            notes.append(f"{len(plan.excluded)} candidate action(s) were dropped; "
                         "see the excluded list for each reason")
        if not plan.ordered and finding_set.findings:
            notes.append(
                "every finding was either unactionable or below the gain floor"
            )
        return notes


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


def plan(evaluation, graph, config: Optional[PlannerConfig] = None) -> ActionPlan:
    """Build a plan. The one-line entry point."""
    return Planner(graph, config).plan(evaluation)


def write_report(plan_: ActionPlan, path: str) -> str:
    """Write ``planner_report.json``.

    The plan's own document: what was proposed, what was dropped and why, how
    every estimate was arrived at, and which ordering rules bound. It is
    written before anything executes, so it can be read as a proposal rather
    than as an account of something already done.
    """
    payload = {
        "planner_version": PLANNER_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **plan_.to_dict(),
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path
