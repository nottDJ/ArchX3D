"""
ArchX3D — Applying an action to a scene graph
=============================================
The only code in the project that writes to a scene graph on the optimiser's
behalf. One handler per action type, each small enough to read in full.

Why this is separate from the optimiser loop
--------------------------------------------
The loop's job is control flow: pick, validate, apply, measure, keep or undo.
Mixing eleven kinds of graph surgery into it would bury that logic, and the
surgery is where the mistakes live — an off-by-one on a colour channel, a
rotation applied in degrees to a radians field. Kept apart, each handler can be
tested against a graph and an expectation, without a render anywhere.

The contract every handler keeps
--------------------------------
* It mutates only what its action declared. A handler that touched an object
  its action never named would defeat the constraint check, which validates
  against those declarations.
* It reports what it changed, field by field. That record is what the history
  file shows and what makes an accepted change auditable.
* It never invents. Every value comes from the action's parameters, which came
  from a measurement. Where a parameter is missing or nonsensical the handler
  changes nothing and says so — a silent no-op would be recorded as a change
  that achieved nothing, which is a different and misleading claim.

Rollback is not here
--------------------
Handlers do not undo themselves. :mod:`optimizer.rollback` restores a whole
snapshot, which is both simpler and safer than eleven inverse operations: an
inverse that drifts from its forward operation is a bug that only shows up
after a rejected action, which is exactly when nobody is looking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple


@dataclass
class MutationResult:
    """What a handler actually did."""

    applied: bool = False
    #: ``[(subject, field, before, after), ...]`` — the audit trail.
    changes: List[Tuple[str, str, Any, Any]] = field(default_factory=list)
    #: Why nothing happened, when nothing did.
    reason: str = ""

    def record(self, subject: str, name: str, before: Any, after: Any) -> None:
        if before != after:
            self.changes.append((subject, name, before, after))
            self.applied = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applied": self.applied,
            "changes": [
                {"subject": s, "field": f, "before": b, "after": a}
                for s, f, b, a in self.changes
            ],
            "reason": self.reason,
        }

    def summary(self) -> str:
        if not self.applied:
            return self.reason or "no change"
        return "; ".join(
            f"{subject}.{name}: {_short(before)} -> {_short(after)}"
            for subject, name, before, after in self.changes[:6]
        )


def _short(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_short(v) for v in value[:3]) + "]"
    return str(value)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def apply(action, graph) -> MutationResult:
    """Apply one action. Returns what changed, or why nothing did."""
    from planner.action_graph import ActionType

    handlers: Dict[str, Callable[[Any, Any], MutationResult]] = {
        ActionType.LIGHTING_ADJUSTMENT: _lighting,
        ActionType.MATERIAL_ADJUSTMENT: _material,
        ActionType.PALETTE_ADJUSTMENT: _palette,
        ActionType.FURNITURE_TRANSLATION: _translate,
        ActionType.FURNITURE_ROTATION: _rotate,
        ActionType.FURNITURE_SCALE: _rescale,
        ActionType.ASSET_REPLACEMENT: _asset,
        ActionType.ASSET_VARIANT_SWAP: _asset,
        ActionType.DECOR_DENSITY: _decor,
        ActionType.CAMERA_CORRECTION: _camera,
        ActionType.STYLE_REFINEMENT: _style,
    }
    handler = handlers.get(action.type)
    if handler is None:
        return MutationResult(reason=f"no handler for action type {action.type!r}")
    return handler(action, graph)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


def _lighting(action, graph) -> MutationResult:
    """Set a room's LightingEnvironment fields to the planned absolutes."""
    result = MutationResult()
    room = graph.room_by_id(action.target_id)
    if room is None or room.lighting is None:
        return MutationResult(reason=f"{action.target_id} has no lighting environment")

    environment = room.lighting
    for name, low, high in (("ambient", 0.0, 1.0),
                            ("color_temperature_k", 1500.0, 12000.0),
                            ("shadow_softness", 0.0, 1.0),
                            ("window_contribution", 0.0, 1.0)):
        if name not in action.parameters:
            continue
        before = getattr(environment, name)
        after = _clamp(float(action.parameters[name]), low, high)
        setattr(environment, name, after)
        result.record(room.id, f"lighting.{name}", round(before, 4), round(after, 4))

    if "daylight_direction" in action.parameters:
        before = environment.daylight_direction
        after = float(action.parameters["daylight_direction"]) % 360.0
        environment.daylight_direction = after
        result.record(room.id, "lighting.daylight_direction",
                      round(before, 2), round(after, 2))

    # The environment is no longer what the vision pass observed, and saying so
    # keeps a later evaluation from citing it as evidence of anything.
    if result.applied and environment.source != "optimised":
        result.record(room.id, "lighting.source", environment.source, "optimised")
        environment.source = "optimised"

    if not result.applied:
        result.reason = "the planned values matched the current ones"
    return result


