"""
ArchX3D — What the run tells you about itself
=============================================
``metrics.json``: the score trajectory, where the gain came from, and how well
the planner's estimates held up.

Three questions, three sections
-------------------------------
**Did it work?** The trajectory — score after every iteration, per axis and per
room — and the total gain. This is the number a regression test compares
between runs.

**Where did the gain come from?** Attribution by action type, by room and by
axis. A run that gained 0.08 entirely from one lighting change is a different
run from one that gained 0.08 from twelve small ones, and only the first
suggests what to try next time.

**Was the plan any good?** Calibration: expected gain against measured gain,
per action type. The planner's efficacy priors are stated guesses
(:mod:`planner.ranking`), and this is the evidence for revising them. A type
that is consistently over-estimated is wasting the top of every queue.

Why calibration is worth the trouble
------------------------------------
The estimates only order the queue, so being wrong about magnitude is cheap.
Being wrong about *order* is not: a systematically optimistic prior puts its
action first every run, and every run spends its most expensive iteration
finding out again. Measuring the error is what turns that from a recurring
cost into a one-off correction.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

METRICS_VERSION = "1.0"


@dataclass
class Metrics:
    """Everything measurable about one optimisation run."""

    baseline_score: float = 0.0
    final_score: float = 0.0
    #: Score after each iteration, starting with the baseline.
    trajectory: List[float] = field(default_factory=list)
    #: Axis -> score at the start and at the end.
    axis_before: Dict[str, float] = field(default_factory=dict)
    axis_after: Dict[str, float] = field(default_factory=dict)
    room_before: Dict[str, float] = field(default_factory=dict)
    room_after: Dict[str, float] = field(default_factory=dict)

    iterations: int = 0
    accepted: int = 0
    rejected: int = 0
    #: Wall-clock, and where it went.
    duration_ms: int = 0
    render_ms: int = 0
    evaluate_ms: int = 0

    stop_reason: str = ""
    #: Per action type: attempts, acceptance, gain and estimate error.
    attribution: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    calibration: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    # -- derived ------------------------------------------------------------

    @property
    def total_gain(self) -> float:
        return self.final_score - self.baseline_score

    @property
    def acceptance_rate(self) -> float:
        total = self.accepted + self.rejected
        return round(self.accepted / total, 3) if total else 0.0

    @property
    def gain_per_iteration(self) -> float:
        return round(self.total_gain / self.iterations, 5) if self.iterations else 0.0

    def axis_deltas(self) -> Dict[str, float]:
        return {
            axis: round(self.axis_after.get(axis, 0.0) - before, 5)
            for axis, before in sorted(self.axis_before.items())
            if axis in self.axis_after
        }

    def room_deltas(self) -> Dict[str, float]:
        return {
            room: round(self.room_after.get(room, 0.0) - before, 5)
            for room, before in sorted(self.room_before.items())
            if room in self.room_after
        }

    def regressions(self) -> Dict[str, float]:
        """Axes that ended worse than they started.

        Possible even in a run whose total gain is positive: the score is a
        weighted mean, so a large lighting improvement can carry a small
        material regression along with it. Naming them is the difference
        between a run that improved the building and a run that traded one
        axis for another without saying so.
        """
        return {axis: delta for axis, delta in self.axis_deltas().items() if delta < 0}

    def summary(self) -> str:
        if not self.iterations:
            return "no iterations run"
        return (f"{self.baseline_score:.4f} -> {self.final_score:.4f} "
                f"({self.total_gain:+.4f}) over {self.iterations} iteration(s), "
                f"{self.accepted} accepted, stopped on {self.stop_reason}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metrics_version": METRICS_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "score": {
                "baseline": round(self.baseline_score, 5),
                "final": round(self.final_score, 5),
                "total_gain": round(self.total_gain, 5),
                "gain_per_iteration": self.gain_per_iteration,
                "trajectory": [round(v, 5) for v in self.trajectory],
            },
            "axes": {
                "before": {k: round(v, 5) for k, v in sorted(self.axis_before.items())},
                "after": {k: round(v, 5) for k, v in sorted(self.axis_after.items())},
                "delta": self.axis_deltas(),
                "regressions": self.regressions(),
            },
            "rooms": {
                "before": {k: round(v, 5) for k, v in sorted(self.room_before.items())},
                "after": {k: round(v, 5) for k, v in sorted(self.room_after.items())},
                "delta": self.room_deltas(),
            },
            "run": {
                "iterations": self.iterations,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "acceptance_rate": self.acceptance_rate,
                "stop_reason": self.stop_reason,
                "duration_ms": self.duration_ms,
                "render_ms": self.render_ms,
                "evaluate_ms": self.evaluate_ms,
            },
            "attribution": self.attribution,
            "calibration": self.calibration,
            "notes": list(self.notes),
        }

    def save(self, path: str) -> str:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
        return path


# ---------------------------------------------------------------------------
# Building metrics from a run
# ---------------------------------------------------------------------------


def build(history, before, after, state, stop_reason: str,
          timings: Optional[Dict[str, int]] = None) -> Metrics:
    """Assemble metrics from a finished run.

    ``before`` and ``after`` are the first and last :class:`EvaluationResult`.
    Taking the axis and room scores from the evaluations rather than
    accumulating them during the loop keeps this honest: they are what the
    engine actually measured at each end, not a running total that could drift.
    """
    timings = timings or {}
    metrics = Metrics(
        baseline_score=history.baseline_score,
        final_score=history.final_score,
        trajectory=list(state.trajectory),
        axis_before=_axis_scores(before),
        axis_after=_axis_scores(after),
        room_before=dict(getattr(getattr(before, "building", None), "room_scores", {}) or {}),
        room_after=dict(getattr(getattr(after, "building", None), "room_scores", {}) or {}),
        iterations=state.iterations,
        accepted=state.accepted,
        rejected=state.rejected,
        duration_ms=history.duration_ms,
        render_ms=int(timings.get("render_ms", 0)),
        evaluate_ms=int(timings.get("evaluate_ms", 0)),
        stop_reason=stop_reason,
        attribution=_attribution(history),
        calibration=_calibration(history),
    )
    metrics.notes = _notes(metrics, history)
    return metrics


def _axis_scores(evaluation) -> Dict[str, float]:
    building = getattr(evaluation, "building", None)
    return {
        axis: float(score.score)
        for axis, score in (getattr(building, "axes", {}) or {}).items()
        if getattr(score, "measured", False)
    }


def _attribution(history) -> Dict[str, Dict[str, Any]]:
    """Where the gain came from, by action type and by room."""
    by_type = history.by_type()
    by_room: Dict[str, float] = {}
    for attempt in history.accepted:
        for room in attempt.rooms or ["(building)"]:
            by_room[room] = round(by_room.get(room, 0.0) + attempt.actual_gain, 5)
    return {"by_type": by_type, "by_room": dict(sorted(by_room.items()))}


def _calibration(history) -> Dict[str, Any]:
    """How close the planner's estimates came, per action type.

    Only accepted attempts count toward the per-type error. A rejected action
    was rolled back, so its "actual gain" is a measurement of a state that no
    longer exists — averaging it in would make every type look over-estimated
    in proportion to how often it failed, which is a different fact and one
    the acceptance rate already reports.
    """
    per_type: Dict[str, Dict[str, Any]] = {}
    for attempt in history.accepted:
        entry = per_type.setdefault(attempt.action_type, {
            "samples": 0, "expected": 0.0, "actual": 0.0,
        })
        entry["samples"] += 1
        entry["expected"] += attempt.expected_gain
        entry["actual"] += attempt.actual_gain

    for entry in per_type.values():
        samples = max(1, entry["samples"])
        entry["mean_expected"] = round(entry["expected"] / samples, 5)
        entry["mean_actual"] = round(entry["actual"] / samples, 5)
        entry["mean_error"] = round(entry["mean_expected"] - entry["mean_actual"], 5)
        entry["ratio"] = (round(entry["actual"] / entry["expected"], 3)
                          if entry["expected"] > 1e-9 else None)
        entry.pop("expected")
        entry.pop("actual")

    overall_expected = sum(a.expected_gain for a in history.accepted)
    overall_actual = sum(a.actual_gain for a in history.accepted)
    return {
        "by_type": dict(sorted(per_type.items())),
        "overall": {
            "expected": round(overall_expected, 5),
            "actual": round(overall_actual, 5),
            "error": round(overall_expected - overall_actual, 5),
            "ratio": (round(overall_actual / overall_expected, 3)
                      if overall_expected > 1e-9 else None),
        },
    }


def _notes(metrics: Metrics, history) -> List[str]:
    """Plain-language observations, so the numbers arrive with a reading."""
    notes: List[str] = []

    if metrics.total_gain <= 0 and metrics.iterations:
        notes.append(
            "the run finished no better than it started; every proposed action "
            "was rejected or cancelled out, which usually means the findings "
            "point at something outside the optimiser's reach"
        )

    regressions = metrics.regressions()
    if regressions:
        worst = min(regressions.items(), key=lambda kv: kv[1])
        notes.append(
            f"{len(regressions)} axis/axes ended worse than they started; "
            f"{worst[0]} lost {abs(worst[1]):.4f}. The total is still positive "
            f"because the score is a weighted mean, but the trade was not asked "
            f"for"
        )

    overall = metrics.calibration.get("overall", {})
    ratio = overall.get("ratio")
    if ratio is not None and metrics.accepted >= 2:
        if ratio < 0.5:
            notes.append(
                f"the planner delivered {ratio:.0%} of what it estimated; the "
                f"efficacy priors in planner.ranking are optimistic for this "
                f"project"
            )
        elif ratio > 2.0:
            notes.append(
                f"the planner delivered {ratio:.0%} of what it estimated; the "
                f"priors are pessimistic and better actions may be ranked too "
                f"low to reach"
            )

    if metrics.acceptance_rate < 0.34 and metrics.iterations >= 3:
        notes.append(
            f"only {metrics.acceptance_rate:.0%} of attempts were kept; the "
            f"ordering is spending expensive iterations on actions that do not "
            f"help"
        )

    return notes
