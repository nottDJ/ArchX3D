"""
ArchX3D — Review payload and user edits
=======================================
The bridge between the vision pipeline and the validation step of the wizard.

Two directions:

* :func:`build_review` flattens a scene graph into everything the review UI
  needs to show — rooms, their furniture, materials, lighting, per-object
  confidence, warnings, and crucially the detections that were *discarded*.
  Showing what was thrown away is what makes the step a review rather than a
  progress bar: a user who sees "3 detections dropped below the confidence
  floor" can decide to keep them.

* :func:`apply_edits` takes the user's decisions back and rewrites the graph
  before generation.

Edits are applied to a copy and every change is reported, so the operation is
inspectable and the original scene graph on disk is never mutated in place by
a partially-applied edit set.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import assets, catalog, geometry2d as g2
from .schema import (
    ConfidencePolicy,
    Dimensions,
    Finish,
    LightSource,
    SceneGraph,
    SceneObject,
    Vec3,
)
# Shared with the automatic validator so a placement the user is allowed to make
# by hand is exactly one the pipeline would also accept, and so tuning a
# tolerance in one place does not silently desynchronise the two.
from .validate import (
    MAX_FOOTPRINT_SHARE,
    OVERLAP_TOLERANCE,
    _is_support_pair,
    _surface_height,
)

#: Bounds on a hand-edited dimension, in metres. Not taste — anything outside
#: this range produces geometry the generator cannot build sensibly.
MIN_DIMENSION = 0.05
MAX_DIMENSION = 20.0

#: Everything one ``object_overrides`` entry may contain. Anything else is
#: reported back rather than ignored, so a UI typo is visible immediately.
OVERRIDE_KEYS = frozenset(
    {
        "category", "room_id", "label", "position", "rotation_z", "dimensions",
        "locked", "asset", "material", "color_hex",
    }
)

#: Keys one ``light_overrides`` entry may contain.
LIGHT_OVERRIDE_KEYS = frozenset(
    {"kind", "position", "mounting", "color_temperature_k", "power_w", "size", "length"}
)

#: Surfaces ``room_finishes`` can address.
FINISH_SURFACES = ("wall", "floor", "ceiling")

#: Bounds on hand-edited lighting values. Outside these the renderer either
#: produces nothing or blows the exposure out entirely.
MIN_COLOR_TEMPERATURE_K = 1500.0
MAX_COLOR_TEMPERATURE_K = 10000.0
MAX_POWER_W = 5000.0


# ---------------------------------------------------------------------------
# Review payload
# ---------------------------------------------------------------------------


def build_review(graph: SceneGraph) -> Dict[str, Any]:
    """Flatten a scene graph into the review UI's view model."""
    diagnostics = graph.diagnostics or {}
    provenance = graph.provenance or {}

    rooms: List[Dict[str, Any]] = []
    for room in graph.rooms:
        objects = [o for o in graph.objects if o.room_id == room.id]
        lights = [x for x in graph.lights if x.room_id == room.id]
        openings = [o for o in graph.openings if o.room_id == room.id]

        rooms.append(
            {
                "id": room.id,
                "room_type": room.room_type,
                "style": room.style,
                "style_confidence": round(room.style_confidence, 3),
                "palette": room.palette.to_dict() if room.palette else None,
                "lighting_environment": room.lighting.to_dict() if room.lighting else None,
                "area": round(room.area, 1),
                "width": round(room.bounds_max[0] - room.bounds_min[0], 2),
                "depth": round(room.bounds_max[1] - room.bounds_min[1], 2),
                "ceiling_height": room.ceiling_height,
                "confidence": round(room.confidence, 3),
                "polygon": [[round(p[0], 3), round(p[1], 3)] for p in room.polygon],
                "bounds_min": list(room.bounds_min),
                "bounds_max": list(room.bounds_max),
                "connected_to": list(room.connected_to),
                "source_images": list(room.source_images),
                "has_imagery": bool(room.source_images),
                "finishes": _finishes_view(room, graph),
                "object_count": len(objects),
                "objects": [_object_view(o, graph) for o in objects],
                "lights": [_light_view(x) for x in lights],
                "openings": [
                    {"id": o.id, "kind": o.kind, "width": round(o.width, 2),
                     "height": round(o.height, 2), "confidence": round(o.confidence, 3)}
                    for o in openings
                ],
            }
        )

    unassigned = [o for o in graph.objects if not o.room_id]

    return {
        "schema_version": graph.schema_version,
        "generated_at": provenance.get("generated_at"),
        "images": diagnostics.get("images", []),
        "image_summary": diagnostics.get("image_summary", {}),
        "rooms": rooms,
        "unassigned_objects": [_object_view(o, graph) for o in unassigned],
        "relationships": [
            {
                "subject": r.subject, "predicate": r.predicate, "object": r.object,
                "confidence": round(r.confidence, 3), "satisfied": r.satisfied,
            }
            for r in graph.relationships
        ],
        "totals": {
            "rooms": len(graph.rooms),
            "rooms_with_imagery": sum(1 for r in graph.rooms if r.source_images),
            "objects": len(graph.objects),
            "buildable": len(graph.buildable_objects()),
            "uncertain": sum(1 for o in graph.objects if o.uncertain),
            "lights": len(graph.lights),
            "openings": len(graph.openings),
        },
        # The vocabularies the edit endpoint will accept, so the UI's dropdowns
        # cannot drift out of step with what the server validates against.
        "vocabulary": {
            "room_types": list(catalog.ROOM_TYPES),
            "categories": [
                {
                    "category": name,
                    "group": prior.group,
                    "typical": list(prior.typical),
                    "support": prior.support,
                }
                for name, prior in sorted(catalog.OBJECT_CATALOG.items())
            ],
            "materials": [
                {
                    "material": name,
                    "color_hex": prior.color_hex,
                    "roughness": prior.roughness,
                    "metallic": prior.metallic,
                    "applies_to": list(prior.applies_to),
                }
                for name, prior in sorted(catalog.MATERIALS.items())
            ],
            "ceiling_types": list(catalog.CEILING_TYPES),
            "light_kinds": [
                {
                    "kind": name,
                    "mounting": prior.mounting,
                    "power_w": prior.power_w,
                    "color_temperature_k": prior.cct_k,
                }
                for name, prior in sorted(catalog.LIGHT_TYPES.items())
            ],
            # The asset browser offers alternatives to whatever the matcher
            # picked, so it needs the variants grouped by the category they
            # belong to rather than as one flat list.
            "assets": [
                {
                    "key": variant.key,
                    "category": variant.category,
                    "styles": list(variant.styles),
                    "materials": list(variant.materials),
                    "signature": list(variant.signature),
                }
                for variant in assets.ASSET_VARIANTS
            ],
        },
        # How well the asset library covered this scene. Surfaced so a
        # systematically uncovered category reads as a known gap rather than
        # as unexplained wrongness in the render.
        "asset_quality": assets.match_quality(graph.objects),
        # How each plan-view sheet lines up with the drawing. Surfaced because
        # a placement's trustworthiness is a property of the registration
        # behind it, not of the detection: an object read off an unregistered
        # sheet may be perfectly detected and still be in the wrong room.
        "registration": diagnostics.get("registration", {}),
        "confidence": diagnostics.get("confidence", {}),
        "warnings": _warnings(graph, diagnostics),
        "ignored": _ignored(diagnostics),
        "validation": {
            "corrected": diagnostics.get("validation", {}).get("corrected", 0),
            "uncorrected": diagnostics.get("validation", {}).get("uncorrected", 0),
            "by_kind": diagnostics.get("validation", {}).get("by_kind", {}),
            "issues": diagnostics.get("validation", {}).get("issues", [])[:80],
        },
        "elapsed_s": provenance.get("elapsed_s"),
    }