# ---------------------------------------------------------------------------
# Materials and palette
# ---------------------------------------------------------------------------


def _material(action, graph) -> MutationResult:
    """Rescale a material's saturation and/or substitute its species."""
    from blender import colour as colour_mod

    result = MutationResult()
    scale = action.parameters.get("saturation_scale")
    species = action.parameters.get("species")
    tint = action.parameters.get("tint_toward")
    blend = float(action.parameters.get("tint_blend", 0.0) or 0.0)
    surfaces = action.parameters.get("surfaces") or []
    object_ids = action.parameters.get("objects") or []

    def recolour(current: str) -> str:
        """Saturation first, then tint. Order matters: tinting toward a
        reference colour already carries that colour's saturation, so scaling
        afterwards would undo half of what the blend just did."""
        out = current
        if scale:
            out = _scale_saturation(colour_mod, out, float(scale))
        if tint and blend > 0:
            out = colour_mod.mix(out, str(tint), blend)
        return out

    rooms = [graph.room_by_id(room_id) for room_id in action.rooms]
    for room in [r for r in rooms if r is not None]:
        for label in surfaces:
            finish = {"wall": room.wall_finish, "floor": room.floor_finish,
                      "ceiling": room.ceiling_finish}.get(label)
            if finish is None:
                continue
            before = finish.color_hex
            finish.color_hex = recolour(before)
            result.record(f"{room.id}.{label}", "finish.color_hex",
                          before, finish.color_hex)
            if species:
                before = finish.material
                finish.material = species
                result.record(f"{room.id}.{label}", "finish.material",
                              before, species)

    for object_id in object_ids:
        obj = graph.object_by_id(object_id)
        if obj is None:
            continue
        before = obj.color_hex
        obj.color_hex = recolour(before)
        result.record(object_id, "color_hex", before, obj.color_hex)
        if species:
            before = obj.material
            obj.material = species
            result.record(object_id, "material", before, species)

    if not result.applied:
        result.reason = "no surface or object carried the targeted material"
    return result


def _scale_saturation(colour_mod, color_hex: str, scale: float) -> str:
    """Multiply a colour's saturation, holding hue and lightness.

    In HLS because that is the space where "more saturated" means what a
    person means by it. Doing it in RGB would change the hue as a side effect,
    and a finding about intensity would come back as a finding about colour.
    """
    hue, lightness, saturation = colour_mod.hls(color_hex)
    return colour_mod.from_hls(hue, lightness, max(0.0, min(1.0, saturation * scale)))


def _palette(action, graph) -> MutationResult:
    """Replace a room's palette roles with the planned colours."""
    result = MutationResult()
    room = graph.room_by_id(action.target_id)
    if room is None or room.palette is None:
        return MutationResult(reason=f"{action.target_id} has no palette")

    roles = action.parameters.get("roles") or {}
    for role, value in sorted(roles.items()):
        if not hasattr(room.palette, role):
            continue
        before = getattr(room.palette, role)
        setattr(room.palette, role, value)
        result.record(room.id, f"palette.{role}", before, value)

    if result.applied and room.palette.source != "optimised":
        result.record(room.id, "palette.source", room.palette.source, "optimised")
        room.palette.source = "optimised"
    if not result.applied:
        result.reason = "the planned roles matched the current ones"
    return result


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


