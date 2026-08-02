"""
ArchX3D — When to stop
======================
Four conditions, checked after every iteration. The first that fires ends the
run and says which one it was.

Why stopping deserves its own module
------------------------------------
A refinement loop with no stopping rule is not a refinement loop, it is a
random walk with a budget. And a loop whose stopping logic is scattered through
its control flow stops for reasons nobody can state afterwards — "it did nine
iterations" is not an answer to "why did it stop at nine".

Gathering the conditions here makes them enumerable, testable in isolation, and
reportable: every run ends with a named reason, and the metrics say how close
the run came to each of the other three.

The four
--------
``target_reached``  the score met the goal. The only happy ending.
``max_iterations``  the budget ran out. Says nothing about quality — the run
                    may have been improving steadily when it stopped, which
                    the report notes so a longer budget can be chosen.
``no_gain``         several actions in a row failed to improve anything. The
                    plan is aimed at the wrong things; continuing spends
                    renders to learn the same thing again.
``below_epsilon``   improvements have become too small to be distinguishable
                    from measurement noise. Continuing would be optimising
                    the evaluator rather than the reconstruction.

The last is the subtle one. A preview render is deterministic, so repeated
evaluation of an unchanged scene gives an identical score — but two *different*
scenes scoring 0.7314 and 0.7316 are not meaningfully different, and a loop
that keeps chasing the fourth decimal will happily accept changes that are
noise-shaped rather than real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Reason codes. Strings rather than an enum because they travel into JSON and
#: are read by people.
TARGET_REACHED = "target_reached"
MAX_ITERATIONS = "max_iterations"
NO_GAIN = "no_gain"
BELOW_EPSILON = "below_epsilon"
PLAN_EXHAUSTED = "plan_exhausted"
NOT_STOPPED = ""


@dataclass
class StoppingPolicy:
    """The rules one run stops by."""

    #: Stop once the building score reaches this. ``1.0`` effectively means
    #: "keep going until something else stops you", which is the honest
    #: default: the pipeline has no idea what score a given reconstruction can
    #: reach, and a made-up target would end runs early or never.
    target_score: float = 1.0

    #: Hard budget. Each iteration is a rebuild, a render and an evaluation —
    #: tens of seconds — so this is the number that decides how long a run
    #: takes.
    max_iterations: int = 12

    #: Consecutive rejections before giving up. Three rather than one because
    #: a plan is ordered by an *estimate*: the best-estimated action failing
    #: says little about the next one, and stopping on the first rejection
    #: would discard a plan on one bad guess.
    max_consecutive_rejections: int = 3

    #: Improvements below this do not count as improvements. Two decimal
    #: places of a similarity score is about where a difference stops being
    #: visible in the render it came from.
    epsilon: float = 0.002

    #: Stop when the total improvement over the last few accepted actions is
    #: itself below epsilon — a loop making real but vanishing progress.
    plateau_window: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_score": self.target_score,
            "max_iterations": self.max_iterations,
            "max_consecutive_rejections": self.max_consecutive_rejections,
            "epsilon": self.epsilon,
            "plateau_window": self.plateau_window,
        }


@dataclass
class StopDecision:
    """Whether to stop, and the reason in words."""

    stop: bool = False
    reason: str = NOT_STOPPED
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"stop": self.stop, "reason": self.reason, "detail": self.detail}


@dataclass
class RunState:
    """What the loop knows about itself so far.

    Held here rather than in the optimiser so that every stopping rule reads
    the same numbers — a condition evaluated against a differently-counted
    iteration is a condition that fires at the wrong time.
    """

    iterations: int = 0
    score: float = 0.0
    baseline_score: float = 0.0
    consecutive_rejections: int = 0
    accepted: int = 0
    rejected: int = 0
    #: Score after every iteration, including the starting point.
    trajectory: List[float] = field(default_factory=list)
    #: Gain of each accepted action, in order.
    accepted_gains: List[float] = field(default_factory=list)
    actions_remaining: int = 0

    def record(self, score: float, accepted: bool, gain: float = 0.0) -> None:
        self.iterations += 1
        self.score = score
        self.trajectory.append(round(score, 6))
        if accepted:
            self.accepted += 1
            self.consecutive_rejections = 0
            self.accepted_gains.append(round(gain, 6))
        else:
            self.rejected += 1
            self.consecutive_rejections += 1

    @property
    def total_gain(self) -> float:
        return self.score - self.baseline_score


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def should_stop(state: RunState, policy: StoppingPolicy) -> StopDecision:
    """Check every condition, in the order a person would.

    Target first because it is the only good reason; budget next because it is
    the only certain one; then the two that mean "this is not working".
    """
    if state.score >= policy.target_score:
        return StopDecision(
            True, TARGET_REACHED,
            f"score {state.score:.4f} reached the {policy.target_score:.4f} target",
        )

    if state.iterations >= policy.max_iterations:
        return StopDecision(
            True, MAX_ITERATIONS,
            f"{state.iterations} iterations is the configured budget"
            + (f"; the score was still rising ({state.accepted_gains[-1]:+.4f} "
               f"on the last accepted action), so a larger budget may help"
               if state.accepted_gains and state.accepted_gains[-1] > policy.epsilon
               else ""),
        )

    if state.consecutive_rejections >= policy.max_consecutive_rejections:
        return StopDecision(
            True, NO_GAIN,
            f"{state.consecutive_rejections} actions in a row failed to improve "
            f"the score; the plan is aimed at the wrong things and continuing "
            f"would spend renders to learn it again",
        )

    if state.actions_remaining <= 0:
        return StopDecision(
            True, PLAN_EXHAUSTED,
            "every action in the plan has been attempted",
        )

    plateau = _plateau(state, policy)
    if plateau is not None:
        return StopDecision(True, BELOW_EPSILON, plateau)

    return StopDecision(False)


def _plateau(state: RunState, policy: StoppingPolicy) -> Optional[str]:
    """Whether the last few accepted actions together achieved nothing.

    Judged on accepted actions rather than iterations: a run alternating
    between a useful change and a rejected one is making progress, and
    counting the rejections would stop it. What this catches is a run whose
    accepted changes have shrunk to nothing.
    """
    window = policy.plateau_window
    if window <= 0 or len(state.accepted_gains) < window:
        return None
    recent = state.accepted_gains[-window:]
    total = sum(recent)
    if total >= policy.epsilon:
        return None
    return (f"the last {window} accepted actions gained {total:+.5f} in total, "
            f"below the {policy.epsilon} epsilon — further changes would be "
            f"smaller than the difference is worth")


def accepts(gain: float, policy: StoppingPolicy) -> bool:
    """Whether a measured gain is large enough to keep the change.

    Strictly greater than epsilon. A change that gained exactly nothing is
    rejected, not kept: the graph is simpler without it, and an accepted no-op
    would make the history claim an improvement that did not happen.
    """
    return gain > policy.epsilon


def describe(decision: StopDecision, state: RunState) -> str:
    """A sentence for the console and the report."""
    if not decision.stop:
        return f"continuing at {state.score:.4f} after {state.iterations} iteration(s)"
    return (f"stopped after {state.iterations} iteration(s) — {decision.reason}: "
            f"{decision.detail}")
