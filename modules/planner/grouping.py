"""
ArchX3D — Root causes, and the actions that address them
========================================================
Turns findings into candidate actions by asking, of every finding, *what one
change would answer this* — and then noticing when several findings want the
same change.

The problem this solves
-----------------------
An evaluation of one badly lit room produces three findings: the render is
darker than the reference, its light is warmer, and its shadows are flatter.
Handed to an optimiser individually they become three edits to the same
``LightingEnvironment``, each applied and measured against the last, each
partially undoing the previous one's effect on the others. Three iterations to
reach a result one iteration could have produced, and a history that reads as
if the optimiser were arguing with itself.

Grouped, they are one ``LightingAdjustment`` carrying three parameter changes
and three trigger findings — the spec's example, and the reason this module
exists.

How grouping works
------------------
Findings share a root cause when they name the same **subsystem** and the same
**scope** — a room, an object, or a material. That is a deliberately blunt
rule, and it is blunt in the safe direction: two findings wrongly grouped
become one action that does slightly too much and gets rolled back if it does
not help, while two wrongly separated become an extra iteration.

Synthesis reads the scene graph
-------------------------------
An action has to say "set ambient to 0.62", not "raise ambient", or the
optimiser would need judgement at the moment of mutation. Computing the
absolute value needs the current one, so synthesis is given the scene graph —
**read-only**. Nothing in the planner mutates anything; that is the
optimiser's job and its exclusive one.

Bounded moves
-------------
Every derived parameter is clamped, and most are damped: a measurement is
evidence about the direction to move, not a solved equation. Jumping the whole
way to what a single reading implies overshoots — a photograph's brightness
includes its exposure, its colour includes its white balance — and the loop
then spends its next iteration coming back. Move most of the way, re-measure,
move again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .action_graph import Action, ActionType
from .findings import FindingSet, PlannedFinding

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

#: How far to move toward what a measurement implies. Below 1.0 because a
#: single reading conflates the thing being measured with the conditions it
#: was measured under; the loop re-measures, so under-shooting converges while
#: over-shooting oscillates.
DAMPING = 0.75

#: Bounds on one lighting step. Ambient is a 0-1 level, so a 2x swing is
#: already drastic; letting one iteration do more would make the result
#: unattributable to the finding that prompted it.
AMBIENT_SCALE_LIMITS = (0.4, 2.5)
#: Kelvin is perceptually compressed, so a large-looking number is a modest
#: shift. 1500 K is roughly "warm white to neutral".
KELVIN_STEP_LIMIT = 1500.0
SHADOW_STEP_LIMIT = 0.35

#: Bounds on one material step, as a multiplier on saturation.
SATURATION_SCALE_LIMITS = (0.35, 3.0)

#: Furthest one translation may move an object, metres. Beyond this the
#: back-projection that produced the number is not trustworthy enough to act
#: on in a single step.
TRANSLATION_LIMIT = 3.0

#: Furthest one camera correction may move a viewpoint, metres.
CAMERA_LIMIT = 4.0

#: How far a palette role may be blended toward the reference in one step. The
#: reference's colour includes its lighting, so the target is a direction, not
#: a destination.
PALETTE_BLEND = 0.35

#: Below this the graph's own footprint and the asset's proportions disagree
#: enough to be worth rescaling rather than re-assetting.
SCALE_DISAGREEMENT = 0.15


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@dataclass
class FindingGroup:
    """Findings that share one root cause, and the action type that fixes it."""

    subsystem: str
    scope: str
    action_type: str
    findings: List[PlannedFinding] = field(default_factory=list)

    @property
    def weight(self) -> float:
        """How much this group argues for its action.

        The sum, not the max: three moderate findings about one room's light
        are better evidence than one, and a group's whole point is that they
        corroborate. Capped at 1 because expected gain is a fraction of a
        similarity score.
        """
        return min(1.0, sum(f.weight for f in self.findings))

    @property
    def confidence(self) -> float:
        return (sum(f.confidence for f in self.findings) / len(self.findings)
                if self.findings else 0.0)

    @property
    def room(self) -> str:
        for finding in self.findings:
            if finding.room:
                return finding.room
        return ""

    @property
    def axes(self) -> List[str]:
        return sorted({f.axis for f in self.findings})

    @property
    def objects(self) -> List[str]:
        return sorted({o for f in self.findings for o in f.objects})

    @property
    def materials(self) -> List[str]:
        return sorted({m for f in self.findings for m in f.materials})

    def evidence(self, *keys: str) -> Optional[Any]:
        """First value found under any of ``keys`` across the group's evidence.

        Findings in a group are about the same thing measured differently, so
        the first reading of a given quantity is the one to use — and asking
        for several aliases keeps synthesis working when an axis renames a key.
        """
        for finding in self.findings:
            for key in keys:
                if key in finding.evidence:
                    return finding.evidence[key]
        return None

    def find(self, *needles: str) -> Optional[PlannedFinding]:
        """The first finding whose summary mentions any of ``needles``."""
        for finding in self.findings:
            lowered = finding.summary.lower()
            if any(needle in lowered for needle in needles):
                return finding
        return None


#: Which action answers which subsystem. The map is the planner's entire theory
#: of "what to do about this", and keeping it a table rather than a chain of
#: conditionals is what makes it reviewable.
SUBSYSTEM_ACTIONS: Dict[str, str] = {
    "LightingEnvironment": ActionType.LIGHTING_ADJUSTMENT,
    "LightSource": ActionType.LIGHTING_ADJUSTMENT,
    "ColourPalette": ActionType.PALETTE_ADJUSTMENT,
    "SurfaceFinish": ActionType.MATERIAL_ADJUSTMENT,
    "MaterialSpecies": ActionType.MATERIAL_ADJUSTMENT,
    "SceneGraphTransform": ActionType.FURNITURE_TRANSLATION,
    "AssetPlacement": ActionType.ASSET_REPLACEMENT,
    "ObjectDetection": ActionType.DECOR_DENSITY,
    "CameraFit": ActionType.CAMERA_CORRECTION,
}

#: Subsystems nothing may act on. Geometry is the DXF's, and the render
#: settings are the evaluation instrument — an optimiser that tuned its own
#: measuring device to raise its score would be doing the opposite of its job.
UNACTIONABLE: Dict[str, str] = {
    "Geometry": "DXF geometry is immutable",
    "RenderSettings": "the optimiser must not tune the instrument that scores it",
}


def group(finding_set: FindingSet) -> List[FindingGroup]:
    """Cluster findings by root cause, in a deterministic order."""
    grouped: Dict[Tuple[str, str], FindingGroup] = {}

    for finding in finding_set:
        if finding.subsystem in UNACTIONABLE:
            continue
        action_type = SUBSYSTEM_ACTIONS.get(finding.subsystem)
        if action_type is None:
            continue
        key = (finding.subsystem, finding.scope)
        if key not in grouped:
            grouped[key] = FindingGroup(subsystem=finding.subsystem,
                                        scope=finding.scope,
                                        action_type=action_type)
        grouped[key].findings.append(finding)

    return [grouped[key] for key in sorted(grouped)]


def unactionable(finding_set: FindingSet) -> List[Tuple[PlannedFinding, str]]:
    """Findings deliberately left alone, with the reason. For the report.

    A plan that silently omits half the findings looks like a plan that solved
    them. Naming what will not be acted on, and why, is the difference.
    """
    out: List[Tuple[PlannedFinding, str]] = []
    for finding in finding_set:
        if finding.subsystem in UNACTIONABLE:
            out.append((finding, UNACTIONABLE[finding.subsystem]))
        elif finding.subsystem not in SUBSYSTEM_ACTIONS:
            out.append((finding, f"no action type answers {finding.subsystem!r}"))
    return out


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def synthesise(group_: FindingGroup, graph) -> List[Action]:
    """Turn one root cause into the action(s) that would address it.

    Usually one. Occasionally two mutually exclusive candidates — a stand-in
    asset can be answered by swapping the asset *or* by correcting the
    proportions that made it a poor match, and which is right is not knowable
    in advance. Both are proposed, marked exclusive, and the higher-ranked one
    is tried; the optimiser measures whether it helped.

    Returns an empty list when the graph does not support the change — a room
    with no palette cannot have its palette adjusted, and proposing it anyway
    would produce an action guaranteed to be rejected.
    """
    builders = {
        ActionType.LIGHTING_ADJUSTMENT: _lighting,
        ActionType.MATERIAL_ADJUSTMENT: _material,
        ActionType.PALETTE_ADJUSTMENT: _palette,
        ActionType.FURNITURE_TRANSLATION: _placement,
        ActionType.ASSET_REPLACEMENT: _asset,
        ActionType.DECOR_DENSITY: _decor,
        ActionType.CAMERA_CORRECTION: _camera,
    }
    builder = builders.get(group_.action_type)
    if builder is None:
        return []
    actions = builder(group_, graph) or []
    for action in actions:
        _attach_provenance(action, group_)
    return actions


def _attach_provenance(action: Action, group_: FindingGroup) -> None:
    action.trigger_findings = [f.key for f in group_.findings]
    action.trigger_summaries = [f.summary for f in group_.findings]
    action.confidence = group_.confidence
    action.axes = group_.axes
    if group_.room and group_.room not in action.rooms:
        action.rooms.append(group_.room)
    action.rationale = action.rationale or _rationale(group_)


def _rationale(group_: FindingGroup) -> str:
    if len(group_.findings) == 1:
        return group_.findings[0].why
    heads = "; ".join(f.summary for f in group_.findings[:3])
    return (f"{len(group_.findings)} findings share one cause "
            f"({group_.subsystem} on {group_.scope}): {heads}")


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


def _lighting(group_: FindingGroup, graph) -> List[Action]:
    """One LightingEnvironment update carrying every lighting complaint.

    This is the spec's example: too warm, too dark and wrongly-directed light
    are three symptoms of one environment, and they are answered together.
    """
    room = _room(graph, group_.room)
    if room is None or getattr(room, "lighting", None) is None:
        return []

    environment = room.lighting
    parameters: Dict[str, Any] = {}
    changes: List[str] = []

    exposure = group_.find("darker", "brighter")
    if exposure is not None:
        reference = float(exposure.evidence.get("reference_luminance", 0.0) or 0.0)
        render = float(exposure.evidence.get("render_luminance", 0.0) or 0.0)
        if render > 1e-4 and reference > 1e-4:
            scale = _damp_scale(reference / render)
            scale = _clamp(scale, *AMBIENT_SCALE_LIMITS)
            target = _clamp(environment.ambient * scale, 0.02, 1.0)
            if abs(target - environment.ambient) > 0.01:
                parameters["ambient"] = round(target, 4)
                changes.append(
                    f"ambient {environment.ambient:.2f} -> {target:.2f}"
                )

    warmth = group_.find("warmer", "cooler")
    if warmth is not None:
        difference = float(warmth.evidence.get("warmth_difference", 0.0) or 0.0)
        # Positive difference means the render is warmer than the reference, so
        # the colour temperature must rise (higher kelvin is cooler light).
        delta = _clamp(difference * 6000.0 * DAMPING,
                       -KELVIN_STEP_LIMIT, KELVIN_STEP_LIMIT)
        if abs(delta) >= 50.0:
            target = _clamp(environment.color_temperature_k + delta, 1800.0, 8000.0)
            parameters["color_temperature_k"] = round(target, 1)
            changes.append(
                f"colour temperature {environment.color_temperature_k:.0f}K "
                f"-> {target:.0f}K"
            )

    contrast = group_.find("flatter", "harsher")
    if contrast is not None:
        difference = float(contrast.evidence.get("render_contrast", 0.0) or 0.0) - float(
            contrast.evidence.get("reference_contrast", 0.0) or 0.0
        )
        # A flatter render needs harder shadows, so softness comes down.
        delta = _clamp(-difference * 3.0 * DAMPING,
                       -SHADOW_STEP_LIMIT, SHADOW_STEP_LIMIT)
        if abs(delta) >= 0.03:
            target = _clamp(environment.shadow_softness + delta, 0.0, 1.0)
            parameters["shadow_softness"] = round(target, 3)
            changes.append(
                f"shadow softness {environment.shadow_softness:.2f} -> {target:.2f}"
            )

    if not parameters:
        return []

    return [Action(
        id=f"{ActionType.LIGHTING_ADJUSTMENT}:{room.id}",
        type=ActionType.LIGHTING_ADJUSTMENT,
        target=f"room:{room.id}",
        parameters=parameters,
        summary=f"Adjust {room.id}'s lighting environment: " + ", ".join(changes),
        rooms=[room.id],
    )]


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def _material(group_: FindingGroup, graph) -> List[Action]:
    """Correct a material's saturation, or substitute a grainier species.

    Two distinct complaints reach this: a surface reading the wrong colour
    intensity, and a surface reading too flat. The first is a tint the graph
    can carry; the second is the material *species*, because texture is a
    property of the recipe rather than of the colour.
    """
    material_name = group_.materials[0] if group_.materials else ""
    room = _room(graph, group_.room)

    parameters: Dict[str, Any] = {}
    changes: List[str] = []

    saturation = group_.find("desaturated", "saturated")
    if saturation is not None:
        ratio = float(saturation.evidence.get("ratio", 1.0) or 1.0)
        if ratio > 1e-3 and abs(1.0 - ratio) > 0.05:
            scale = _clamp(_damp_scale(1.0 / ratio), *SATURATION_SCALE_LIMITS)
            parameters["saturation_scale"] = round(scale, 4)
            changes.append(f"saturation x{scale:.2f}")

    # A colour *cast* on a surface is the commonest finding of all, and it is
    # not a saturation problem: the material is the wrong colour, not the
    # wrong intensity of the right one. Blended rather than adopted, for the
    # same reason the palette is — the reference's colour carries its own
    # lighting with it.
    reference_hex = group_.evidence("reference_mean")
    if isinstance(reference_hex, str) and reference_hex.startswith("#"):
        parameters["tint_toward"] = reference_hex
        parameters["tint_blend"] = PALETTE_BLEND
        changes.append(f"tint {int(PALETTE_BLEND * 100)}% toward {reference_hex}")

    texture = group_.find("texture")
    if texture is not None:
        species = _grainier_species(_species_of_material(material_name, graph, room))
        if species:
            parameters["species"] = species
            changes.append(f"species -> {species}")

    if not parameters:
        return []

    # Surface finishes are room-scoped; an object's material is object-scoped.
    # Which one this is decides what the mutation touches, so it is recorded
    # rather than inferred later.
    surfaces = _surfaces_using(material_name, room)
    objects = _objects_using(material_name, graph, room)
    if not surfaces and not objects:
        return []
    parameters["surfaces"] = surfaces
    parameters["objects"] = objects

    scope = material_name or (room.id if room else "building")
    return [Action(
        id=f"{ActionType.MATERIAL_ADJUSTMENT}:{scope}",
        type=ActionType.MATERIAL_ADJUSTMENT,
        target=f"material:{material_name}" if material_name else f"room:{scope}",
        parameters=parameters,
        summary=f"Adjust {material_name or scope}: " + ", ".join(changes),
        rooms=[room.id] if room is not None else [],
        materials=[material_name] if material_name else [],
        objects=objects,
    )]


def _species_of_material(material_name: str, graph, room) -> str:
    """The catalog species a material name refers to.

    Material names in the render come from the Blender library
    (``M_<species>_<hex>``); the graph stores the species itself. Recovering it
    is what lets a substitution stay inside the taxonomy.
    """
    from vision import catalog

    if material_name.startswith("M_"):
        stem = material_name[2:].rsplit("_", 1)[0]
        if stem in catalog.MATERIALS:
            return stem
        normalised = catalog.normalise_material(stem.replace("_", " "))
        if normalised != "unknown":
            return normalised

    if room is not None:
        for finish in (room.floor_finish, room.wall_finish, room.ceiling_finish):
            if finish is not None and finish.material in catalog.MATERIALS:
                return finish.material
    return ""


def _grainier_species(species: str) -> str:
    """A sibling species with more visible texture, or nothing.

    Stays inside the family — swapping oak for marble because the render looks
    flat would answer a texture complaint by inventing a different building.
    The constraint layer would reject it; not proposing it is better.
    """
    from vision import catalog

    if not species:
        return ""
    family = catalog.material_family(species)
    current = catalog.get_material(species)
    candidates = [
        catalog.get_material(name) for name in catalog.species_of(family)
        if name != species
    ]
    grainier = [c for c in candidates if c.grain > current.grain + 0.05]
    if not grainier:
        return ""
    # The smallest step that increases grain: a large jump would answer a
    # subtle complaint with an obvious change.
    return min(grainier, key=lambda prior: (prior.grain, prior.name)).name


def _surfaces_using(material_name: str, room) -> List[str]:
    """Which of a room's surfaces wear this material."""
    if room is None:
        return []
    from vision import catalog

    species = _species_of_material(material_name, None, room)
    surfaces = []
    for label, finish in (("wall", room.wall_finish), ("floor", room.floor_finish),
                          ("ceiling", room.ceiling_finish)):
        if finish is None:
            continue
        if not material_name or finish.material == species or (
            catalog.material_family(finish.material) == catalog.material_family(species)
            and species
        ):
            surfaces.append(label)
    return surfaces