def _finishes_view(room, graph: SceneGraph) -> Dict[str, Any]:
    """Per-room finishes, falling back to the graph-level ones when unobserved."""
    wall = room.wall_finish
    if wall is None:
        wall = graph.walls[0].finish if graph.walls else None

    return {
        "wall": wall.to_dict() if wall is not None else None,
        "floor": (room.floor_finish or graph.floor).to_dict(),
        "ceiling": (room.ceiling_finish or graph.ceiling).to_dict(),
        "ceiling_type": room.ceiling_type or graph.ceiling_type,
    }


def _object_view(obj: SceneObject, graph: SceneGraph) -> Dict[str, Any]:
    return {
        "id": obj.id,
        "category": obj.category,
        "label": obj.label,
        "group": obj.group,
        "room_id": obj.room_id,
        "position": obj.position.to_dict(),
        "rotation_z": round(obj.rotation_z, 1),
        "dimensions": obj.dimensions.to_dict(),
        "material": obj.material,
        "material_family": catalog.material_family(obj.material),
        "color_hex": obj.color_hex,
        "asset": obj.asset,
        "asset_score": round(obj.asset_score, 3),
        # Named so the UI can say "closest available match" rather than
        # presenting a compromise as if it were a likeness.
        "asset_quality": _asset_quality(obj),
        "confidence": round(obj.confidence, 3),
        "band": ConfidencePolicy.classify(obj.confidence),
        "uncertain": obj.uncertain,
        "locked": obj.locked,
        "support": obj.support,
        "support_id": obj.support_id,
        "will_build": (not obj.uncertain) and not obj.dimensions.is_degenerate(),
        "source_images": list(obj.source_images),
        "observation_count": obj.observation_count,
        "flags": list(obj.flags),
        "distance_to_nearest_wall": obj.distance_to_nearest_wall,
    }


def _asset_quality(obj: SceneObject) -> str:
    """How well the chosen procedural model matches what was observed.

    ``none`` means no variant exists for the category at all and the object is
    built as a proportioned block — a gap in the library rather than a bad
    choice, and worth distinguishing.
    """
    if not obj.asset or obj.asset.startswith("generic_"):
        return "none"
    if obj.asset_score >= 0.80:
        return "close"
    if obj.asset_score >= assets.POOR_MATCH_THRESHOLD:
        return "fair"
    return "approximate"


def _light_view(light) -> Dict[str, Any]:
    return {
        "id": light.id,
        "kind": light.kind,
        "mounting": light.mounting,
        "position": light.position.to_dict(),
        "color_temperature_k": light.color_temperature_k,
        "power_w": round(light.power_w, 1),
        "confidence": round(light.confidence, 3),
        "uncertain": light.uncertain,
        "source_images": list(light.source_images),
    }


def _warnings(graph: SceneGraph, diagnostics: Dict[str, Any]) -> List[str]:
    """Everything the user should look at before spending render time."""
    warnings = list(diagnostics.get("warnings", []))

    empty = [r.id for r in graph.rooms if not r.source_images]
    if empty:
        warnings.append(
            f"{len(empty)} room(s) have no reference image and will be built "
            f"empty: {', '.join(empty)}"
        )

    uncertain = [o for o in graph.objects if o.uncertain]
    if uncertain:
        warnings.append(
            f"{len(uncertain)} detection(s) fell below the confidence threshold "
            "and will be skipped unless you keep them"
        )

    unresolved = diagnostics.get("validation", {}).get("uncorrected", 0)
    if unresolved:
        warnings.append(
            f"{unresolved} placement problem(s) could not be fixed automatically"
        )

    warnings.extend(_user_placement_conflicts(graph))
    warnings.extend(_registration_warnings(diagnostics))

    for error in diagnostics.get("errors", [])[:5]:
        warnings.append(f"error: {error}")

    return warnings


