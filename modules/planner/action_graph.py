"""
ArchX3D — Actions and the graph they form
=========================================
What an action *is*, and how a set of them is ordered into something safe to
execute.

An action is an instruction, not an observation
-----------------------------------------------
"The render is too warm" is a finding. "Set room_a's lighting colour
temperature to 3100 K, because of these three findings" is an action. The
difference is the whole point of the planning layer: an action names a target,
a field, and a value, so the optimiser can apply it, check it, and — crucially
— *undo* it, without re-deriving intent from prose.

Every action carries the findings that triggered it. Not for the optimiser,
which never reads them, but for the report: an accepted change nobody can
trace back to evidence is indistinguishable from a random walk.

The graph
---------
Actions are not independent. Correcting a camera changes what "misplaced"
means for every object measured through it; changing a room's style re-derives
the materials a material adjustment was about to tune. So the plan is a
directed acyclic graph of *must-run-before* edges, plus a set of mutual
exclusions, and execution walks it in dependency order with rank breaking ties.

Cycles are possible in principle — two rules can each claim precedence — and
are broken deterministically at the weakest edge rather than raised, because a
plan that cannot be executed is worse than a plan executed in a slightly
suboptimal order.

Determinism
-----------
Every ordering here is total: rank, then cost, then action id. No dictionary
iteration order reaches an output, and nothing is random. Two runs over the
same evaluation produce the same plan, which is what makes an optimisation run
reproducible and a regression test possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


class ActionType:
    """The vocabulary of changes the optimiser knows how to make.

    Deliberately closed. An action type exists only where there is a mutation
    that applies it, a constraint set that validates it, and a rollback that
    undoes it — a planner able to propose changes nobody can execute would
    produce plans that look richer and achieve less.
    """

    #: Tune a material's saturation, roughness or tint.
    MATERIAL_ADJUSTMENT = "material_adjustment"
    #: Change a room's LightingEnvironment — ambient, warmth, direction.
    LIGHTING_ADJUSTMENT = "lighting_adjustment"
    #: Shift the colour roles a room's surfaces are drawn from.
    PALETTE_ADJUSTMENT = "palette_adjustment"
    #: Swap an object's asset for a better-matching one in the same category.
    ASSET_REPLACEMENT = "asset_replacement"
    #: Swap between variants of the asset already chosen.
    ASSET_VARIANT_SWAP = "asset_variant_swap"
    #: Admit or withhold low-confidence decor.
    DECOR_DENSITY = "decor_density"
    #: Turn an object about its vertical axis.
    FURNITURE_ROTATION = "furniture_rotation"
    #: Move an object in the floor plane.
    FURNITURE_TRANSLATION = "furniture_translation"
    #: Rescale an object's dimensions.
    FURNITURE_SCALE = "furniture_scale_refinement"
    #: Correct a fitted ViewPoint.
    CAMERA_CORRECTION = "camera_correction"
    #: Change a room's style label, which re-derives materials and assets.
    STYLE_REFINEMENT = "style_refinement"

    ALL: Tuple[str, ...] = (
        MATERIAL_ADJUSTMENT, LIGHTING_ADJUSTMENT, PALETTE_ADJUSTMENT,
        ASSET_REPLACEMENT, ASSET_VARIANT_SWAP, DECOR_DENSITY,
        FURNITURE_ROTATION, FURNITURE_TRANSLATION, FURNITURE_SCALE,
        CAMERA_CORRECTION, STYLE_REFINEMENT,
    )

    #: Types that change only a stored ViewPoint. They need no Blender
    #: rebuild — the preview renderer reconstructs cameras from the graph —
    #: which makes them an order of magnitude cheaper than everything else.
    CAMERA_ONLY: Tuple[str, ...] = (CAMERA_CORRECTION,)

    #: Types that alter what gets built, so the .blend must be regenerated
    #: before the change can be seen. Derived below the class body — a
    #: comprehension inside it cannot see the class's own names.
    REQUIRES_REBUILD: Tuple[str, ...] = ()


ActionType.REQUIRES_REBUILD = tuple(
    action_type for action_type in ActionType.ALL
    if action_type not in ActionType.CAMERA_ONLY
)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """One executable, reversible, explainable change.

    ``parameters`` is a plain dict rather than a typed subclass per action: it
    has to survive a JSON round trip into the plan report and the history file,
    and the per-type contract is enforced where it can actually be checked —
    in the mutation that applies it, against a real scene graph.
    """

    #: Stable within a plan; ``type:scope``, so the same problem in the same
    #: place yields the same id across runs.
    id: str
    type: str
    #: What the action changes: ``room:<id>``, ``object:<id>``,
    #: ``material:<name>`` or ``viewpoint:<id>``.
    target: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    #: One sentence a person can read.
    summary: str = ""
    #: Why this action, from the findings that triggered it.
    rationale: str = ""

    #: Estimated similarity gain, ``[0, 1]``. An ordering heuristic, not a
    #: prediction — the optimiser measures the real thing and records both.
    expected_gain: float = 0.0
    #: Relative execution cost. 1.0 is one rebuild-and-evaluate cycle.
    cost: float = 1.0
    #: Ranking score, computed by :mod:`planner.ranking`.
    priority: float = 0.0
    #: How much to trust the estimate, from the triggering findings.
    confidence: float = 0.0

    #: Keys of the findings this action answers. Explainability only.
    trigger_findings: List[str] = field(default_factory=list)
    #: Human-readable summaries of those findings, so a report is legible
    #: without cross-referencing the evaluation.
    trigger_summaries: List[str] = field(default_factory=list)

    rooms: List[str] = field(default_factory=list)
    objects: List[str] = field(default_factory=list)
    materials: List[str] = field(default_factory=list)
    #: The axes this action is trying to move, for attributing the outcome.
    axes: List[str] = field(default_factory=list)

    #: Set by the planner when an action is dropped before execution.
    excluded: bool = False
    excluded_reason: str = ""

    # -- properties ---------------------------------------------------------

    @property
    def requires_rebuild(self) -> bool:
        return self.type in ActionType.REQUIRES_REBUILD

    @property
    def target_kind(self) -> str:
        return self.target.split(":", 1)[0] if ":" in self.target else ""

    @property
    def target_id(self) -> str:
        return self.target.split(":", 1)[1] if ":" in self.target else self.target

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "parameters": self.parameters,
            "summary": self.summary,
            "rationale": self.rationale,
            "expected_gain": round(self.expected_gain, 4),
            "cost": round(self.cost, 3),
            "priority": round(self.priority, 4),
            "confidence": round(self.confidence, 3),
            "requires_rebuild": self.requires_rebuild,
            "trigger_findings": list(self.trigger_findings),
            "trigger_summaries": list(self.trigger_summaries),
            "rooms": list(self.rooms),
            "objects": list(self.objects),
            "materials": list(self.materials),
            "axes": list(self.axes),
        }
        if self.excluded:
            out["excluded"] = True
            out["excluded_reason"] = self.excluded_reason
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Action":
        return Action(
            id=str(d.get("id", "")),
            type=str(d.get("type", "")),
            target=str(d.get("target", "")),
            parameters=dict(d.get("parameters") or {}),
            summary=str(d.get("summary", "")),
            rationale=str(d.get("rationale", "")),
            expected_gain=float(d.get("expected_gain", 0.0) or 0.0),
            cost=float(d.get("cost", 1.0) or 1.0),
            priority=float(d.get("priority", 0.0) or 0.0),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            trigger_findings=list(d.get("trigger_findings") or []),
            trigger_summaries=list(d.get("trigger_summaries") or []),
            rooms=list(d.get("rooms") or []),
            objects=list(d.get("objects") or []),
            materials=list(d.get("materials") or []),
            axes=list(d.get("axes") or []),
            excluded=bool(d.get("excluded", False)),
            excluded_reason=str(d.get("excluded_reason", "")),
        )


def sort_key(action: Action) -> Tuple[float, float, str]:
    """Total order over actions: priority, then cheapness, then id.

    Total rather than merely sorted — two actions with identical priority and
    cost still order the same way every run, because the id breaks the tie.
    A plan whose order wobbles cannot be diffed between runs.
    """
    return (-round(action.priority, 6), round(action.cost, 6), action.id)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


@dataclass
class ActionGraph:
    """Actions plus the ordering constraints between them.

    Edges are ``(before, after)``: the first action must be executed before
    the second. Exclusions are unordered pairs that must never both run.
    """

    actions: List[Action] = field(default_factory=list)
    #: ``before_id -> {after_id, ...}``
    edges: Dict[str, Set[str]] = field(default_factory=dict)
    #: Why each edge exists, for the report.
    edge_reasons: Dict[Tuple[str, str], str] = field(default_factory=dict)
    #: Pairs that must not both execute, with the reason.
    exclusions: List[Tuple[str, str, str]] = field(default_factory=list)
    #: Edges removed to break a cycle, kept so the report can say so.
    broken_edges: List[Tuple[str, str, str]] = field(default_factory=list)

    # -- construction -------------------------------------------------------

    def add(self, action: Action) -> None:
        self.actions.append(action)

    def link(self, before: str, after: str, reason: str = "") -> None:
        """Require ``before`` to execute before ``after``.

        Self-edges and duplicates are ignored rather than raised: the rule set
        is declarative and several rules legitimately imply the same edge.
        """
        if before == after:
            return
        self.edges.setdefault(before, set()).add(after)
        self.edge_reasons.setdefault((before, after), reason)

    def exclude(self, first: str, second: str, reason: str) -> None:
        if first == second:
            return
        pair = tuple(sorted((first, second)))
        if any(pair == (a, b) for a, b, _ in self.exclusions):
            return
        self.exclusions.append((pair[0], pair[1], reason))

    # -- queries ------------------------------------------------------------

    def action(self, action_id: str) -> Optional[Action]:
        return next((a for a in self.actions if a.id == action_id), None)

    def active(self) -> List[Action]:
        """Actions that survived exclusion, in rank order."""
        return sorted((a for a in self.actions if not a.excluded), key=sort_key)

    def predecessors(self, action_id: str) -> List[str]:
        return sorted(
            before for before, afters in self.edges.items() if action_id in afters
        )

    # -- ordering -----------------------------------------------------------

    def topological_order(self) -> List[Action]:
        """Execution order: dependencies first, rank breaking ties.

        Kahn's algorithm with a deterministic tie-break. Where several actions
        are ready at once the highest-ranked goes first, which is what makes
        the plan's order match its stated priorities wherever the dependencies
        permit it.

        Any cycle is broken before this runs; see :meth:`break_cycles`.
        """
        active = {a.id: a for a in self.active()}
        indegree = {aid: 0 for aid in active}
        for before, afters in self.edges.items():
            if before not in active:
                continue
            for after in afters:
                if after in indegree:
                    indegree[after] += 1

        ready = sorted((active[aid] for aid, degree in indegree.items() if degree == 0),
                       key=sort_key)
        ordered: List[Action] = []

        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for after in sorted(self.edges.get(current.id, ())):
                if after not in indegree:
                    continue
                indegree[after] -= 1
                if indegree[after] == 0:
                    ready.append(active[after])
            ready.sort(key=sort_key)

        # Anything left is in a cycle that break_cycles missed. Appending it in
        # rank order is better than dropping it: an action executed in the
        # wrong order is measured and rolled back if it did not help, whereas a
        # dropped one is silently never tried.
        remaining = [a for aid, a in active.items()
                     if aid not in {o.id for o in ordered}]
        ordered.extend(sorted(remaining, key=sort_key))
        return ordered

    def break_cycles(self) -> List[Tuple[str, str, str]]:
        """Remove the weakest edge of every cycle, deterministically.

        Two ordering rules can each claim to come first — a camera correction
        before a translation, a translation's room needing its style settled
        first — and the result is unexecutable. The edge removed is the one
        into the *highest-ranked* action, on the grounds that the plan's best
        action is the one least worth delaying.
        """
        removed: List[Tuple[str, str, str]] = []
        rank = {a.id: index for index, a in enumerate(self.active())}

        while True:
            cycle = self._find_cycle(set(rank))
            if not cycle:
                break
            # The edge to drop: the one whose *target* is ranked best.
            candidates = [
                (cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))
            ]
            before, after = min(
                candidates, key=lambda pair: (rank.get(pair[1], 10 ** 6), pair[0], pair[1])
            )
            reason = self.edge_reasons.get((before, after), "ordering rule")
            self.edges.get(before, set()).discard(after)
            removed.append((before, after, reason))

        self.broken_edges.extend(removed)
        return removed

    def _find_cycle(self, allowed: Set[str]) -> List[str]:
        """One cycle among the allowed nodes, or an empty list."""
        colour: Dict[str, int] = {}
        stack: List[str] = []

        def visit(node: str) -> List[str]:
            colour[node] = 1
            stack.append(node)
            for neighbour in sorted(self.edges.get(node, ())):
                if neighbour not in allowed:
                    continue
                if colour.get(neighbour, 0) == 1:
                    return stack[stack.index(neighbour):]
                if colour.get(neighbour, 0) == 0:
                    found = visit(neighbour)
                    if found:
                        return found
            colour[node] = 2
            stack.pop()
            return []

        for node in sorted(allowed):
            if colour.get(node, 0) == 0:
                found = visit(node)
                if found:
                    return found
        return []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edges": [
                {"before": before, "after": after,
                 "reason": self.edge_reasons.get((before, after), "")}
                for before in sorted(self.edges)
                for after in sorted(self.edges[before])
            ],
            "exclusions": [
                {"kept": a, "dropped": b, "reason": reason}
                for a, b, reason in self.exclusions
            ],
            "broken_edges": [
                {"before": a, "after": b, "reason": reason}
                for a, b, reason in self.broken_edges
            ],
        }


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class ActionPlan:
    """What the planner hands the optimiser.

    Ordered, deduplicated, dependency-resolved and free of contradictions. The
    optimiser walks :attr:`ordered` and needs nothing else — no findings, no
    evaluation result, no scene graph knowledge beyond what each action names.
    """

    #: Execution order.
    ordered: List[Action] = field(default_factory=list)
    #: Everything considered, including what was dropped.
    considered: List[Action] = field(default_factory=list)
    graph: ActionGraph = field(default_factory=ActionGraph)

    baseline_score: float = 0.0
    #: Sum of the expected gains, capped at the headroom that actually exists.
    expected_total_gain: float = 0.0
    #: Free-form: counts, groupings, what was dropped and why.
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.ordered)

    def __iter__(self):
        return iter(self.ordered)

    @property
    def excluded(self) -> List[Action]:
        return [a for a in self.considered if a.excluded]

    def action(self, action_id: str) -> Optional[Action]:
        return next((a for a in self.considered if a.id == action_id), None)

    def of_type(self, action_type: str) -> List[Action]:
        return [a for a in self.ordered if a.type == action_type]

    def for_room(self, room_id: str) -> List[Action]:
        return [a for a in self.ordered if room_id in a.rooms]

    def summary(self) -> str:
        if not self.ordered:
            return "no actions proposed"
        dropped = len(self.excluded)
        parts = [f"{len(self.ordered)} action(s)",
                 f"expected gain {self.expected_total_gain:.3f}"]
        if dropped:
            parts.append(f"{dropped} dropped")
        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseline_score": round(self.baseline_score, 4),
            "expected_total_gain": round(self.expected_total_gain, 4),
            "counts": {
                "proposed": len(self.considered),
                "ordered": len(self.ordered),
                "excluded": len(self.excluded),
                "by_type": {
                    action_type: len([a for a in self.ordered if a.type == action_type])
                    for action_type in ActionType.ALL
                    if any(a.type == action_type for a in self.ordered)
                },
            },
            "actions": [a.to_dict() for a in self.ordered],
            "excluded": [a.to_dict() for a in self.excluded],
            "graph": self.graph.to_dict(),
            "diagnostics": self.diagnostics,
            "notes": list(self.notes),
        }