def _objects_using(material_name: str, graph, room) -> List[str]:
    if graph is None or not material_name:
        return []
    from vision import catalog

    species = _species_of_material(material_name, graph, room)
    if not species:
        return []
    family = catalog.material_family(species)
    return sorted(
        obj.id for obj in graph.objects
        if obj.material and catalog.material_family(obj.material) == family
        and (room is None or obj.room_id == room.id)
    )[:12]


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


def _palette(group_: FindingGroup, graph) -> List[Action]:
    """Nudge a room's colour roles toward the reference's measured colour."""
    room = _room(graph, group_.room)
    if room is None or getattr(room, "palette", None) is None:
        return []

    reference_hex = group_.evidence("reference_mean")
    render_hex = group_.evidence("render_mean")
    if not isinstance(reference_hex, str) or not isinstance(render_hex, str):
        return []

    from blender import colour as colour_mod

    # Blend toward the reference rather than adopting it: the reference's mean
    # includes its own lighting, so it is the direction that is trustworthy,
    # not the destination.
    roles = {}
    for role, current in room.palette.roles().items():
        shifted = colour_mod.mix(current, reference_hex, PALETTE_BLEND)
        if shifted.upper() != current.upper():
            roles[role] = shifted
    if not roles:
        return []

    return [Action(
        id=f"{ActionType.PALETTE_ADJUSTMENT}:{room.id}",
        type=ActionType.PALETTE_ADJUSTMENT,
        target=f"room:{room.id}",
        parameters={"roles": roles, "blend": PALETTE_BLEND,
                    "toward": reference_hex},
        summary=f"Shift {room.id}'s palette {int(PALETTE_BLEND * 100)}% toward "
                f"the reference's measured colour {reference_hex}",
        rooms=[room.id],
    )]


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def _placement(group_: FindingGroup, graph) -> List[Action]:
    """Move an object toward where its detection puts it.

    Where the object also carries an unsatisfied orientation relationship, a
    rotation is proposed alongside — the two are independent, both derive from
    the same displacement finding, and an object put in the right place facing
    the wrong way is still wrong.
    """
    object_id = group_.objects[0] if group_.objects else ""
    obj = _object(graph, object_id)
    if obj is None:
        return []

    implied = group_.evidence("implied")
    actual = group_.evidence("actual")
    if not (isinstance(implied, (list, tuple)) and isinstance(actual, (list, tuple))):
        return []

    dx = (float(implied[0]) - float(actual[0])) * DAMPING
    dy = (float(implied[1]) - float(actual[1])) * DAMPING
    distance = math.hypot(dx, dy)
    if distance < 0.02:
        return []
    if distance > TRANSLATION_LIMIT:
        scale = TRANSLATION_LIMIT / distance
        dx, dy, distance = dx * scale, dy * scale, TRANSLATION_LIMIT

    actions = [Action(
        id=f"{ActionType.FURNITURE_TRANSLATION}:{obj.id}",
        type=ActionType.FURNITURE_TRANSLATION,
        target=f"object:{obj.id}",
        parameters={"dx": round(dx, 4), "dy": round(dy, 4),
                    "damping": DAMPING},
        summary=f"Move {obj.category or obj.id} {distance * 100:.0f} cm toward "
                f"where the reference places it",
        rooms=[obj.room_id] if obj.room_id else [],
        objects=[obj.id],
    )]

    rotation = _rotation(obj, graph, group_)
    if rotation is not None:
        actions.append(rotation)
    return actions