def _registration_warnings(diagnostics: Dict[str, Any]) -> List[str]:
    """Plan views whose alignment to the drawing was assumed, not measured.

    This belongs in the review panel rather than only in the log, because it
    changes what the reviewer is looking at. Furniture read off an
    unregistered sheet can be detected perfectly and still be in the wrong
    room, and nothing about the object itself shows that — only the
    registration behind it does. A reviewer who does not know which sheets
    registered cannot tell a placement worth correcting from one worth
    discarding wholesale.
    """
    registration = diagnostics.get("registration") or {}
    plan_views = registration.get("plan_views") or []
    if not plan_views:
        return []

    warnings: List[str] = []

    unregistered = [r for r in plan_views if not r.get("registered")]
    if unregistered:
        names = ", ".join(str(r.get("image_id", "?")) for r in unregistered[:4])
        warnings.append(
            f"{len(unregistered)} of {len(plan_views)} plan view(s) could not be "
            f"aligned to the drawing ({names}). Their furniture was placed by "
            "assuming each image is one floor plan filling the frame; if it is "
            "not, those placements are wrong. Labelling the plan's rooms "
            "legibly, or cropping to a single plan, would fix it."
        )

    for record in plan_views:
        region = record.get("sheet_region") or {}
        if record.get("registered") and region.get("looks_composite"):
            warnings.append(
                f"{record.get('image_id', '?')}: the drawing occupies only "
                f"{region.get('coverage', 0):.0%} of this sheet. It registered "
                "correctly, but anything read from the rest of the frame is not "
                "part of this floor."
            )

        others = record.get("unmatched_image_labels") or []
        if record.get("registered") and len(others) >= 2:
            warnings.append(
                f"{record.get('image_id', '?')}: {len(others)} label(s) on this "
                f"sheet name rooms the drawing does not contain "
                f"({', '.join(str(x) for x in others[:4])}). This sheet most "
                "likely shows more than one floor."
            )

    return warnings


def _user_placement_conflicts(graph: SceneGraph) -> List[str]:
    """Overlaps involving something the user placed by hand.

    Recomputed from the graph on every build rather than replayed from the edit
    report, so the warning disappears as soon as the user moves the object
    clear instead of lingering as a stale complaint. Automatic validation does
    not run again after the review step, which is why this check lives here.
    """
    placed = [
        o for o in graph.objects
        if any(f.startswith(("position_set_by_user", "rotation_z_set_by_user",
                             "dimensions_set_by_user")) for f in o.flags)
    ]
    if not placed:
        return []

    conflicts: List[str] = []
    seen: set = set()
    for obj in placed:
        for other in graph.objects:
            if other.id == obj.id or other.room_id != obj.room_id:
                continue
            if other.dimensions.is_degenerate() or _is_support_pair(obj, other):
                continue
            if (obj.position.z + obj.dimensions.height <= other.position.z
                    or other.position.z + other.dimensions.height <= obj.position.z):
                continue
            penetration = g2.rect_overlap(
                obj.footprint_corners(), other.footprint_corners()
            )
            if penetration <= OVERLAP_TOLERANCE:
                continue
            key = tuple(sorted((obj.id, other.id)))
            if key in seen:
                continue
            seen.add(key)
            conflicts.append(
                f"you placed {obj.category} where it overlaps "
                f"{other.category} by {penetration:.2f} m"
            )

    return conflicts[:10]


