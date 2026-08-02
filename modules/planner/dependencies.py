"""
ArchX3D — Ordering rules and contradictions
===========================================
Which actions must precede which, and which may never both run.

Why order matters at all
------------------------
Each action is applied, measured and kept or reverted on its own. That makes
the loop robust to a bad estimate but not to a bad *order*: an action measured
against a state that a later action is about to invalidate produces a verdict
about a world that will not exist.

Two examples, both real in this pipeline:

* A camera correction changes what "misplaced" means for every object measured
  through that camera. Move the furniture first and the loop is chasing an
  error in the instrument — and worse, it will *succeed*, because moving an
  object to compensate for a bad camera does improve that camera's view.
* A room's style re-derives its materials (``blender.styles.resolve_material``)
  and biases its asset matching. Tune a material first and the style change
  overwrites it, so the measured gain belonged to work that no longer exists.

Every rule below names the code that makes it true. A rule that cannot point
at a mechanism is a superstition, and ordering superstitions are expensive:
they serialise a plan that could have run in any order.

Contradictions
--------------
Some pairs must not both execute, not merely be ordered. Two ways to fix the
same stand-in asset, an object being moved and the same object being withheld:
in each case doing both is either meaningless or self-cancelling. The
lower-ranked one is dropped, with the reason recorded — a plan that silently
loses actions is indistinguishable from a planner that never proposed them.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from .action_graph import Action, ActionGraph, ActionType, sort_key

# ---------------------------------------------------------------------------
# Ordering rules
# ---------------------------------------------------------------------------

#: ``(earlier_type, later_type, reason)``. Applied only where the two actions
#: share a scope — a camera correction in the kitchen has no claim on the
#: living room's furniture.
ORDERING: Tuple[Tuple[str, str, str], ...] = (
    (ActionType.CAMERA_CORRECTION, ActionType.FURNITURE_TRANSLATION,
     "displacement is measured through this camera, so the camera must be "
     "right before the furniture is judged against it"),
    (ActionType.CAMERA_CORRECTION, ActionType.FURNITURE_ROTATION,
     "orientation is judged from the same projection as displacement"),
    (ActionType.CAMERA_CORRECTION, ActionType.FURNITURE_SCALE,
     "proportions are compared against a detection box projected through "
     "this camera"),

    (ActionType.STYLE_REFINEMENT, ActionType.MATERIAL_ADJUSTMENT,
     "styles.resolve_material re-derives a room's materials from its style, "
     "which would overwrite a material tuned first"),
    (ActionType.STYLE_REFINEMENT, ActionType.PALETTE_ADJUSTMENT,
     "a style supplies the fallback palette, so it settles before the palette "
     "is nudged"),
    (ActionType.STYLE_REFINEMENT, ActionType.ASSET_REPLACEMENT,
     "assets.match_asset scores candidates against the room's style"),

    (ActionType.PALETTE_ADJUSTMENT, ActionType.MATERIAL_ADJUSTMENT,
     "MaterialLibrary.surface tints a finish against the room palette, so a "
     "palette change re-tints a material adjusted first"),

    (ActionType.ASSET_REPLACEMENT, ActionType.FURNITURE_SCALE,
     "a new asset brings its own proportions, so rescaling to the old one's "
     "would be discarded"),
    (ActionType.DECOR_DENSITY, ActionType.FURNITURE_TRANSLATION,
     "admitting withheld objects changes what the room contains, and "
     "collision resolution may move the existing furniture in response"),
)

#: Pairs that must never both run, and how to explain dropping one.
EXCLUSIVE: Tuple[Tuple[str, str, str], ...] = (
    (ActionType.ASSET_REPLACEMENT, ActionType.ASSET_VARIANT_SWAP,
     "both replace the same object's asset; only the better-ranked one is "
     "tried, and the other stays available for the next plan"),
    (ActionType.ASSET_REPLACEMENT, ActionType.FURNITURE_SCALE,
     "a poor asset match is either the wrong asset or the wrong proportions; "
     "doing both at once makes the result unattributable"),
    (ActionType.ASSET_VARIANT_SWAP, ActionType.FURNITURE_SCALE,
     "as above — one hypothesis at a time"),
)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _shares_scope(earlier: Action, later: Action) -> bool:
    """Whether an ordering rule between these two types actually applies.

    Rules are stated between *types*; they bind only where the actions touch
    the same thing. Without this every camera correction in the building would
    gate every translation in it, and a plan that could have run in any order
    would serialise into a queue.
    """
    if set(earlier.objects) & set(later.objects):
        return True
    if set(earlier.materials) & set(later.materials):
        return True
    return bool(set(earlier.rooms) & set(later.rooms))


def _same_target(first: Action, second: Action) -> bool:
    return first.target == second.target or bool(
        set(first.objects) & set(second.objects)
    )


# ---------------------------------------------------------------------------
# Building the graph
# ---------------------------------------------------------------------------


def build(actions: Sequence[Action]) -> ActionGraph:
    """Assemble the dependency graph and resolve every contradiction.

    Order of operations matters: exclusions are resolved *before* edges are
    drawn, so no dependency is recorded against an action that will not run.
    Cycles are broken last, once the surviving set is known.
    """
    graph = ActionGraph()
    for action in actions:
        graph.add(action)

    _apply_exclusions(graph)
    _apply_ordering(graph)
    graph.break_cycles()
    return graph


def _apply_exclusions(graph: ActionGraph) -> None:
    """Drop the lower-ranked half of every contradictory pair."""
    ranked = sorted(graph.actions, key=sort_key)

    for index, first in enumerate(ranked):
        if first.excluded:
            continue
        for second in ranked[index + 1:]:
            if second.excluded:
                continue
            reason = _conflict(first, second)
            if reason is None:
                continue
            # ``ranked`` is in priority order, so ``first`` is the keeper.
            second.excluded = True
            second.excluded_reason = (
                f"superseded by {first.id}: {reason}"
            )
            graph.exclude(first.id, second.id, reason)


def _conflict(first: Action, second: Action) -> "str | None":
    """Why these two must not both run, or ``None`` if they may."""
    if not _same_target(first, second):
        return None

    for left, right, reason in EXCLUSIVE:
        if {first.type, second.type} == {left, right}:
            return reason

    # The same action type twice on one target is always a contradiction:
    # either they say the same thing, in which case one is redundant, or they
    # disagree, in which case running both leaves the target in a state
    # neither finding asked for.
    if first.type == second.type and first.target == second.target:
        return (f"two {first.type} actions target {first.target}; the "
                f"higher-ranked one carries the change")

    return None


def _apply_ordering(graph: ActionGraph) -> None:
    """Draw a must-run-before edge for every rule that binds."""
    active = [a for a in graph.actions if not a.excluded]
    by_type: Dict[str, List[Action]] = {}
    for action in active:
        by_type.setdefault(action.type, []).append(action)

    for earlier_type, later_type, reason in ORDERING:
        for earlier in by_type.get(earlier_type, ()):
            for later in by_type.get(later_type, ()):
                if earlier.id == later.id:
                    continue
                if _shares_scope(earlier, later):
                    graph.link(earlier.id, later.id, reason)


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def blocked_by(graph: ActionGraph, action_id: str, completed: Iterable[str]) -> List[str]:
    """Which predecessors of ``action_id`` have not run yet.

    The optimiser calls this to decide what is ready. A predecessor that was
    *attempted* counts as completed even if it was rolled back: the question
    the rule answers is "has this been settled", and a rejected camera
    correction has settled the camera as firmly as an accepted one.
    """
    done = set(completed)
    return [pred for pred in graph.predecessors(action_id) if pred not in done]


def ready(graph: ActionGraph, completed: Iterable[str],
          attempted: Iterable[str] = ()) -> List[Action]:
    """Actions whose dependencies are satisfied, best first."""
    done = set(completed)
    seen = set(attempted)
    out = [
        action for action in graph.active()
        if action.id not in seen and not blocked_by(graph, action.id, done)
    ]
    return sorted(out, key=sort_key)


def describe(graph: ActionGraph) -> List[str]:
    """Plain-language lines for the report."""
    lines: List[str] = []
    for before in sorted(graph.edges):
        for after in sorted(graph.edges[before]):
            reason = graph.edge_reasons.get((before, after), "")
            lines.append(f"{before} before {after} — {reason}")
    for kept, dropped, reason in graph.exclusions:
        lines.append(f"{dropped} dropped for {kept} — {reason}")
    for before, after, reason in graph.broken_edges:
        lines.append(f"cycle broken: dropped {before} before {after} ({reason})")
    return lines