def _rotation(obj, graph, group_: FindingGroup) -> Optional[Action]:
    """Turn an object to satisfy a relationship the solver could not.

    The evaluation has no rotation axis — a photograph and a render agreeing
    on where a chair *is* say little about which way it faces. The graph does
    know: the vision pass recorded "sofa faces tv_unit", and the placement
    solver recorded whether it managed to honour it. An unsatisfied
    relationship on an object already known to be misplaced is a real,
    deterministic defect, and rotating to face the named target is its fix.
    """
    if graph is None:
        return None

    for relationship in getattr(graph, "relationships", []) or []:
        if relationship.subject != obj.id or relationship.satisfied:
            continue
        if relationship.predicate not in ("faces", "facing", "oriented_toward"):
            continue
        target = _object(graph, relationship.object)
        if target is None:
            continue

        heading = math.degrees(math.atan2(
            -(target.position.x - obj.position.x),
            target.position.y - obj.position.y,
        )) % 360.0
        delta = ((heading - obj.rotation_z + 180.0) % 360.0) - 180.0
        if abs(delta) < 5.0:
            return None

        return Action(
            id=f"{ActionType.FURNITURE_ROTATION}:{obj.id}",
            type=ActionType.FURNITURE_ROTATION,
            target=f"object:{obj.id}",
            parameters={"rotation_z": round(heading, 2),
                        "delta_deg": round(delta, 2),
                        "relationship": relationship.predicate,
                        "toward": target.id},
            summary=f"Rotate {obj.category or obj.id} {delta:+.0f}° to face "
                    f"{target.category or target.id}",
            rationale=f"the graph asserts {obj.id} {relationship.predicate} "
                      f"{target.id} with confidence "
                      f"{relationship.confidence:.2f}, and records it as "
                      f"unsatisfied",
            rooms=[obj.room_id] if obj.room_id else [],
            objects=[obj.id],
        )
    return None


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def _asset(group_: FindingGroup, graph) -> List[Action]:
    """Answer a stand-in asset, two ways, and let ranking choose.

    An object built from a poorly matching asset has two possible causes: no
    good variant exists for its category, or its recorded proportions are
    wrong and are matching against the wrong shape. Both are plausible from
    the same evidence, so both are proposed and marked mutually exclusive —
    :mod:`planner.dependencies` enforces that only one runs.
    """
    object_id = group_.objects[0] if group_.objects else ""
    obj = _object(graph, object_id)
    if obj is None:
        return []

    from vision import assets

    dimensions = (obj.dimensions.width, obj.dimensions.depth, obj.dimensions.height)
    room = _room(graph, obj.room_id)
    style = getattr(room, "style", "") or ""

    match = assets.match_asset(obj.category, dimensions, style, obj.material,
                               obj.color_hex, obj.label)
    actions: List[Action] = []

    if match.variant is not None and match.variant.key != obj.asset:
        actions.append(Action(
            id=f"{ActionType.ASSET_REPLACEMENT}:{obj.id}",
            type=ActionType.ASSET_REPLACEMENT,
            target=f"object:{obj.id}",
            parameters={"asset": match.variant.key,
                        "asset_score": round(match.score, 4),
                        "previous": obj.asset},
            summary=f"Rebuild {obj.category or obj.id} from {match.variant.key} "
                    f"(matches {match.score:.2f} against the current "
                    f"{obj.asset_score:.2f})",
            rooms=[obj.room_id] if obj.room_id else [],
            objects=[obj.id],
        ))
    else:
        alternative = _alternative_variant(obj)
        if alternative:
            actions.append(Action(
                id=f"{ActionType.ASSET_VARIANT_SWAP}:{obj.id}",
                type=ActionType.ASSET_VARIANT_SWAP,
                target=f"object:{obj.id}",
                parameters={"asset": alternative, "previous": obj.asset},
                summary=f"Try {alternative} for {obj.category or obj.id}; the "
                        f"current choice is already the best-scoring one and "
                        f"still matches poorly",
                rooms=[obj.room_id] if obj.room_id else [],
                objects=[obj.id],
            ))

    scale = _scale_action(obj, match)
    if scale is not None:
        actions.append(scale)
    return actions