def _ignored(diagnostics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detections the pipeline discarded, with a plain-English reason."""
    explanations = {
        "unrecognised_category": "not in the recognised object vocabulary",
        "below_confidence_floor": "confidence below the 0.40 floor",
        "missing_or_empty_bbox": "no usable image box, so it could not be placed",
        "unenforceable_predicate": "relationship type the placement solver cannot apply",
        "relationship_dangling_reference": "relationship referenced an object that was dropped",
        "appearance_from_technical_drawing": (
            "materials/furniture reported from a CAD drawing, which has none"
        ),
        "dropped_by_skip_mode": "exterior or site image, which cannot furnish an interior",
        "light_below_confidence_floor": "light confidence below the 0.40 floor",
        "opening_below_confidence_floor": "opening confidence below the 0.40 floor",
        "unrecognised_light_kind": "not a recognised luminaire type",
        "unrecognised_opening_kind": "not a recognised opening type",
        "arch_below_confidence_floor": "structural element below the confidence floor",
        "unrecognised_arch_kind": "not a recognised structural element",
    }
    return [
        {"reason": reason, "count": count,
         "explanation": explanations.get(reason, "discarded during parsing")}
        for reason, count in sorted(diagnostics.get("rejections", {}).items())
    ]


# ---------------------------------------------------------------------------
# Applying user edits
# ---------------------------------------------------------------------------


@dataclass
class EditReport:
    removed: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    recategorised: List[str] = field(default_factory=list)
    moved: List[str] = field(default_factory=list)
    room_types_changed: List[str] = field(default_factory=list)
    lights_removed: List[str] = field(default_factory=list)
    transformed: List[str] = field(default_factory=list)
    lock_changed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    restyled: List[str] = field(default_factory=list)
    finishes_changed: List[str] = field(default_factory=list)
    lights_changed: List[str] = field(default_factory=list)
    lights_added: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    #: Accepted edits that produced a questionable result, e.g. two objects the
    #: user chose to overlap. Not errors — the human's placement wins — but the
    #: UI should say so rather than let it be discovered in the render.
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "removed_objects": self.removed,
            "kept_uncertain_objects": self.kept,
            "recategorised": self.recategorised,
            "moved_between_rooms": self.moved,
            "room_types_changed": self.room_types_changed,
            "lights_removed": self.lights_removed,
            "transformed": self.transformed,
            "lock_changed": self.lock_changed,
            "added_objects": self.added,
            "restyled": self.restyled,
            "finishes_changed": self.finishes_changed,
            "lights_changed": self.lights_changed,
            "lights_added": self.lights_added,
            "rejected_edits": self.rejected,
            "warnings": self.warnings,
            "total_changes": (
                len(self.removed) + len(self.kept) + len(self.recategorised)
                + len(self.moved) + len(self.room_types_changed)
                + len(self.lights_removed) + len(self.transformed) + len(self.lock_changed)
                + len(self.added) + len(self.restyled) + len(self.finishes_changed)
                + len(self.lights_changed) + len(self.lights_added)
            ),
        }


def apply_edits(graph: SceneGraph, edits: Dict[str, Any]) -> tuple[SceneGraph, EditReport]:
    """Return a copy of ``graph`` with the user's review decisions applied.

    Recognised keys:

    ``remove_objects``      list of object ids to delete outright
    ``keep_objects``        uncertain object ids the user confirmed
    ``object_overrides``    per-object changes, see below
    ``room_types``          ``{room_id: room_type}``
    ``remove_lights``       list of light ids to delete

    An override entry accepts any of::

        {"category": str, "room_id": str, "label": str,       # classification
         "position": {"x": float, "y": float},                # transform
         "rotation_z": float, "dimensions": {"width": float,
         "depth": float, "height": float}, "locked": bool}

    The three transform keys are validated together against the object's final
    state, not one at a time — a resize that only fits because of a
    simultaneous move must be judged on the result, not on either half.

    Anything unrecognised is reported in ``rejected`` rather than silently
    ignored, so a UI bug surfaces instead of quietly dropping a user's edit.
    """
    updated = copy.deepcopy(graph)
    report = EditReport()

    known_objects = {o.id for o in updated.objects}
    known_lights = {x.id for x in updated.lights}
    known_rooms = {r.id for r in updated.rooms}

    # --- Removals ---------------------------------------------------------
    remove = set(edits.get("remove_objects") or [])
    for object_id in sorted(remove):
        if object_id not in known_objects:
            report.rejected.append(f"remove_objects: unknown id {object_id!r}")
    if remove:
        before = len(updated.objects)
        updated.objects = [o for o in updated.objects if o.id not in remove]
        report.removed = sorted(remove & known_objects)
        # Anything resting on a removed object must go too, or it floats.
        report.removed += _cascade_supports(updated, set(report.removed))
        if before - len(updated.objects) != len(report.removed):
            report.rejected.append("removal count did not match; see removed list")

    # --- Confirmations ----------------------------------------------------
    for object_id in edits.get("keep_objects") or []:
        obj = updated.object_by_id(object_id)
        if obj is None:
            report.rejected.append(f"keep_objects: unknown id {object_id!r}")
            continue
        if obj.uncertain:
            obj.uncertain = False
            obj.flags.append("confirmed_by_user")
            report.kept.append(object_id)

    # --- Per-object overrides --------------------------------------------
    overrides = edits.get("object_overrides") or {}
    for object_id, changes in overrides.items():
        obj = updated.object_by_id(object_id)
        if obj is None:
            report.rejected.append(f"object_overrides: unknown id {object_id!r}")
            continue
        if not isinstance(changes, dict):
            report.rejected.append(f"object_overrides: {object_id!r} value is not an object")
            continue

        for key in sorted(set(changes) - OVERRIDE_KEYS):
            report.rejected.append(
                f"object_overrides: {object_id!r} has unrecognised key {key!r}"
            )

        if "category" in changes:
            category = catalog.normalise_category(str(changes["category"]))
            if category is None:
                report.rejected.append(
                    f"object_overrides: {object_id!r} category "
                    f"{changes['category']!r} is not recognised"
                )
            else:
                obj.category = category
                prior = catalog.get_prior(category)
                if prior:
                    obj.group = prior.group
                obj.flags.append("category_set_by_user")
                report.recategorised.append(object_id)

        if "room_id" in changes:
            room_id = str(changes["room_id"])
            if room_id not in known_rooms:
                report.rejected.append(
                    f"object_overrides: {object_id!r} room {room_id!r} does not exist"
                )
            elif room_id != obj.room_id:
                _move_to_room(obj, updated, room_id)
                obj.flags.append("room_set_by_user")
                report.moved.append(object_id)

        if "label" in changes:
            obj.label = str(changes["label"])[:160]

        _apply_appearance(obj, changes, report)

        # Transform last: it is validated against the room the object ended up
        # in, which the room_id override above may just have changed.
        _apply_transform(obj, updated, changes, report)

    # --- Room types -------------------------------------------------------
    for room_id, room_type in (edits.get("room_types") or {}).items():
        room = updated.room_by_id(room_id)
        if room is None:
            report.rejected.append(f"room_types: unknown room {room_id!r}")
            continue
        normalised = str(room_type).strip().lower().replace(" ", "_")
        if normalised not in catalog.ROOM_TYPES:
            report.rejected.append(f"room_types: {room_type!r} is not a known room type")
            continue
        if normalised != room.room_type:
            room.room_type = normalised
            room.confidence = 1.0  # stated by a human
            report.room_types_changed.append(room_id)

    # --- Lights -----------------------------------------------------------
    remove_lights = set(edits.get("remove_lights") or [])
    for light_id in sorted(remove_lights):
        if light_id not in known_lights:
            report.rejected.append(f"remove_lights: unknown id {light_id!r}")
    if remove_lights:
        updated.lights = [x for x in updated.lights if x.id not in remove_lights]
        report.lights_removed = sorted(remove_lights & known_lights)

    # --- Duplicated / pasted objects --------------------------------------
    for spec in edits.get("add_objects") or []:
        _add_object(updated, spec, report)

    # --- Surface finishes --------------------------------------------------
    for room_id, surfaces in (edits.get("room_finishes") or {}).items():
        _apply_room_finishes(updated, room_id, surfaces, report)

    # --- Lighting ----------------------------------------------------------
    for light_id, changes in (edits.get("light_overrides") or {}).items():
        _apply_light_override(updated, light_id, changes, report)

    for spec in edits.get("add_lights") or []:
        _add_light(updated, spec, report)

    # --- Keep the graph internally consistent -----------------------------
    # Runs last so it sees the additions too, not just the removals.
    surviving = {o.id for o in updated.objects}
    updated.relationships = [
        r for r in updated.relationships
        if r.subject in surviving and r.object in surviving
    ]

    unknown_keys = set(edits) - {
        "remove_objects", "keep_objects", "object_overrides", "room_types",
        "remove_lights", "add_objects", "room_finishes", "light_overrides",
        "add_lights",
    }
    for key in sorted(unknown_keys):
        report.rejected.append(f"unrecognised edit key {key!r}")

    updated.diagnostics = dict(updated.diagnostics or {})
    updated.diagnostics["user_edits"] = report.to_dict()

    return updated, report


def _apply_appearance(
    obj: SceneObject, changes: Dict[str, Any], report: EditReport
) -> None:
    """Swap the asset variant, material or colour of one object.

    Appearance is independent of placement by design: replacing a three-seat
    sofa with a sectional must not move it, because the user chose where it
    goes and is only disagreeing about what it looks like.
    """
    changed = False

    if "asset" in changes:
        key = str(changes["asset"])
        variants = {v.key for v in assets.variants_for(obj.category)}
        if key and key not in variants:
            report.rejected.append(
                f"object_overrides: {obj.id!r} asset {key!r} is not a variant of "
                f"{obj.category!r}"
            )
        else:
            obj.asset = key
            # The score described how well the *matcher* liked its own pick;
            # it says nothing about a choice the user made, so it is cleared
            # rather than left to look like evidence for the new asset.
            obj.asset_score = 0.0
            obj.flags.append("asset_set_by_user")
            changed = True

    if "material" in changes:
        material = _strict_material(changes["material"])
        if material is None:
            report.rejected.append(
                f"object_overrides: {obj.id!r} material {changes['material']!r} "
                "is not recognised"
            )
        else:
            obj.material = material
            obj.flags.append("material_set_by_user")
            changed = True

    if "color_hex" in changes:
        colour = _parse_hex(changes["color_hex"])
        if colour is None:
            report.rejected.append(
                f"object_overrides: {obj.id!r} color_hex {changes['color_hex']!r} "
                "is not a #RRGGBB colour"
            )
        else:
            obj.color_hex = colour
            obj.flags.append("color_set_by_user")
            changed = True

    if changed and obj.id not in report.restyled:
        report.restyled.append(obj.id)


def _strict_material(raw: Any) -> Optional[str]:
    """Resolve a material name, or ``None`` if it does not resolve.

    ``catalog.normalise_material`` falls back to ``"unknown"``, which is right
    for model output — a strange word should not abort a run — and wrong here.
    A user who types a material that does not exist has made a mistake worth
    reporting, not a request to set the material to "unknown".
    """
    text = str(raw).strip()
    if not text:
        return None
    resolved = catalog.normalise_material(text)
    if resolved == "unknown" and text.lower() != "unknown":
        return None
    return resolved


def _parse_hex(value: Any) -> Optional[str]:
    """Validate a ``#RRGGBB`` colour, rejecting anything else."""
    text = str(value).strip()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) != 7:
        return None
    try:
        int(text[1:], 16)
    except ValueError:
        return None
    return text.upper()


