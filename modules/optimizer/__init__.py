"""
ArchX3D — Optimisation
======================
Executes an :class:`planner.action_graph.ActionPlan` against a scene graph,
keeping only the changes that measurably improve the reconstruction.

    ActionPlan ──► apply ──► validate ──► render ──► evaluate ──► keep or undo

Measured, never predicted
-------------------------
Each action is applied, the scene is rebuilt and re-evaluated, and the change
survives only if the score actually rose. The planner's expected gain decides
what to try first and nothing else — so a wrong estimate costs one iteration
rather than a degraded reconstruction.

Hard guarantees
---------------
* **No model calls.** Nothing here invokes a vision model or any other. Every
  value applied came from a measurement, through the planner, as arithmetic.
* **No DXF geometry.** Walls, doors, windows and structural elements are
  immutable, checked before an action runs and again after it has, against a
  digest taken beforehand.
* **No locked objects.** A lock is the user's statement of ground truth.
* **Deterministic.** Same plan, same graph, same executor, same run.

Modules
-------
``optimizer``    the loop: pick, validate, apply, measure, keep or undo
``constraints``  what may never be touched, and what must remain true
``mutations``    the only code that writes to a scene graph on its behalf
``rollback``     whole-graph snapshots and in-place restore
``stopping``     the four conditions a run ends on
``history``      ``optimization_history.json`` — every attempt, including the
                 rejected ones, which are the valuable half
``metrics``      ``metrics.json`` — trajectory, attribution, and how well the
                 planner's estimates held up

Execution is injected
---------------------
The loop never imports Blender. It calls an executor that turns a graph into an
evaluation; :func:`optimizer.pipeline.build_executor` wires the real one, and
tests pass a function. Every decision the loop makes is therefore testable
without a render.
"""

from .constraints import ConstraintReport, Violation, check_action, check_graph
from .history import Attempt, History
from .metrics import Metrics
from .optimizer import ExecutionResult, Optimizer, OptimizerConfig
from .rollback import Snapshot, restore, take
from .stopping import RunState, StopDecision, StoppingPolicy

OPTIMIZER_VERSION = "1.0"

__all__ = [
    "OPTIMIZER_VERSION",
    "Attempt",
    "ConstraintReport",
    "ExecutionResult",
    "History",
    "Metrics",
    "Optimizer",
    "OptimizerConfig",
    "RunState",
    "Snapshot",
    "StopDecision",
    "StoppingPolicy",
    "Violation",
    "check_action",
    "check_graph",
    "restore",
    "take",
]
