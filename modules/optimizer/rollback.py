"""
ArchX3D — Undoing a change
==========================
Snapshots of the scene graph, and restoring one.

Whole-state, not inverse operations
-----------------------------------
Every action could in principle define its own undo — subtract the translation,
restore the previous colour. That approach fails in a specific and nasty way:
an inverse that drifts from its forward operation produces a graph that is
*nearly* the previous one, and the discrepancy only surfaces after a rejected
action, which is precisely when nobody is examining the state closely. Several
near-misses accumulate into a graph nobody chose.

So a snapshot is the whole graph, serialised, and restoring is deserialising
it back over the original object. It costs a few milliseconds against an
iteration that costs tens of seconds, and it is exactly correct by
construction: restore is the identity if apply did nothing, and total if it did
everything.

Restoring in place
------------------
The graph is restored *into the same object* rather than replaced, because the
optimiser, the planner and the caller may all hold a reference to it. Handing
back a new instance would leave two of the three looking at a graph the third
has abandoned — the kind of bug that shows up as an optimisation that appears
to work and produces a file nobody expected.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Snapshot:
    """A scene graph as it stood, with enough context to explain itself."""

    #: The graph's own serialised form.
    payload: Dict[str, Any] = field(default_factory=dict)
    #: Digests of everything that must never change, for the constraint check.
    immutables: Dict[str, Any] = field(default_factory=dict)
    #: Which action this was taken before. Empty for the run's baseline.
    before_action: str = ""
    #: The score at the time, so a restore can also restore the baseline.
    score: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.payload)


def take(graph, before_action: str = "", score: float = 0.0) -> Snapshot:
    """Capture the graph's current state.

    ``to_dict`` rather than ``copy.deepcopy`` because the round trip through
    the schema's own serialisation is what the rest of the pipeline reads and
    writes — a snapshot that captured something ``from_dict`` cannot rebuild
    would restore to a state the generator could not consume.
    """
    from optimizer import constraints

    return Snapshot(
        payload=copy.deepcopy(graph.to_dict()) if graph is not None else {},
        immutables=constraints.immutable_snapshot(graph),
        before_action=before_action,
        score=float(score),
    )


def restore(graph, snapshot: Snapshot) -> bool:
    """Put the graph back, in place. Returns whether anything was restored.

    Every mutable collection is replaced wholesale. Fields are assigned rather
    than the object rebound, so every existing reference to this graph sees
    the restored state.
    """
    if graph is None or not snapshot:
        return False

    from vision.schema import SceneGraph

    restored = SceneGraph.from_dict(snapshot.payload)
    for name in ("rooms", "walls", "openings", "architecture", "lights",
                 "objects", "relationships", "viewpoints"):
        setattr(graph, name, getattr(restored, name))
    graph.floor = restored.floor
    graph.ceiling = restored.ceiling
    graph.ceiling_type = restored.ceiling_type
    graph.provenance = restored.provenance
    graph.diagnostics = restored.diagnostics
    return True


def changed(graph, snapshot: Snapshot) -> bool:
    """Whether the graph differs from a snapshot.

    Used to catch a handler that reported a change it did not make, and one
    that made a change it did not report. Both are silent failures otherwise:
    the first inflates the history, the second hides a mutation from it.
    """
    if graph is None or not snapshot:
        return False
    return graph.to_dict() != snapshot.payload


@dataclass
class RollbackLedger:
    """Every restore performed in a run, and why.

    Kept separately from the history because it answers a different question:
    the history says what the optimiser tried, this says how often it had to
    take it back. A run with many rollbacks is not a failed run — the loop is
    supposed to reject what does not help — but a run where *every* action
    rolled back means the plan was aimed at the wrong things, and that is worth
    seeing at a glance.
    """

    entries: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, action_id: str, reason: str, restored: bool,
               detail: Optional[Dict[str, Any]] = None) -> None:
        self.entries.append({
            "action": action_id,
            "reason": reason,
            "restored": restored,
            **(detail or {}),
        })

    @property
    def count(self) -> int:
        return len(self.entries)

    def reasons(self) -> Dict[str, int]:
        """How many rollbacks each kind of reason accounts for."""
        tally: Dict[str, int] = {}
        for entry in self.entries:
            kind = str(entry.get("reason", "")).split(":", 1)[0] or "unknown"
            tally[kind] = tally.get(kind, 0) + 1
        return dict(sorted(tally.items()))

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "by_reason": self.reasons(),
                "entries": list(self.entries)}