def _alternative_variant(obj) -> str:
    """A different variant of the same category, chosen deterministically."""
    from vision import assets

    variants = [v.key for v in assets.variants_for(obj.category) if v.key != obj.asset]
    return sorted(variants)[0] if variants else ""


def _scale_action(obj, match) -> Optional[Action]:
    """Correct proportions that disagree with the asset they matched against.

    The variant's signature is the shape it is built to be. When the recorded
    dimensions are a long way from it, the object is being stretched into a
    silhouette the asset was never meant to have, and the reading that
    produced those dimensions is the more likely error.
    """
    if match.variant is None:
        return None
    signature = getattr(match.variant, "signature", None)
    if not signature or obj.dimensions.height <= 1e-6:
        return None

    # Height-normalised, matching how the asset matcher itself compares.
    current = (obj.dimensions.width / obj.dimensions.height,
               obj.dimensions.depth / obj.dimensions.height)
    target = (signature[0] / signature[2] if signature[2] else current[0],
              signature[1] / signature[2] if signature[2] else current[1])
    disagreement = max(abs(current[0] - target[0]), abs(current[1] - target[1]))
    if disagreement < SCALE_DISAGREEMENT:
        return None

    width = obj.dimensions.height * (current[0] + (target[0] - current[0]) * DAMPING)
    depth = obj.dimensions.height * (current[1] + (target[1] - current[1]) * DAMPING)

    return Action(
        id=f"{ActionType.FURNITURE_SCALE}:{obj.id}",
        type=ActionType.FURNITURE_SCALE,
        target=f"object:{obj.id}",
        parameters={"width": round(width, 4), "depth": round(depth, 4),
                    "height": round(obj.dimensions.height, 4),
                    "disagreement": round(disagreement, 4)},
        summary=f"Reproportion {obj.category or obj.id} toward "
                f"{match.variant.key}'s shape "
                f"({obj.dimensions.width:.2f}x{obj.dimensions.depth:.2f} -> "
                f"{width:.2f}x{depth:.2f} m)",
        rationale=f"the recorded footprint disagrees with the matched asset's "
                  f"proportions by {disagreement:.2f}, which is what made it a "
                  f"poor match in the first place",
        rooms=[obj.room_id] if obj.room_id else [],
        objects=[obj.id],
    )


