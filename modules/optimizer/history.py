"""
ArchX3D — The record of what was tried
======================================
``optimization_history.json``: every action attempted, what it changed, what it
gained, and — for the ones that did not survive — exactly why they were taken
back.

Rejections are the valuable half
--------------------------------
A history of accepted changes is a changelog. What makes this file worth
keeping is the rejected entries: they are the only record of what the
reconstruction *cannot* be improved by, and they are what stops the next run
from spending its budget rediscovering the same dead ends.

So a rejected entry carries as much as an accepted one — the parameters tried,
the measured gain, the constraint that failed — and the reason is a sentence
rather than a code.

Expected against actual
-----------------------
Every entry records both. The planner's estimate is an ordering heuristic, and
the only way to know whether it is a *good* heuristic is to write down what it
predicted next to what happened. :mod:`optimizer.metrics` reads exactly that.

Replayability
-------------
An entry holds everything needed to re-apply its action: the type, the target
and the full parameters. That is deliberate — a history from which the run
cannot be reconstructed is an account rather than a record.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HISTORY_VERSION = "1.0"

#: Outcomes an attempt can have. Every one of them is a fact about the run
#: rather than a judgement about the reconstruction.
ACCEPTED = "accepted"
REJECTED = "rejected"
BLOCKED = "blocked"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Attempt:
    """One action, tried once."""

    iteration: int
    action_id: str
    action_type: str
    target: str
    outcome: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    #: What the planner thought it would gain.
    expected_gain: float = 0.0
    #: What it actually gained. Negative when the change made things worse.
    actual_gain: float = 0.0
    score_before: float = 0.0
    score_after: float = 0.0
    #: Per-axis movement, so a change can be attributed to what it touched.
    axis_deltas: Dict[str, float] = field(default_factory=dict)

    #: Why an attempt did not survive. Always populated when the outcome is
    #: anything but ``accepted``.
    rollback_reason: str = ""
    #: Which constraints failed, when that is what happened.
    violations: List[Dict[str, Any]] = field(default_factory=list)
    #: Field-level record of what the mutation changed.
    changes: List[Dict[str, Any]] = field(default_factory=list)

    #: Explainability, carried from the action.
    trigger_findings: List[str] = field(default_factory=list)
    trigger_summaries: List[str] = field(default_factory=list)
    rooms: List[str] = field(default_factory=list)
    objects: List[str] = field(default_factory=list)
    materials: List[str] = field(default_factory=list)

    duration_ms: int = 0
    timestamp: str = ""

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED

    @property
    def estimate_error(self) -> float:
        """How wrong the estimate was. Positive means over-optimistic."""
        return self.expected_gain - self.actual_gain

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "iteration": self.iteration,
            "action": self.action_id,
            "type": self.action_type,
            "target": self.target,
            "outcome": self.outcome,
            "parameters": self.parameters,
            "expected_gain": round(self.expected_gain, 5),
            "actual_gain": round(self.actual_gain, 5),
            "estimate_error": round(self.estimate_error, 5),
            "score_before": round(self.score_before, 5),
            "score_after": round(self.score_after, 5),
            "axis_deltas": {k: round(v, 5) for k, v in sorted(self.axis_deltas.items())},
            "trigger_findings": list(self.trigger_findings),
            "trigger_summaries": list(self.trigger_summaries),
            "affected": {
                "rooms": list(self.rooms),
                "objects": list(self.objects),
                "materials": list(self.materials),
            },
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }
        if self.changes:
            out["changes"] = self.changes
        if self.rollback_reason:
            out["rollback_reason"] = self.rollback_reason
        if self.violations:
            out["violations"] = self.violations
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Attempt":
        affected = d.get("affected") or {}
        return Attempt(
            iteration=int(d.get("iteration", 0) or 0),
            action_id=str(d.get("action", "")),
            action_type=str(d.get("type", "")),
            target=str(d.get("target", "")),
            outcome=str(d.get("outcome", "")),
            parameters=dict(d.get("parameters") or {}),
            expected_gain=float(d.get("expected_gain", 0.0) or 0.0),
            actual_gain=float(d.get("actual_gain", 0.0) or 0.0),
            score_before=float(d.get("score_before", 0.0) or 0.0),
            score_after=float(d.get("score_after", 0.0) or 0.0),
            axis_deltas=dict(d.get("axis_deltas") or {}),
            rollback_reason=str(d.get("rollback_reason", "")),
            violations=list(d.get("violations") or []),
            changes=list(d.get("changes") or []),
            trigger_findings=list(d.get("trigger_findings") or []),
            trigger_summaries=list(d.get("trigger_summaries") or []),
            rooms=list(affected.get("rooms") or []),
            objects=list(affected.get("objects") or []),
            materials=list(affected.get("materials") or []),
            duration_ms=int(d.get("duration_ms", 0) or 0),
            timestamp=str(d.get("timestamp", "")),
        )


@dataclass
class History:
    """Every attempt in one run, in the order they happened."""

    attempts: List[Attempt] = field(default_factory=list)
    baseline_score: float = 0.0
    final_score: float = 0.0
    stop_reason: str = ""
    stop_detail: str = ""
    started_at: str = ""
    duration_ms: int = 0
    #: The plan this run executed, for cross-reference.
    plan_summary: Dict[str, Any] = field(default_factory=dict)

    # -- recording ----------------------------------------------------------

    def add(self, attempt: Attempt) -> Attempt:
        attempt.timestamp = attempt.timestamp or _now()
        self.attempts.append(attempt)
        return attempt

    # -- queries ------------------------------------------------------------

    @property
    def accepted(self) -> List[Attempt]:
        return [a for a in self.attempts if a.outcome == ACCEPTED]

    @property
    def rejected(self) -> List[Attempt]:
        return [a for a in self.attempts if a.outcome != ACCEPTED]

    @property
    def total_gain(self) -> float:
        return self.final_score - self.baseline_score

    def for_action(self, action_id: str) -> Optional[Attempt]:
        return next((a for a in self.attempts if a.action_id == action_id), None)

    def attempted_ids(self) -> List[str]:
        return [a.action_id for a in self.attempts]

    def by_type(self) -> Dict[str, Dict[str, Any]]:
        """Per action type: how often it was tried and what it delivered.

        The table that answers "which kind of change is actually worth making
        on this project", which is the question a second run wants answered
        before it spends anything.
        """
        tally: Dict[str, Dict[str, Any]] = {}
        for attempt in self.attempts:
            entry = tally.setdefault(attempt.action_type, {
                "attempted": 0, "accepted": 0, "total_gain": 0.0,
                "expected_gain": 0.0,
            })
            entry["attempted"] += 1
            entry["expected_gain"] += attempt.expected_gain
            if attempt.accepted:
                entry["accepted"] += 1
                entry["total_gain"] += attempt.actual_gain
        for entry in tally.values():
            entry["total_gain"] = round(entry["total_gain"], 5)
            entry["expected_gain"] = round(entry["expected_gain"], 5)
            entry["acceptance_rate"] = (
                round(entry["accepted"] / entry["attempted"], 3)
                if entry["attempted"] else 0.0
            )
        return dict(sorted(tally.items()))

    def rejection_reasons(self) -> Dict[str, int]:
        tally: Dict[str, int] = {}
        for attempt in self.rejected:
            reason = attempt.rollback_reason or attempt.outcome
            head = reason.split(":", 1)[0].strip() or "unspecified"
            tally[head] = tally.get(head, 0) + 1
        return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))

    def summary(self) -> str:
        if not self.attempts:
            return "nothing attempted"
        return (f"{len(self.accepted)} accepted, {len(self.rejected)} rejected, "
                f"score {self.baseline_score:.4f} -> {self.final_score:.4f} "
                f"({self.total_gain:+.4f})")

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "history_version": HISTORY_VERSION,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "baseline_score": round(self.baseline_score, 5),
            "final_score": round(self.final_score, 5),
            "total_gain": round(self.total_gain, 5),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "counts": {
                "attempted": len(self.attempts),
                "accepted": len(self.accepted),
                "rejected": len(self.rejected),
            },
            "by_type": self.by_type(),
            "rejection_reasons": self.rejection_reasons(),
            "plan": self.plan_summary,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "History":
        return History(
            attempts=[Attempt.from_dict(a) for a in (d.get("attempts") or [])],
            baseline_score=float(d.get("baseline_score", 0.0) or 0.0),
            final_score=float(d.get("final_score", 0.0) or 0.0),
            stop_reason=str(d.get("stop_reason", "")),
            stop_detail=str(d.get("stop_detail", "")),
            started_at=str(d.get("started_at", "")),
            duration_ms=int(d.get("duration_ms", 0) or 0),
            plan_summary=dict(d.get("plan") or {}),
        )

    def save(self, path: str) -> str:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
        return path

    @staticmethod
    def load(path: str) -> "History":
        """Read a previous run's history, or an empty one.

        A missing or corrupt history is not an error: it means no run has
        happened yet, or one was interrupted, and either way the next run
        starts from the graph rather than from the file.
        """
        if not os.path.exists(path):
            return History()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return History.from_dict(json.load(handle))
        except (OSError, json.JSONDecodeError):
            return History()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