def _add_object(graph: SceneGraph, spec: Any, report: EditReport) -> None:
    """Insert a duplicated or pasted object.

    Copying an existing object is the common case — duplicate and paste both
    land here — so ``source_id`` clones everything about it and the rest of the
    spec overrides individual fields. Creating from scratch requires only a
    category, with the catalog supplying plausible dimensions.
    """
    if not isinstance(spec, dict):
        report.rejected.append("add_objects: entry is not an object")
        return

    source_id = spec.get("source_id")
    if source_id:
        source = graph.object_by_id(str(source_id))
        if source is None:
            report.rejected.append(f"add_objects: unknown source_id {source_id!r}")
            return
        created = copy.deepcopy(source)
        created.locked = False
        # A copy is not independent evidence; it carries the original's flags
        # only as provenance, never its "confirmed by a human" status.
        created.flags = [f"copied_from_{source.id}"]
        created.observation_count = 0
        created.source_images = []
    else:
        category = catalog.normalise_category(str(spec.get("category", "")))
        if category is None:
            report.rejected.append(
                f"add_objects: {spec.get('category')!r} is not a recognised category"
            )
            return
        prior = catalog.get_prior(category)
        created = SceneObject(
            category=category,
            group=prior.group if prior else "furniture",
            dimensions=Dimensions(*prior.typical) if prior else Dimensions(0.6, 0.6, 0.6),
            confidence=1.0,  # placed by a human, not detected
            flags=["created_by_user"],
        )

    created.id = _unique_id(graph, created.category)
    created.uncertain = False

    room_id = str(spec.get("room_id") or created.room_id)
    if room_id and graph.room_by_id(room_id) is None:
        report.rejected.append(f"add_objects: room {room_id!r} does not exist")
        return
    created.room_id = room_id

    # Anything resting on the source would be duplicated too if it were
    # copied blindly; a pasted lamp is a free-standing lamp until the user
    # says otherwise.
    created.support = "floor" if created.support == "on_object" else created.support
    created.support_id = ""

    graph.objects.append(created)

    overrides = {k: v for k, v in spec.items() if k in OVERRIDE_KEYS}
    if overrides:
        _apply_appearance(created, overrides, report)
        _apply_transform(created, graph, overrides, report)
        if any(entry.startswith(f"object_overrides: {created.id!r}")
               for entry in report.rejected):
            # The placement was refused, so the copy would land on top of its
            # original. Rolling it back is better than adding a hidden object.
            graph.objects.remove(created)
            report.rejected.append(
                f"add_objects: {created.id!r} discarded because its placement was refused"
            )
            return

    report.added.append(created.id)


def _unique_id(graph: SceneGraph, category: str) -> str:
    """A stable, readable id that cannot collide with an existing one."""
    taken = {o.id for o in graph.objects}
    index = 1
    while f"{category}_user_{index}" in taken:
        index += 1
    return f"{category}_user_{index}"