def _translate(action, graph) -> MutationResult:
    result = MutationResult()
    obj = graph.object_by_id(action.target_id)
    if obj is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    dx = float(action.parameters.get("dx", 0.0))
    dy = float(action.parameters.get("dy", 0.0))
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return MutationResult(reason="the planned move is zero")

    before = (round(obj.position.x, 4), round(obj.position.y, 4))
    obj.position.x += dx
    obj.position.y += dy
    result.record(obj.id, "position",
                  list(before), [round(obj.position.x, 4), round(obj.position.y, 4)])
    _note(obj, f"optimiser moved by ({dx:+.3f}, {dy:+.3f}) m")
    return result


def _rotate(action, graph) -> MutationResult:
    result = MutationResult()
    obj = graph.object_by_id(action.target_id)
    if obj is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    if "rotation_z" in action.parameters:
        after = float(action.parameters["rotation_z"]) % 360.0
    elif "delta_deg" in action.parameters:
        after = (obj.rotation_z + float(action.parameters["delta_deg"])) % 360.0
    else:
        return MutationResult(reason="no rotation given")

    before = obj.rotation_z
    obj.rotation_z = after
    result.record(obj.id, "rotation_z", round(before, 2), round(after, 2))
    _note(obj, f"optimiser rotated to {after:.0f}°")

    # The relationship that prompted this is now honoured; recording it keeps
    # a later plan from proposing the same rotation again.
    toward = action.parameters.get("toward")
    for relationship in getattr(graph, "relationships", []) or []:
        if relationship.subject == obj.id and relationship.object == toward:
            if not relationship.satisfied:
                relationship.satisfied = True
                result.record(obj.id, f"relationship[{relationship.predicate}]",
                              False, True)
    return result


def _rescale(action, graph) -> MutationResult:
    result = MutationResult()
    obj = graph.object_by_id(action.target_id)
    if obj is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    for name in ("width", "depth", "height"):
        if name not in action.parameters:
            continue
        before = getattr(obj.dimensions, name)
        after = max(0.01, float(action.parameters[name]))
        setattr(obj.dimensions, name, after)
        result.record(obj.id, f"dimensions.{name}", round(before, 4), round(after, 4))

    if result.applied:
        _note(obj, "optimiser corrected proportions toward the matched asset")
    else:
        result.reason = "the planned dimensions matched the current ones"
    return result


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def _asset(action, graph) -> MutationResult:
    """Swap an object's asset, and rescore the match honestly.

    The score is recomputed rather than copied from the action: the action's
    estimate was made against the object as it was, and if an earlier action
    in the same run changed its dimensions the stored score would be a claim
    about a shape that no longer exists.
    """
    from vision import assets

    result = MutationResult()
    obj = graph.object_by_id(action.target_id)
    if obj is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    asset_key = str(action.parameters.get("asset") or "")
    if not asset_key or asset_key == obj.asset:
        return MutationResult(reason="the planned asset is already in use")

    room = graph.room_by_id(obj.room_id)
    style = getattr(room, "style", "") or ""
    match = assets.match_asset(
        obj.category,
        (obj.dimensions.width, obj.dimensions.depth, obj.dimensions.height),
        style, obj.material, obj.color_hex, obj.label,
    )
    score = match.score if (match.variant and match.variant.key == asset_key) else \
        _score_for(assets, obj, asset_key, style)

    before_asset, before_score = obj.asset, obj.asset_score
    obj.asset = asset_key
    obj.asset_score = round(float(score), 4)
    result.record(obj.id, "asset", before_asset, asset_key)
    result.record(obj.id, "asset_score", round(before_score, 4), obj.asset_score)
    _note(obj, f"optimiser rebuilt from {asset_key}")
    return result


