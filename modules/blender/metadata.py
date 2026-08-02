"""
ArchX3D — Semantic metadata for the exported GLB
================================================
Stamps every exported object with what it *is*, so the web viewer can hide the
roof, isolate the structure, or fly to a room without guessing.

Why this exists
---------------
A glTF file is a bag of meshes. The viewer needs to answer questions the
geometry cannot: which of these 340 meshes is the roof? which are furniture?
where is the kitchen? Without an answer it has to infer from mesh names and
bounding boxes, which works until someone renames a builder.

So the generator — which knows all of this exactly, because it just built the
scene from a scene graph — writes it down. The viewer reads it when present and
falls back to inference when it is absent, so an older GLB still opens.

What this does *not* do
-----------------------
It creates no geometry, changes no material, moves nothing and alters no
lighting. It writes custom properties onto objects that already exist, and one
JSON blob onto the scene. Removing this module entirely would change the
rendered image not at all.

Custom properties become glTF ``extras`` only when the exporter is called with
``export_extras=True``; see ``blender_generator.export_scene``.

The vocabulary
--------------
``archx3d_kind`` is a closed set. The viewer switches on it, so adding a value
without teaching the viewer about it produces an object nobody can hide.

Coordinates in the scene manifest
---------------------------------
Room bounds and polygons are in **Blender plan metres** — the same frame as
``geometry.json`` and the scene graph, +Z up. The glTF exporter converts the
scene to Y-up on the way out, so a viewer reading these must apply the same
conversion: ``(x, y) → (x, -y)`` in the ground plane. ``up_axis`` in the
manifest records which convention the *file* ended up in, so the viewer never
has to assume.
"""

from __future__ import annotations

import json

try:  # pragma: no cover - only importable inside Blender
    import bpy
except ImportError:  # pragma: no cover
    bpy = None  # type: ignore[assignment]

#: Bump when the meaning of a field changes, so a viewer can refuse to
#: misinterpret an older file rather than rendering it wrongly.
METADATA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

ROOF = "roof"
WALL = "wall"
FLOOR = "floor"
OPENING = "opening"
STRUCTURE = "structure"
FURNITURE = "furniture"
DECOR = "decor"
APPLIANCE = "appliance"
LIGHT = "light"
UNKNOWN = "unknown"

KINDS = (
    ROOF, WALL, FLOOR, OPENING, STRUCTURE,
    FURNITURE, DECOR, APPLIANCE, LIGHT, UNKNOWN,
)

#: Exact object names the generator gives the architectural shell. Matched on
#: the whole name rather than a substring: ``ceiling_fan_1`` is a fan, not a
#: ceiling, and a substring test would hide the fan with the roof.
_SHELL_NAMES = {
    "walls": WALL,
    "floor": FLOOR,
    "ceiling": ROOF,
    "roof": ROOF,
}

#: Prefixes used by the scene-graph builders, in ``prefix_id`` form.
_PREFIXES = (
    ("arch_", STRUCTURE),
    ("light_", LIGHT),
    ("cutter_", OPENING),
)

#: Light objects created by the lighting rigs, which have no scene-graph id.
_LIGHT_NAMES = {
    "sun_daylight", "key_sun", "fill_area",
    "keylight", "filllight", "rimlight",
}

#: ``SceneObject.group`` values map straight onto kinds; anything unrecognised
#: falls back to furniture, which is the group the catalogue defaults to.
_GROUP_TO_KIND = {
    "furniture": FURNITURE,
    "decor": DECOR,
    "appliance": APPLIANCE,
}


