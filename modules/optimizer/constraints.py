"""
ArchX3D — What the optimiser may never do
=========================================
The guardrails. Checked twice per action: before it is applied, against the
action's declared intent, and after, against the graph it produced.

Why twice
---------
A pre-check catches an action that *asks* for something forbidden — moving a
locked object, retargeting a wall. A post-check catches an action that asked
for something permitted and produced something forbidden anyway: a translation
within its stated limit that nevertheless carried a sofa through a wall, a
material substitution that resolved to a species outside the taxonomy.

Only the second kind is dangerous, and only the second kind is impossible to
anticipate from the action alone. Both run, and a failure of either rolls the
change back before it can be rendered — a violated constraint must never reach
a preview, because a preview that looks better while breaking a rule is the
worst possible outcome: it gets accepted.

The two classes of rule
-----------------------
**Immutable** — things the optimiser has no business touching at all. DXF
geometry, walls, doors, windows, and any object the user locked. These are not
"try not to"; an action naming one is rejected outright.

**Invariants** — properties the graph must still have afterwards. Materials
must exist in the taxonomy, a room's materials must remain consistent with its
style, and an object must stay inside the room it belongs to.

Both are enumerated rather than inferred. A guardrail that depends on a
heuristic is a guardrail that fails quietly on the case nobody imagined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# What may never be touched
# ---------------------------------------------------------------------------

#: Target kinds no action may name. The DXF is the single source of truth for
#: shape and the optimiser is downstream of it in every sense.
IMMUTABLE_TARGETS: Tuple[str, ...] = ("wall", "opening", "door", "window",
                                      "geometry", "architecture")

#: Scene-graph collections nothing may add to, remove from or edit.
IMMUTABLE_COLLECTIONS: Tuple[str, ...] = ("walls", "openings", "architecture")

#: Fields of a SceneObject an action may change. Anything else — id, room_id,
#: support, source_images — is provenance or structure, and an optimiser
#: rewriting provenance would be falsifying the record it is judged against.
MUTABLE_OBJECT_FIELDS: Tuple[str, ...] = (
    "position", "rotation_z", "dimensions", "scale", "asset", "asset_score",
    "material", "color_hex", "uncertain",
)

#: How far outside its room's bounds an object may sit before it counts as
#: escaped. Rooms are bounding boxes around segmented polygons, so a small
#: overhang is normal — a sofa against a wall legitimately overlaps it.
CONTAINMENT_TOLERANCE = 0.35


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """One broken rule, in terms specific enough to fix."""

    rule: str
    detail: str
    #: ``immutable`` or ``invariant`` — whether the action was forbidden or
    #: its result was.
    kind: str = "invariant"
    subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "detail": self.detail, "kind": self.kind,
                "subject": self.subject}

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass
class ConstraintReport:
    """Everything that failed, or nothing."""

    violations: List[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, rule: str, detail: str, kind: str = "invariant",
            subject: str = "") -> None:
        self.violations.append(Violation(rule, detail, kind, subject))

    def reason(self) -> str:
        if self.ok:
            return ""
        if len(self.violations) == 1:
            return str(self.violations[0])
        return f"{len(self.violations)} constraints violated: " + "; ".join(
            str(v) for v in self.violations[:3]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "violations": [v.to_dict() for v in self.violations]}


# ---------------------------------------------------------------------------
# Pre-checks: is this action allowed to run at all?
# ---------------------------------------------------------------------------


def check_action(action, graph) -> ConstraintReport:
    """Whether an action may be applied, judged from the action and the graph.

    Runs before any mutation. Everything here is knowable in advance, which is
    what makes rejection cheap: a forbidden action costs no render.
    """
    report = ConstraintReport()

    if action.target_kind in IMMUTABLE_TARGETS:
        report.add("immutable_target",
                   f"{action.type} targets {action.target}, which is derived "
                   f"from the DXF and may not be modified",
                   kind="immutable", subject=action.target)

    for object_id in action.objects:
        obj = graph.object_by_id(object_id) if graph else None
        if obj is None:
            report.add("unknown_object",
                       f"{object_id} is not in the scene graph",
                       subject=object_id)
            continue
        if getattr(obj, "locked", False):
            report.add("locked_object",
                       f"{object_id} was locked by the user, which is a "
                       f"statement of ground truth the optimiser may not "
                       f"overrule",
                       kind="immutable", subject=object_id)

    for room_id in action.rooms:
        if graph is not None and room_id and graph.room_by_id(room_id) is None:
            report.add("unknown_room", f"{room_id} is not in the scene graph",
                       subject=room_id)

    _check_parameters(action, graph, report)
    return report


def _check_parameters(action, graph, report: ConstraintReport) -> None:
    """Per-type parameter validation, before anything is applied."""
    from planner.action_graph import ActionType

    parameters = action.parameters or {}

    if action.type == ActionType.MATERIAL_ADJUSTMENT:
        species = parameters.get("species")
        if species and not _in_taxonomy(species):
            report.add("material_taxonomy",
                       f"{species!r} is not a material in the catalog",
                       subject=species)

    elif action.type == ActionType.STYLE_REFINEMENT:
        style = parameters.get("style")
        if style and not _known_style(style):
            report.add("style_taxonomy",
                       f"{style!r} is not a style in the catalog", subject=style)

    elif action.type in (ActionType.ASSET_REPLACEMENT, ActionType.ASSET_VARIANT_SWAP):
        asset = parameters.get("asset")
        obj = graph.object_by_id(action.objects[0]) if (graph and action.objects) else None
        if asset and obj is not None and not _asset_fits(asset, obj.category):
            report.add("asset_category",
                       f"asset {asset!r} does not belong to the "
                       f"{obj.category!r} category",
                       subject=asset)

    elif action.type == ActionType.DECOR_DENSITY:
        for object_id in parameters.get("admit", []) or []:
            obj = graph.object_by_id(object_id) if graph else None
            if obj is None:
                report.add("unknown_object",
                           f"cannot admit {object_id}: not in the graph",
                           subject=object_id)
            elif obj.dimensions.is_degenerate():
                report.add("degenerate_dimensions",
                           f"cannot admit {object_id}: it has a zero extent "
                           f"and nothing would be built",
                           subject=object_id)


# ---------------------------------------------------------------------------
# Post-checks: is the graph this produced still legal?
# ---------------------------------------------------------------------------


def check_graph(graph, baseline: Optional[Dict[str, Any]] = None,
                scope: Optional[Iterable[str]] = None) -> ConstraintReport:
    """Whether a mutated graph still satisfies every invariant.

    ``baseline`` is a snapshot taken before the mutation — used to prove that
    nothing immutable changed, which cannot be checked from the current state
    alone. ``scope`` narrows the object checks to the rooms an action touched,
    because re-validating a whole building after moving one chair is waste.
    """
    report = ConstraintReport()
    if graph is None:
        return report

    if baseline is not None:
        _check_immutables(graph, baseline, report)

    rooms = set(scope) if scope else None
    _check_taxonomy(graph, rooms, report)
    _check_style_consistency(graph, rooms, report)
    _check_containment(graph, rooms, report)
    return report


def immutable_snapshot(graph) -> Dict[str, Any]:
    """A digest of everything that must not change.

    Cheap and comparable: the point is to detect a change, not to describe it,
    so a hash per collection is enough and keeps the snapshot small enough to
    take on every iteration.
    """
    from render.cache import digest

    if graph is None:
        return {}
    snapshot = {
        collection: digest([
            record.to_dict() for record in
            sorted(getattr(graph, collection, []) or [],
                   key=lambda r: getattr(r, "id", ""))
        ])
        for collection in IMMUTABLE_COLLECTIONS
    }
    snapshot["locked_objects"] = digest([
        obj.to_dict() for obj in
        sorted((o for o in graph.objects if getattr(o, "locked", False)),
               key=lambda o: o.id)
    ])
    return snapshot


def _check_immutables(graph, baseline: Dict[str, Any],
                      report: ConstraintReport) -> None:
    current = immutable_snapshot(graph)
    labels = {
        "walls": "wall geometry",
        "openings": "doors and windows",
        "architecture": "structural elements",
        "locked_objects": "objects the user locked",
    }
    for key, before in baseline.items():
        if current.get(key) != before:
            report.add("immutable_modified",
                       f"{labels.get(key, key)} changed; the DXF and the "
                       f"user's locks are outside the optimiser's authority",
                       kind="immutable", subject=key)


def _check_taxonomy(graph, rooms: Optional[set], report: ConstraintReport) -> None:
    """Every material named anywhere must exist in the catalog."""
    for obj in graph.objects:
        if rooms is not None and obj.room_id not in rooms:
            continue
        if obj.material and not _in_taxonomy(obj.material):
            report.add("material_taxonomy",
                       f"{obj.id} claims material {obj.material!r}, which is "
                       f"not in the catalog",
                       subject=obj.id)

    for room in graph.rooms:
        if rooms is not None and room.id not in rooms:
            continue
        for label, finish in (("wall", room.wall_finish), ("floor", room.floor_finish),
                              ("ceiling", room.ceiling_finish)):
            if finish is None or not finish.material:
                continue
            if not _in_taxonomy(finish.material):
                report.add("material_taxonomy",
                           f"{room.id}'s {label} finish claims "
                           f"{finish.material!r}, which is not in the catalog",
                           subject=room.id)
            elif not _applies_to(finish.material, label):
                report.add("material_surface",
                           f"{finish.material!r} is not a {label} material",
                           subject=room.id)


def _check_style_consistency(graph, rooms: Optional[set],
                             report: ConstraintReport) -> None:
    """A room's style must be a known one, and its finishes plausible for it.

    "Plausible" is deliberately weak: the style vocabulary biases material
    choice rather than restricting it, and a modern room with an exposed brick
    wall is a real room. What this catches is a style label the rest of the
    pipeline cannot resolve, which would silently fall back to defaults and
    undo whatever the action was trying to achieve.
    """
    for room in graph.rooms:
        if rooms is not None and room.id not in rooms:
            continue
        if room.style and room.style != "unknown" and not _known_style(room.style):
            report.add("style_taxonomy",
                       f"{room.id} claims style {room.style!r}, which the "
                       f"catalog cannot resolve",
                       subject=room.id)


def _check_containment(graph, rooms: Optional[set],
                       report: ConstraintReport) -> None:
    """Objects must stay in the room they belong to.

    Checked against the room's bounds with a tolerance, because a room's
    bounding box is drawn around a segmented polygon and furniture against a
    wall legitimately sits on the line. What this catches is an object that
    has left — a translation that overshot into the corridor.
    """
    for obj in graph.objects:
        if not obj.room_id:
            continue
        if rooms is not None and obj.room_id not in rooms:
            continue
        room = graph.room_by_id(obj.room_id)
        if room is None:
            continue
        low_x, low_y = room.bounds_min
        high_x, high_y = room.bounds_max
        if high_x - low_x <= 0 or high_y - low_y <= 0:
            continue  # a room with no measured bounds cannot contain anything

        x, y = obj.position.x, obj.position.y
        if not (low_x - CONTAINMENT_TOLERANCE <= x <= high_x + CONTAINMENT_TOLERANCE
                and low_y - CONTAINMENT_TOLERANCE <= y <= high_y + CONTAINMENT_TOLERANCE):
            report.add("room_containment",
                       f"{obj.id} sits at ({x:.2f}, {y:.2f}), outside "
                       f"{room.id}'s bounds "
                       f"({low_x:.2f}, {low_y:.2f})-({high_x:.2f}, {high_y:.2f})",
                       subject=obj.id)


# ---------------------------------------------------------------------------
# Taxonomy lookups
# ---------------------------------------------------------------------------


def _in_taxonomy(material: str) -> bool:
    from vision import catalog

    return material in catalog.MATERIALS


def _applies_to(material: str, surface: str) -> bool:
    from vision import catalog

    prior = catalog.MATERIALS.get(material)
    return prior is None or surface in prior.applies_to


def _known_style(style: str) -> bool:
    """Whether the catalog can actually resolve this style.

    ``normalise_style`` answers "unknown" for anything it does not recognise,
    and "unknown" is itself a legitimate entry — a room whose style was never
    established. So membership alone would accept every string ever written.
    What matters is whether a *named* style resolved to something usable.
    """
    from vision import catalog

    raw = (style or "").strip().lower()
    if raw in ("", "unknown"):
        return True                      # no claim is not a false claim
    return catalog.normalise_style(style) != "unknown"


def _asset_fits(asset_key: str, category: str) -> bool:
    from vision import assets

    return any(variant.key == asset_key for variant in assets.variants_for(category))
