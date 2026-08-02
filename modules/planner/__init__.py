"""
ArchX3D — Planning layer
========================
Converts evaluation findings into ranked, explainable candidate actions.

    EvaluationResult ──► ActionPlan ──► (optimiser)

Why a layer between evaluation and optimisation
-----------------------------------------------
The optimiser must never consume raw findings. A finding is an observation —
"the render is 0.42 darker than the reference" — and an optimiser fed
observations has to re-derive intent at the moment it mutates a scene graph.
That produces three edits where one would do, each measured against the
others' side effects, and a history that reads as an argument with itself.

The planner does that reasoning once, up front, where it can be inspected:
which findings share a cause, what single change answers them, in what order
changes must happen, and which pairs contradict. What comes out is a list of
instructions with absolute values, provenance, and an execution order — and
the optimiser needs nothing else.

Modules
-------
``findings``      the boundary: the only reader of an EvaluationResult
``grouping``      root causes, and the actions that answer them
``ranking``       expected gain, cost, priority
``dependencies``  ordering rules and contradictions
``action_graph``  Action, ActionPlan, and the DAG they form
``planner``       orchestration and ``planner_report.json``

Guarantees
----------
* **Deterministic.** Same evaluation and graph produce the same plan, ids and
  order included.
* **No model calls.** Every number is arithmetic over the evaluation and the
  scene graph. No vision model, no LLM, nothing sampled.
* **No mutation.** The planner reads the scene graph to compute absolute
  parameter values and never writes to it. Producing a plan changes nothing,
  which is what makes a plan safe to read before running it.
"""

from .action_graph import Action, ActionGraph, ActionPlan, ActionType
from .findings import FindingSet, PlannedFinding
from .planner import Planner, PlannerConfig, plan, write_report

PLANNER_VERSION = "1.0"

__all__ = [
    "PLANNER_VERSION",
    "Action",
    "ActionGraph",
    "ActionPlan",
    "ActionType",
    "FindingSet",
    "PlannedFinding",
    "Planner",
    "PlannerConfig",
    "plan",
    "write_report",
]