def _score_for(assets, obj, asset_key: str, style: str) -> float:
    """Score one named variant against an object, without picking a winner.

    ``match_asset`` returns only its best candidate, so a deliberate swap to a
    second choice needs its own number — otherwise the graph would record the
    winner's score against the loser's key.
    """
    variant = assets.get_variant(asset_key)
    if variant is None:
        return 0.0
    signature = variant.signature
    dimensions = (obj.dimensions.width, obj.dimensions.depth, obj.dimensions.height)
    return round(assets._proportion_score(dimensions, signature), 4)


# ---------------------------------------------------------------------------
# Decor density
# ---------------------------------------------------------------------------


def _decor(action, graph) -> MutationResult:
    """Admit withheld detections, or withhold admitted ones.

    Nothing is created or destroyed. The objects exist in the graph either
    way; ``uncertain`` decides whether the generator builds them, and this
    flips exactly that flag.
    """
    result = MutationResult()

    for object_id in action.parameters.get("admit", []) or []:
        obj = graph.object_by_id(object_id)
        if obj is None or not obj.uncertain:
            continue
        obj.uncertain = False
        result.record(object_id, "uncertain", True, False)
        _note(obj, "optimiser admitted this withheld detection")

    for object_id in action.parameters.get("withhold", []) or []:
        obj = graph.object_by_id(object_id)
        if obj is None or obj.uncertain:
            continue
        obj.uncertain = True
        result.record(object_id, "uncertain", False, True)
        _note(obj, "optimiser withheld this detection")

    if not result.applied:
        result.reason = "every named object was already in the requested state"
    return result


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


def _camera(action, graph) -> MutationResult:
    """Move a stored ViewPoint. The only action that needs no rebuild."""
    result = MutationResult()
    viewpoint = next((v for v in getattr(graph, "viewpoints", []) or []
                      if v.image_id == action.target_id), None)
    if viewpoint is None:
        return MutationResult(reason=f"no viewpoint {action.target_id!r}")

    dx = float(action.parameters.get("dx", 0.0))
    dy = float(action.parameters.get("dy", 0.0))
    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
        before = (round(viewpoint.position.x, 4), round(viewpoint.position.y, 4))
        viewpoint.position.x += dx
        viewpoint.position.y += dy
        result.record(viewpoint.image_id, "position", list(before),
                      [round(viewpoint.position.x, 4), round(viewpoint.position.y, 4)])

    for name in ("yaw", "pitch_deg", "vertical_fov_deg"):
        if name not in action.parameters:
            continue
        before = getattr(viewpoint, name)
        after = float(action.parameters[name])
        setattr(viewpoint, name, after)
        result.record(viewpoint.image_id, name, round(before, 3), round(after, 3))

    if not result.applied:
        result.reason = "the planned correction is zero"
    return result


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def _style(action, graph) -> MutationResult:
    """Set a room's style, which re-derives its materials downstream.

    Only the label changes here. The consequences — different material
    substitutions, different asset scoring — happen in the generator, which is
    where that logic lives and where it stays.
    """
    from vision import catalog

    result = MutationResult()
    room = graph.room_by_id(action.target_id)
    if room is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    style = catalog.normalise_style(str(action.parameters.get("style") or ""))
    if not style or style == room.style:
        return MutationResult(reason="the planned style is already set")

    before = room.style
    room.style = style
    result.record(room.id, "style", before, style)

    # Adopted from elsewhere in the building, not observed here — and the
    # confidence has to say so, because asset matching weights it.
    confidence = float(action.parameters.get("style_confidence", 0.5) or 0.5)
    before_confidence = room.style_confidence
    room.style_confidence = min(0.7, confidence)
    result.record(room.id, "style_confidence",
                  round(before_confidence, 3), round(room.style_confidence, 3))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _note(obj, message: str) -> None:
    """Leave a trace on the object itself.

    The history file records what happened, but the graph outlives the run and
    someone reading it later deserves to know which values a person observed
    and which an optimiser chose.
    """
    flags = getattr(obj, "flags", None)
    if flags is None:
        return
    if message not in flags:
        flags.append(message)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