def _apply_room_finishes(
    graph: SceneGraph, room_id: str, surfaces: Any, report: EditReport
) -> None:
    """Edit the wall, floor or ceiling treatment of one room."""
    room = graph.room_by_id(room_id)
    if room is None:
        report.rejected.append(f"room_finishes: unknown room {room_id!r}")
        return
    if not isinstance(surfaces, dict):
        report.rejected.append(f"room_finishes: {room_id!r} value is not an object")
        return

    changed = False
    for surface in FINISH_SURFACES:
        if surface not in surfaces:
            continue
        patch = surfaces[surface]
        if not isinstance(patch, dict):
            report.rejected.append(
                f"room_finishes: {room_id!r} {surface} value is not an object"
            )
            continue

        current = getattr(room, f"{surface}_finish", None)
        if current is None:
            # Unobserved surfaces fall back to the graph-level finish; editing
            # one promotes it to a per-room override rather than changing every
            # room at once.
            current = _graph_finish(graph, surface)
        updated_finish = copy.deepcopy(current)

        if "material" in patch:
            material = _strict_material(patch["material"])
            if material is None:
                report.rejected.append(
                    f"room_finishes: {room_id!r} {surface} material "
                    f"{patch['material']!r} is not recognised"
                )
                continue
            updated_finish.material = material
            prior = catalog.MATERIALS.get(material)
            if prior is not None:
                # Roughness and metallic belong to the material, not the user's
                # taste, so they follow the new material unless overridden.
                updated_finish.roughness = prior.roughness
                updated_finish.metallic = prior.metallic

        if "color_hex" in patch:
            colour = _parse_hex(patch["color_hex"])
            if colour is None:
                report.rejected.append(
                    f"room_finishes: {room_id!r} {surface} color_hex "
                    f"{patch['color_hex']!r} is not a #RRGGBB colour"
                )
                continue
            updated_finish.color_hex = colour

        for key in ("roughness", "metallic"):
            if key in patch:
                try:
                    setattr(updated_finish, key, min(1.0, max(0.0, float(patch[key]))))
                except (TypeError, ValueError):
                    report.rejected.append(
                        f"room_finishes: {room_id!r} {surface} {key} is not a number"
                    )

        if "finish" in patch:
            updated_finish.finish = str(patch["finish"])[:40]

        updated_finish.confidence = 1.0  # stated by a human
        updated_finish.description = "set in the review step"
        setattr(room, f"{surface}_finish", updated_finish)
        changed = True

    if "ceiling_type" in surfaces:
        ceiling_type = str(surfaces["ceiling_type"]).strip().lower().replace(" ", "_")
        if ceiling_type not in catalog.CEILING_TYPES:
            report.rejected.append(
                f"room_finishes: {surfaces['ceiling_type']!r} is not a known ceiling type"
            )
        else:
            room.ceiling_type = ceiling_type
            changed = True

    if changed:
        report.finishes_changed.append(room_id)


def _graph_finish(graph: SceneGraph, surface: str):
    """The scene-wide finish a room falls back to for an unobserved surface."""
    if surface == "floor":
        return graph.floor
    if surface == "ceiling":
        return graph.ceiling
    return graph.walls[0].finish if graph.walls else Finish()


def _apply_light_override(
    graph: SceneGraph, light_id: str, changes: Any, report: EditReport
) -> None:
    """Edit one luminaire's kind, placement or emission."""
    light = next((x for x in graph.lights if x.id == light_id), None)
    if light is None:
        report.rejected.append(f"light_overrides: unknown id {light_id!r}")
        return
    if not isinstance(changes, dict):
        report.rejected.append(f"light_overrides: {light_id!r} value is not an object")
        return

    for key in sorted(set(changes) - LIGHT_OVERRIDE_KEYS):
        report.rejected.append(
            f"light_overrides: {light_id!r} has unrecognised key {key!r}"
        )

    changed = False

    if "kind" in changes:
        kind = catalog.normalise_light_kind(str(changes["kind"]))
        if kind is None:
            report.rejected.append(
                f"light_overrides: {light_id!r} kind {changes['kind']!r} "
                "is not a recognised luminaire"
            )
        else:
            light.kind = kind
            prior = catalog.LIGHT_TYPES.get(kind)
            if prior is not None:
                light.mounting = prior.mounting
            changed = True

    if "position" in changes:
        point = _parse_xy(changes["position"])
        height = None
        if isinstance(changes["position"], dict) and "z" in changes["position"]:
            try:
                height = float(changes["position"]["z"])
            except (TypeError, ValueError):
                height = None
        if point is None:
            report.rejected.append(
                f"light_overrides: {light_id!r} position is not a point"
            )
        else:
            room = graph.room_by_id(light.room_id)
            ceiling = room.ceiling_height if room else 3.0
            z = light.position.z if height is None else min(max(0.0, height), ceiling)
            light.position = Vec3(point[0], point[1], z)
            changed = True

    if "color_temperature_k" in changes:
        try:
            kelvin = float(changes["color_temperature_k"])
        except (TypeError, ValueError):
            report.rejected.append(
                f"light_overrides: {light_id!r} color_temperature_k is not a number"
            )
        else:
            light.color_temperature_k = min(
                MAX_COLOR_TEMPERATURE_K, max(MIN_COLOR_TEMPERATURE_K, kelvin)
            )
            changed = True

    if "power_w" in changes:
        try:
            power = float(changes["power_w"])
        except (TypeError, ValueError):
            report.rejected.append(
                f"light_overrides: {light_id!r} power_w is not a number"
            )
        else:
            light.power_w = min(MAX_POWER_W, max(0.0, power))
            changed = True

    for key in ("size", "length"):
        if key in changes:
            try:
                setattr(light, key, min(20.0, max(0.0, float(changes[key]))))
                changed = True
            except (TypeError, ValueError):
                report.rejected.append(
                    f"light_overrides: {light_id!r} {key} is not a number"
                )

    if "mounting" in changes:
        mounting = str(changes["mounting"]).strip().lower()
        if mounting not in ("floor", "wall", "ceiling", "table"):
            report.rejected.append(
                f"light_overrides: {light_id!r} mounting {changes['mounting']!r} "
                "is not floor, wall, ceiling or table"
            )
        else:
            light.mounting = mounting
            changed = True

    if changed:
        light.uncertain = False
        if light_id not in report.lights_changed:
            report.lights_changed.append(light_id)