# ---------------------------------------------------------------------------
# Decor density
# ---------------------------------------------------------------------------


def _decor(group_: FindingGroup, graph) -> List[Action]:
    """Admit objects the confidence policy withheld.

    "Decor density" is the spec's name for the admit/withhold lever, and this
    is the only place it is pulled. Nothing is invented: the objects were
    detected and recorded, and the generator declined to build them because
    they fell below the acceptance threshold. Admitting one is a policy
    decision the optimiser can make and unmake, which is exactly what makes it
    a legitimate action rather than a fabrication.
    """
    if graph is None:
        return []

    candidates = sorted(
        {obj_id for finding in group_.findings for obj_id in finding.objects}
    )
    admit = [
        object_id for object_id in candidates
        if (obj := _object(graph, object_id)) is not None
        and getattr(obj, "uncertain", False)
        and not obj.dimensions.is_degenerate()
    ]
    if not admit:
        return []

    room = group_.room or ""
    return [Action(
        id=f"{ActionType.DECOR_DENSITY}:{room or 'building'}",
        type=ActionType.DECOR_DENSITY,
        target=f"room:{room}" if room else "building:all",
        parameters={"admit": admit},
        summary=f"Admit {len(admit)} withheld detection(s) in "
                f"{room or 'the building'}: {', '.join(admit[:4])}",
        rationale="these were detected but withheld for being below the "
                  "acceptance threshold; admitting them is reversible and the "
                  "render will show whether they belong",
        rooms=[room] if room else [],
        objects=admit,
    )]


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