def classify(obj) -> str:
    """Return the semantic kind of one Blender object.

    Checks in order of reliability: an explicit group written by the furniture
    builder, then the object's data type, then its name. Guessing from the name
    is the last resort precisely because it is the thing that breaks silently
    when a builder is renamed.
    """
    group = obj.get("archx3d_group")
    if isinstance(group, str) and group:
        return _GROUP_TO_KIND.get(group, FURNITURE)

    # Anything carrying a catalogue category came from the furniture builder,
    # whatever it is called.
    if obj.get("archx3d_category"):
        return FURNITURE

    if getattr(obj, "type", None) == "LIGHT":
        return LIGHT

    name = (obj.name or "").strip().lower()

    if name in _SHELL_NAMES:
        return _SHELL_NAMES[name]
    if name in _LIGHT_NAMES:
        return LIGHT

    for prefix, kind in _PREFIXES:
        if name.startswith(prefix):
            return kind

    # Blender de-duplicates names with a ``.001`` suffix; a second floor plane
    # is still a floor.
    base = name.split(".")[0]
    if base in _SHELL_NAMES:
        return _SHELL_NAMES[base]
    if base in _LIGHT_NAMES:
        return LIGHT

    return UNKNOWN


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


def tag_objects(graph=None) -> dict:
    """Write ``archx3d_kind`` onto every object in the scene.

    Returns a count per kind, so the caller can print what it tagged and a
    scene full of ``unknown`` is visible rather than silent.
    """
    if bpy is None:  # pragma: no cover
        return {}

    room_of = {}
    if graph is not None:
        room_of = {o.id: o.room_id for o in graph.objects if o.room_id}
        room_of.update({lt.id: lt.room_id for lt in graph.lights if lt.room_id})

    counts: dict = {}
    for obj in bpy.data.objects:
        kind = classify(obj)
        obj["archx3d_kind"] = kind

        # Carry the room through so the viewer can isolate one space, and so
        # "fly to the kitchen" can highlight what is actually in it.
        object_id = obj.get("archx3d_id")
        if isinstance(object_id, str) and object_id in room_of:
            obj["archx3d_room"] = room_of[object_id]

        counts[kind] = counts.get(kind, 0) + 1

    return counts


def scene_manifest(graph=None, config=None) -> dict:
    """The scene-level block describing the building as a whole.

    Kept small on purpose. It is not a second copy of the scene graph — it is
    the handful of facts a viewer needs that it cannot read off the meshes:
    which rooms exist, where they are, and how tall they are.
    """
    manifest = {
        "version": METADATA_VERSION,
        "generator": "archx3d",
        # The glTF exporter converts Blender's +Z up to glTF's +Y up, so this
        # describes the file the viewer is holding, not the Blender scene.
        "up_axis": "Y",
        "units": "metre",
        "rooms": [],
    }

    if graph is None:
        return manifest

    for room in graph.rooms:
        manifest["rooms"].append({
            "id": room.id,
            "name": (room.room_type or "room").replace("_", " ").title(),
            "room_type": room.room_type,
            "style": room.style,
            "area_m2": round(room.area, 2),
            "ceiling_height": round(room.ceiling_height, 3),
            # Plan metres, +Z up — the viewer applies (x, y) -> (x, -y).
            "bounds_min": [round(room.bounds_min[0], 4), round(room.bounds_min[1], 4)],
            "bounds_max": [round(room.bounds_max[0], 4), round(room.bounds_max[1], 4)],
            "polygon": [[round(p[0], 4), round(p[1], 4)] for p in room.polygon],
            "connected_to": list(room.connected_to),
            "object_count": len([o for o in graph.objects if o.room_id == room.id]),
        })

    return manifest


def tag_scene(graph=None, config=None) -> dict:
    """Stamp the whole scene: every object, plus the scene-level manifest.

    Call immediately before export. Returns the per-kind counts.
    """
    if bpy is None:  # pragma: no cover
        return {}

    counts = tag_objects(graph)

    # Blender custom properties hold scalars and strings, not nested
    # structures, so the manifest travels as a JSON string. The viewer parses
    # it; anything that cannot is expected to ignore it.
    bpy.context.scene["archx3d"] = json.dumps(scene_manifest(graph, config))
    bpy.context.scene["archx3d_version"] = METADATA_VERSION

    return counts


def summarise(counts: dict) -> str:
    """One line naming what was tagged, most numerous first."""
    if not counts:
        return "nothing tagged"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{count} {kind}" for kind, count in ordered)