def _add_light(graph: SceneGraph, spec: Any, report: EditReport) -> None:
    """Insert a luminaire the user asked for."""
    if not isinstance(spec, dict):
        report.rejected.append("add_lights: entry is not an object")
        return

    kind = catalog.normalise_light_kind(str(spec.get("kind", "ceiling_light")))
    if kind is None:
        report.rejected.append(
            f"add_lights: {spec.get('kind')!r} is not a recognised luminaire"
        )
        return

    room_id = str(spec.get("room_id", ""))
    room = graph.room_by_id(room_id)
    if room is None:
        report.rejected.append(f"add_lights: room {room_id!r} does not exist")
        return

    prior = catalog.LIGHT_TYPES.get(kind)
    taken = {x.id for x in graph.lights}
    index = 1
    while f"{kind}_user_{index}" in taken:
        index += 1

    centre = g2.polygon_centroid(room.polygon) if room.polygon else (0.0, 0.0)
    light = LightSource(
        id=f"{kind}_user_{index}",
        kind=kind,
        room_id=room_id,
        mounting=prior.mounting if prior else "ceiling",
        position=Vec3(centre[0], centre[1], room.ceiling_height),
        confidence=1.0,
    )
    graph.lights.append(light)

    overrides = {k: v for k, v in spec.items() if k in LIGHT_OVERRIDE_KEYS}
    if overrides:
        _apply_light_override(graph, light.id, overrides, report)
        if light.id in report.lights_changed:
            report.lights_changed.remove(light.id)

    report.lights_added.append(light.id)


def _apply_transform(
    obj: SceneObject, graph: SceneGraph, changes: Dict[str, Any], report: EditReport
) -> None:
    """Move, rotate, resize or lock one object, if the result is buildable.

    The candidate state is assembled first and checked as a whole, so the
    object is only mutated once the final placement is known to be legal. A
    rejected edit therefore leaves the object exactly as it was rather than
    half-applied.
    """
    wanted = [key for key in ("position", "rotation_z", "dimensions") if key in changes]
    lock_requested = "locked" in changes

    if not wanted and not lock_requested:
        return

    # A locked object is pinned unless the same edit also unlocks it — that
    # ordering lets the UI offer "unlock and drag" as one gesture.
    if obj.locked and wanted and bool(changes.get("locked", True)):
        report.rejected.append(
            f"object_overrides: {obj.id!r} is locked; unlock it before transforming it"
        )
        wanted = []

    if lock_requested:
        locked = bool(changes["locked"])
        if locked != obj.locked:
            obj.locked = locked
            obj.flags.append("locked_by_user" if locked else "unlocked_by_user")
            report.lock_changed.append(obj.id)

    if not wanted:
        return

    # --- Assemble the candidate -------------------------------------------
    position = Vec3(obj.position.x, obj.position.y, obj.position.z)
    rotation = obj.rotation_z
    dimensions = Dimensions(
        obj.dimensions.width, obj.dimensions.depth, obj.dimensions.height
    )

    if "position" in changes:
        point = _parse_xy(changes["position"])
        if point is None:
            report.rejected.append(
                f"object_overrides: {obj.id!r} position must be "
                "{'x': float, 'y': float} or [x, y]"
            )
            return
        position = Vec3(point[0], point[1], position.z)

    if "rotation_z" in changes:
        try:
            # Normalised so the stored value stays readable after many turns.
            rotation = float(changes["rotation_z"]) % 360.0
        except (TypeError, ValueError):
            report.rejected.append(
                f"object_overrides: {obj.id!r} rotation_z is not a number"
            )
            return

    if "dimensions" in changes:
        parsed = _parse_dimensions(changes["dimensions"], dimensions)
        if parsed is None:
            report.rejected.append(
                f"object_overrides: {obj.id!r} dimensions must be an object of numbers"
            )
            return
        dimensions = parsed

    # --- Judge it ----------------------------------------------------------
    errors, warnings = _check_placement(obj, graph, position, rotation, dimensions)
    if errors:
        for error in errors:
            report.rejected.append(f"object_overrides: {obj.id!r} {error}")
        return

    # --- Commit ------------------------------------------------------------
    delta = (position.x - obj.position.x, position.y - obj.position.y)
    delta_rotation = rotation - obj.rotation_z
    pivot = (obj.position.x, obj.position.y)

    obj.position = position
    obj.rotation_z = rotation
    obj.dimensions = dimensions
    # A hand-placed object is no longer a guess, so it stops being withheld for
    # low confidence — the user has taken responsibility for it.
    if obj.uncertain:
        obj.uncertain = False
        obj.flags.append("confirmed_by_user")
        if obj.id not in report.kept:
            report.kept.append(obj.id)
    for key in wanted:
        obj.flags.append(f"{key}_set_by_user")
    report.transformed.append(obj.id)
    report.warnings.extend(warnings)

    _cascade_transform(graph, obj, delta, delta_rotation, pivot)


def _parse_xy(value: Any) -> Optional[Tuple[float, float]]:
    """Read a plan-space point from either ``{"x": .., "y": ..}`` or ``[x, y]``."""
    try:
        if isinstance(value, dict):
            return float(value["x"]), float(value["y"])
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _parse_dimensions(value: Any, current: Dimensions) -> Optional[Dimensions]:
    """Read a partial dimensions patch, leaving unmentioned axes untouched."""
    if not isinstance(value, dict):
        return None
    try:
        return Dimensions(
            float(value.get("width", current.width)),
            float(value.get("depth", current.depth)),
            float(value.get("height", current.height)),
        )
    except (TypeError, ValueError):
        return None