def _camera(group_: FindingGroup, graph) -> List[Action]:
    """Shift a viewpoint by the offset every object in it appeared to share."""
    finding = group_.findings[0]
    viewpoint_id = finding.viewpoint
    viewpoint = _viewpoint(graph, viewpoint_id)
    if viewpoint is None:
        return []

    offset = finding.evidence.get("offset_m")
    if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
        return []

    # The offset is how far objects appeared to be from where they are, so the
    # camera moves the other way.
    dx = -float(offset[0]) * DAMPING
    dy = -float(offset[1]) * DAMPING
    distance = math.hypot(dx, dy)
    if distance < 0.05:
        return []
    if distance > CAMERA_LIMIT:
        scale = CAMERA_LIMIT / distance
        dx, dy, distance = dx * scale, dy * scale, CAMERA_LIMIT

    return [Action(
        id=f"{ActionType.CAMERA_CORRECTION}:{viewpoint_id}",
        type=ActionType.CAMERA_CORRECTION,
        target=f"viewpoint:{viewpoint_id}",
        parameters={"dx": round(dx, 4), "dy": round(dy, 4),
                    "coherence": finding.evidence.get("coherence")},
        summary=f"Move viewpoint {viewpoint_id} {distance * 100:.0f} cm to "
                f"remove the offset shared by every object in it",
        rooms=[finding.room] if finding.room else [],
    )]


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def style_actions(finding_set: FindingSet, graph) -> List[Action]:
    """Give a style-less room the building's style.

    Not derived from a group, because no finding says "this room has no
    style" — the symptom is a scatter of material and colour complaints in a
    room the generator had to furnish with defaults. The fix is deterministic
    and needs no inference: adopt the style the rest of the building already
    has, area-weighted.

    Nothing is proposed when no other room has a style, because there would be
    nothing to adopt and guessing one is exactly the kind of invention this
    pipeline refuses.
    """
    if graph is None:
        return []

    dominant, weight = _dominant_style(graph)
    if not dominant or dominant == "unknown":
        return []

    actions: List[Action] = []
    for room_id in finding_set.rooms():
        room = _room(graph, room_id)
        if room is None:
            continue
        if room.style and room.style != "unknown":
            continue
        appearance = [f for f in finding_set.by_room(room_id)
                      if f.axis in ("colour", "material")]
        if not appearance:
            continue

        actions.append(Action(
            id=f"{ActionType.STYLE_REFINEMENT}:{room_id}",
            type=ActionType.STYLE_REFINEMENT,
            target=f"room:{room_id}",
            parameters={"style": dominant, "previous": room.style or "unknown",
                        "style_confidence": round(weight, 3)},
            summary=f"Adopt the building's {dominant} style for {room_id}, "
                    f"which currently has none",
            rationale=f"{len(appearance)} appearance finding(s) in a room the "
                      f"generator had to furnish with defaults; the rest of "
                      f"the building is {dominant} "
                      f"({weight:.0%} of styled floor area)",
            expected_gain=0.0,
            confidence=min(0.7, weight),
            rooms=[room_id],
            axes=sorted({f.axis for f in appearance}),
            trigger_findings=[f.key for f in appearance],
            trigger_summaries=[f.summary for f in appearance],
        ))
    return actions