def _check_placement(
    obj: SceneObject,
    graph: SceneGraph,
    position: Vec3,
    rotation: float,
    dimensions: Dimensions,
) -> Tuple[List[str], List[str]]:
    """Return ``(errors, warnings)`` for a proposed placement.

    Errors block the edit because the generator could not build the result.
    Warnings do not: a user who deliberately overlaps two objects is allowed
    to, and is told rather than overruled.
    """
    errors: List[str] = []
    warnings: List[str] = []

    for axis in ("width", "depth", "height"):
        extent = getattr(dimensions, axis)
        if not math.isfinite(extent) or not (MIN_DIMENSION <= extent <= MAX_DIMENSION):
            errors.append(
                f"{axis} {extent:g} m is outside the buildable range "
                f"{MIN_DIMENSION}–{MAX_DIMENSION} m"
            )

    if not (math.isfinite(position.x) and math.isfinite(position.y)):
        errors.append("position is not a finite point")
    if errors:
        return errors, warnings

    room = graph.room_by_id(obj.room_id) if obj.room_id else None
    if room is not None and room.polygon:
        # Wall- and ceiling-mounted objects sit on the boundary by definition,
        # and anything on a surface follows its parent, so only free-standing
        # objects are held to the room polygon.
        if obj.support == "floor":
            if not g2.point_in_polygon((position.x, position.y), room.polygon):
                errors.append("would sit outside its room")
            elif dimensions.footprint_area > MAX_FOOTPRINT_SHARE * room.area:
                errors.append(
                    f"footprint {dimensions.footprint_area:.1f} m² exceeds "
                    f"{MAX_FOOTPRINT_SHARE:.0%} of the {room.area:.1f} m² room"
                )

    if errors:
        return errors, warnings

    candidate = _footprint(position, rotation, dimensions)
    top = position.z + dimensions.height
    for other in graph.objects:
        if other.id == obj.id or other.room_id != obj.room_id:
            continue
        if other.dimensions.is_degenerate() or _is_support_pair(obj, other):
            continue
        # _vertical_overlap reads the live object, which still holds the old
        # height, so the candidate's own span is compared explicitly.
        other_top = other.position.z + other.dimensions.height
        if top <= other.position.z or other_top <= position.z:
            continue
        penetration = g2.rect_overlap(candidate, other.footprint_corners())
        if penetration > OVERLAP_TOLERANCE:
            warnings.append(
                f"{obj.id} now overlaps {other.id} ({other.category}) "
                f"by {penetration:.2f} m"
            )

    return errors, warnings


def _footprint(
    position: Vec3, rotation: float, dimensions: Dimensions
) -> List[Tuple[float, float]]:
    """Oriented floor corners of a candidate placement."""
    return g2.rect_corners(
        position.x, position.y, dimensions.width, dimensions.depth, rotation
    )


def _cascade_transform(
    graph: SceneGraph,
    parent: SceneObject,
    delta: Tuple[float, float],
    delta_rotation: float,
    pivot: Tuple[float, float],
) -> None:
    """Carry anything resting on ``parent`` along with it.

    The mirror of :func:`_cascade_supports`: moving a table without its lamp
    strands the lamp in mid-air just as deleting the table would. Children
    orbit the parent's old centre so a rotation takes them around with it, and
    their height is re-seated on the parent's new surface. Runs to a fixed
    point so a stack (tray on table, cup on tray) follows in full.
    """
    theta = math.radians(delta_rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    frontier = {parent.id}
    seen = {parent.id}

    while frontier:
        children = [
            o for o in graph.objects
            if o.support == "on_object" and o.support_id in frontier and o.id not in seen
        ]
        if not children:
            break

        for child in children:
            offset_x = child.position.x - pivot[0]
            offset_y = child.position.y - pivot[1]
            child.position = Vec3(
                pivot[0] + delta[0] + offset_x * cos_t - offset_y * sin_t,
                pivot[1] + delta[1] + offset_x * sin_t + offset_y * cos_t,
                child.position.z,
            )
            child.rotation_z = (child.rotation_z + delta_rotation) % 360.0

            support = graph.object_by_id(child.support_id)
            if support is not None:
                child.position = Vec3(
                    child.position.x, child.position.y, _surface_height(support)
                )
            child.flags.append(f"followed_{parent.id}")

        seen |= {child.id for child in children}
        frontier = {child.id for child in children}


def _cascade_supports(graph: SceneGraph, removed: set) -> List[str]:
    """Remove anything left resting on a deleted object.

    A vase whose table was deleted would otherwise hang in mid-air. Runs to a
    fixed point so a chain (vase on tray on table) clears fully.
    """
    cascaded: List[str] = []

    while True:
        orphans = [
            o.id for o in graph.objects
            if o.support == "on_object" and o.support_id and o.support_id in removed
        ]
        if not orphans:
            break
        removed |= set(orphans)
        cascaded.extend(orphans)
        graph.objects = [o for o in graph.objects if o.id not in set(orphans)]

    return sorted(cascaded)


def _move_to_room(obj: SceneObject, graph: SceneGraph, room_id: str) -> None:
    """Reassign an object to another room, repositioning it inside it.

    A room change must move the geometry too — leaving the object at its old
    coordinates would put it physically in the previous room while claiming to
    belong to the new one.
    """
    from . import geometry2d as g2
    from .schema import Vec3

    target = graph.room_by_id(room_id)
    obj.room_id = room_id
    if target is None or not target.polygon:
        return

    point = (obj.position.x, obj.position.y)
    if g2.point_in_polygon(point, target.polygon):
        return

    margin = max(obj.dimensions.width, obj.dimensions.depth) / 2.0 + 0.05
    moved = g2.shrink_polygon_to_bounds(point, target.polygon, margin)
    obj.position = Vec3(moved[0], moved[1], obj.position.z)
    obj.flags.append(f"repositioned_into_{room_id}")