def _dominant_style(graph) -> Tuple[str, float]:
    """Area-weighted style across the rooms that have one."""
    scores: Dict[str, float] = {}
    for room in getattr(graph, "rooms", []) or []:
        if room.style and room.style != "unknown":
            weight = max(room.area, 1.0) * max(room.style_confidence, 0.2)
            scores[room.style] = scores.get(room.style, 0.0) + weight
    if not scores:
        return "", 0.0
    best = max(sorted(scores), key=lambda key: scores[key])
    return best, scores[best] / sum(scores.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _room(graph, room_id: str):
    if graph is None or not room_id:
        return None
    return graph.room_by_id(room_id)


def _object(graph, object_id: str):
    if graph is None or not object_id:
        return None
    return graph.object_by_id(object_id)


def _viewpoint(graph, viewpoint_id: str):
    if graph is None or not viewpoint_id:
        return None
    return next((v for v in getattr(graph, "viewpoints", []) or []
                 if v.image_id == viewpoint_id), None)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _damp_scale(scale: float) -> float:
    """Move a multiplicative correction only ``DAMPING`` of the way.

    In log space, so halving and doubling are damped symmetrically. Damping a
    ratio linearly would treat 0.5x as a smaller correction than 2x, which is
    the wrong shape for anything measured as a proportion.
    """
    if scale <= 0:
        return 1.0
    return math.exp(math.log(scale) * DAMPING)
