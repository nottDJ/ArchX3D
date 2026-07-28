"""
ArchX3D — Procedural furniture builders (Blender)
=================================================
Builds geometry for each object in the scene graph.

Runs inside Blender's bundled Python, so it may only import ``bpy``, ``bmesh``,
the standard library, and the stdlib-only vision modules (``schema``,
``catalog``, ``assets``, ``geometry2d``).

Approach
--------
Every item is assembled from primitives into a single mesh via one accumulating
`Part` builder, then emitted as one object. That keeps the object count (and
therefore the GLB size and the draw-call count in the web viewer) low, and
avoids per-part `bpy.ops` calls, which dominate runtime when placing dozens of
items.

Each builder receives the *target* metric dimensions from the scene graph and
proportions its parts to fill them, so an object is genuinely re-proportioned
rather than uniformly scaled. Builders construct at the origin with the
footprint centred on x/y, the base at z = 0, and the front facing +Y; the
caller then applies position and rotation.

Material slots, by convention:
    0 = primary surface   1 = frame / legs   2 = accent / secondary
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import bmesh
import bpy

Vector3 = Tuple[float, float, float]


# ---------------------------------------------------------------------------
# Mesh accumulation
# ---------------------------------------------------------------------------


class Part:
    """Accumulates primitives into one mesh with per-face material slots."""

    def __init__(self) -> None:
        self.bm = bmesh.new()
        self._slot_layer = self.bm.faces.layers.int.new("slot")

    def box(self, center: Vector3, size: Vector3, slot: int = 0) -> None:
        """Axis-aligned box, ``center`` is the volumetric centre."""
        sx, sy, sz = (max(1e-4, v) for v in size)
        matrix = _translation(center) @ _scale((sx, sy, sz))
        result = bmesh.ops.create_cube(self.bm, size=1.0, matrix=matrix)
        self._tag(result, slot)

    def box_base(self, center_xy: Tuple[float, float], size: Vector3, z0: float, slot: int = 0) -> None:
        """Box specified by its bottom face height rather than its centre."""
        self.box((center_xy[0], center_xy[1], z0 + size[2] / 2.0), size, slot)

    def cylinder(
        self, center: Vector3, radius: float, height: float, slot: int = 0, segments: int = 12
    ) -> None:
        matrix = _translation(center)
        result = bmesh.ops.create_cone(
            self.bm,
            cap_ends=True,
            cap_tris=False,
            segments=max(3, segments),
            radius1=max(1e-4, radius),
            radius2=max(1e-4, radius),
            depth=max(1e-4, height),
            matrix=matrix,
        )
        self._tag(result, slot)

    def cone(
        self, center: Vector3, radius_bottom: float, radius_top: float, height: float,
        slot: int = 0, segments: int = 12,
    ) -> None:
        result = bmesh.ops.create_cone(
            self.bm,
            cap_ends=True,
            cap_tris=False,
            segments=max(3, segments),
            radius1=max(1e-4, radius_bottom),
            radius2=max(1e-4, radius_top),
            depth=max(1e-4, height),
            matrix=_translation(center),
        )
        self._tag(result, slot)

    def sphere(self, center: Vector3, radius: float, slot: int = 0, subdivisions: int = 2) -> None:
        result = bmesh.ops.create_icosphere(
            self.bm,
            subdivisions=max(1, subdivisions),
            radius=max(1e-4, radius),
            matrix=_translation(center),
        )
        self._tag(result, slot)

    def _tag(self, result: Dict, slot: int) -> None:
        """Mark the faces just created with their material slot."""
        for element in result.get("verts", []):
            for face in element.link_faces:
                face[self._slot_layer] = slot

    def to_object(self, name: str, materials: Sequence[bpy.types.Material]) -> bpy.types.Object:
        mesh = bpy.data.meshes.new(f"{name}Mesh")

        # Transfer slot tags onto real material indices.
        slot_layer = self.bm.faces.layers.int.get("slot")
        face_slots = [f[slot_layer] if slot_layer else 0 for f in self.bm.faces]

        self.bm.to_mesh(mesh)
        self.bm.free()

        for material in materials:
            mesh.materials.append(material)

        limit = max(0, len(materials) - 1)
        for polygon, slot in zip(mesh.polygons, face_slots):
            polygon.material_index = min(slot, limit)

        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        return obj


def _translation(vector: Vector3):
    from mathutils import Matrix

    return Matrix.Translation(vector)


def _scale(vector: Vector3):
    from mathutils import Matrix

    matrix = Matrix.Identity(4)
    matrix[0][0], matrix[1][1], matrix[2][2] = vector
    return matrix


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
#
# Signature: build_x(part, w, d, h, params) -> None
# `w`/`d`/`h` are the target metric extents; `params` comes from the matched
# asset variant.


def build_box_furniture(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """Generic proportioned block — the fallback for anything unmodelled."""
    part.box_base((0, 0), (w, d, h), 0.0)


def build_sofa(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    leg_h = params.get("leg_height", 0.12) * h / 0.85
    arm_ratio = params.get("arm_height", 0.55)
    back_ratio = params.get("back_height", 0.78)

    arm_w = min(0.20, w * 0.12)
    back_d = min(0.18, d * 0.22)
    seat_h = h * 0.42

    # Seat block
    part.box_base((0, 0), (w, d - back_d, seat_h - leg_h), leg_h, slot=0)
    # Backrest, at the rear (−Y is the back; front faces +Y)
    part.box_base((0, -(d - back_d) / 2.0), (w, back_d, h * back_ratio), leg_h, slot=0)
    # Arms
    for sign in (-1.0, 1.0):
        part.box_base(
            (sign * (w - arm_w) / 2.0, back_d / 2.0),
            (arm_w, d - back_d, h * arm_ratio),
            leg_h,
            slot=0,
        )
    # Seat cushions, slightly proud of the seat block
    cushions = max(2, int(round(w / 0.75)))
    cushion_w = (w - arm_w * 2 - 0.04) / cushions
    for index in range(cushions):
        x = -(w - arm_w * 2) / 2.0 + cushion_w * (index + 0.5)
        part.box_base(
            (x, back_d / 2.0), (cushion_w * 0.94, (d - back_d) * 0.9, 0.10), seat_h, slot=2
        )
    _add_legs(part, w * 0.82, d * 0.72, leg_h, slot=1)


def build_sectional(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """L-shaped sofa: a main run plus a chaise along one end."""
    chaise_ratio = params.get("chaise_ratio", 0.6)
    back_ratio = params.get("back_height", 0.80)

    main_d = min(d * 0.55, 1.0)
    chaise_w = w * (1.0 - chaise_ratio)
    leg_h = 0.10
    seat_h = h * 0.42
    back_d = min(0.18, main_d * 0.25)

    # Main run along the front edge
    main_w = w - chaise_w
    main_x = -(w / 2.0) + main_w / 2.0
    main_y = (d / 2.0) - main_d / 2.0

    part.box_base((main_x, main_y), (main_w, main_d - back_d, seat_h - leg_h), leg_h, slot=0)
    part.box_base(
        (main_x, main_y - (main_d - back_d) / 2.0), (main_w, back_d, h * back_ratio), leg_h, slot=0
    )

    # Chaise extending back along +X
    chaise_x = (w / 2.0) - chaise_w / 2.0
    part.box_base((chaise_x, 0.0), (chaise_w - back_d, d, seat_h - leg_h), leg_h, slot=0)
    part.box_base(
        (chaise_x + (chaise_w - back_d) / 2.0, 0.0), (back_d, d, h * back_ratio), leg_h, slot=0
    )

    # Cushions on the main run
    cushions = max(2, int(round(main_w / 0.8)))
    cushion_w = main_w / cushions
    for index in range(cushions):
        x = main_x - main_w / 2.0 + cushion_w * (index + 0.5)
        part.box_base((x, main_y), (cushion_w * 0.94, (main_d - back_d) * 0.9, 0.10), seat_h, slot=2)
    part.box_base((chaise_x, 0.0), ((chaise_w - back_d) * 0.9, d * 0.92, 0.10), seat_h, slot=2)


def build_armchair(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    back_ratio = params.get("back_height", 1.0)
    leg_h = 0.12
    seat_h = h * 0.45
    arm_w = w * 0.14
    back_d = d * 0.18

    part.box_base((0, 0), (w, d - back_d, seat_h - leg_h), leg_h, slot=0)
    part.box_base((0, -(d - back_d) / 2.0), (w, back_d, h * 0.80 * back_ratio), leg_h, slot=0)
    for sign in (-1.0, 1.0):
        part.box_base(
            (sign * (w - arm_w) / 2.0, back_d / 2.0), (arm_w, d - back_d, h * 0.55), leg_h, slot=0
        )
    part.box_base((0, back_d / 2.0), (w * 0.72, (d - back_d) * 0.88, 0.09), seat_h, slot=2)
    _add_legs(part, w * 0.78, d * 0.72, leg_h, slot=1)


def build_chair(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    seat_h = h * 0.48
    leg_r = 0.022
    seat_t = 0.06 if not params.get("padded") else 0.10

    part.box_base((0, 0), (w, d, seat_t), seat_h - seat_t, slot=0)
    # Backrest at −Y
    part.box_base((0, -d / 2.0 + 0.03), (w * 0.92, 0.05, h - seat_h), seat_h, slot=0)
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            part.cylinder(
                (sx * (w / 2.0 - 0.05), sy * (d / 2.0 - 0.05), (seat_h - seat_t) / 2.0),
                leg_r, seat_h - seat_t, slot=1, segments=8,
            )


def build_office_chair(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    seat_h = h * 0.45
    part.box_base((0, 0), (w, d, 0.10), seat_h, slot=0)
    part.box_base((0, -d / 2.0 + 0.04), (w * 0.86, 0.07, h - seat_h - 0.10), seat_h + 0.10, slot=0)
    part.cylinder((0, 0, seat_h / 2.0), 0.035, seat_h, slot=1, segments=10)
    # Five-star base
    for index in range(5):
        angle = 2 * math.pi * index / 5.0
        part.box(
            (math.cos(angle) * w * 0.25, math.sin(angle) * w * 0.25, 0.03),
            (w * 0.5, 0.04, 0.05),
            slot=1,
        )


def build_stool(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    part.cylinder((0, 0, h - 0.04), max(w, d) / 2.0, 0.08, slot=0, segments=14)
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            part.cylinder(
                (sx * (w / 2.0 - 0.05), sy * (d / 2.0 - 0.05), (h - 0.08) / 2.0),
                0.02, h - 0.08, slot=1, segments=8,
            )


def build_table(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    top_t = params.get("top_thickness", 0.05)
    slab_legs = params.get("leg_style", 0.0) >= 1.0

    if params.get("round"):
        part.cylinder((0, 0, h - top_t / 2.0), max(w, d) / 2.0, top_t, slot=0, segments=20)
        part.cylinder((0, 0, (h - top_t) / 2.0), max(0.05, w * 0.09), h - top_t, slot=1, segments=12)
        part.cylinder((0, 0, 0.02), max(w, d) * 0.22, 0.04, slot=1, segments=16)
        return

    part.box_base((0, 0), (w, d, top_t), h - top_t, slot=0)

    if slab_legs:
        # Solid slab supports at each end.
        for sign in (-1.0, 1.0):
            part.box_base((sign * (w / 2.0 - 0.04), 0), (0.06, d * 0.86, h - top_t), 0.0, slot=1)
        return

    inset = min(0.08, w * 0.08)
    leg = 0.05
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            part.box_base(
                (sx * (w / 2.0 - inset), sy * (d / 2.0 - inset)), (leg, leg, h - top_t), 0.0, slot=1
            )


def build_tv_unit(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    plinth = 0.06
    part.box_base((0, 0), (w, d, h - plinth), plinth, slot=0)
    part.box_base((0, 0), (w * 0.9, d * 0.85, plinth), 0.0, slot=1)

    if params.get("open_shelf"):
        # Recessed niches read as an open media unit rather than a solid block.
        bays = max(2, int(round(w / 0.7)))
        bay_w = (w - 0.08) / bays
        for index in range(bays):
            x = -w / 2.0 + 0.04 + bay_w * (index + 0.5)
            part.box_base((x, d * 0.12), (bay_w * 0.86, d * 0.6, (h - plinth) * 0.42),
                          plinth + (h - plinth) * 0.16, slot=2)


def build_cabinet(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    plinth = min(0.08, h * 0.08)
    part.box_base((0, 0), (w, d, h - plinth), plinth, slot=0)
    part.box_base((0, 0), (w * 0.92, d * 0.9, plinth), 0.0, slot=1)

    # Door split line + handles, so it does not read as a plain box.
    doors = max(1, int(round(w / 0.55)))
    door_w = w / doors
    for index in range(doors):
        x = -w / 2.0 + door_w * (index + 0.5)
        part.box_base((x, d / 2.0), (door_w * 0.94, 0.015, (h - plinth) * 0.94),
                      plinth + (h - plinth) * 0.03, slot=2)
        part.cylinder((x + door_w * 0.34, d / 2.0 + 0.02, plinth + (h - plinth) * 0.55),
                      0.012, 0.10, slot=1, segments=8)


def build_bookshelf(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    board = 0.03
    part.box_base((0, 0), (w, d, board), 0.0, slot=0)
    part.box_base((0, 0), (w, d, board), h - board, slot=0)
    for sign in (-1.0, 1.0):
        part.box_base((sign * (w - board) / 2.0, 0), (board, d, h), 0.0, slot=0)
    part.box_base((0, -d / 2.0 + 0.01), (w, 0.015, h), 0.0, slot=2)

    shelves = max(2, int(h / 0.34))
    for index in range(1, shelves):
        part.box_base((0, 0), (w - board * 2, d, board), h * index / shelves, slot=0)


def build_shelf(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """Wall-mounted floating shelf (or a small stack of them)."""
    board = min(0.04, h if h < 0.06 else 0.04)
    if h <= 0.08:
        part.box_base((0, 0), (w, d, board), 0.0, slot=0)
        return
    count = max(2, int(h / 0.35))
    for index in range(count):
        part.box_base((0, 0), (w, d, board), h * index / max(1, count - 1) - board, slot=0)


def build_bed(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    headboard = params.get("headboard", 0.9)
    base_h = h * 0.42
    mattress_h = h * 0.36

    part.box_base((0, 0), (w, d, base_h), 0.0, slot=1)
    part.box_base((0, 0), (w * 0.98, d * 0.98, mattress_h), base_h, slot=0)
    # Headboard at −Y
    part.box_base((0, -d / 2.0 + 0.04), (w, 0.08, h * headboard), 0.0, slot=2)
    # Pillows
    for sign in (-1.0, 1.0):
        part.box_base(
            (sign * w * 0.22, -d / 2.0 + 0.36), (w * 0.38, 0.34, 0.12), base_h + mattress_h, slot=2
        )


def build_screen(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """A TV or monitor panel with a thin bezel."""
    part.box_base((0, 0), (w, max(0.03, d * 0.5), h), 0.0, slot=0)
    part.box_base((0, max(0.016, d * 0.28)), (w * 0.96, 0.008, h * 0.94), h * 0.03, slot=2)
    if d > 0.15:  # monitors have a stand; wall-mounted TVs do not
        part.box_base((0, 0), (w * 0.25, d * 0.7, 0.03), 0.0, slot=1)
        part.box_base((0, 0), (0.05, 0.05, h * 0.25), 0.0, slot=1)


def build_rug(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    part.box_base((0, 0), (w, d, max(0.008, h)), 0.001, slot=0)


def build_cushion(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    part.box_base((0, 0), (w, d, h), 0.0, slot=0)


def build_curtain(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """A pair of gathered drapes, approximated with vertical folds."""
    panel_w = w / 2.0 * 0.92
    folds = max(3, int(panel_w / 0.12))
    for sign in (-1.0, 1.0):
        for index in range(folds):
            x = sign * (w / 4.0) + (index - folds / 2.0 + 0.5) * (panel_w / folds)
            depth = d * (0.6 + 0.4 * (index % 2))
            part.box_base((x, 0), (panel_w / folds * 0.95, depth, h), 0.0, slot=0)


def build_blinds(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    slats = max(4, int(h / 0.08))
    for index in range(slats):
        part.box_base((0, 0), (w, d, 0.02), h * index / slats, slot=0)


def build_frame(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    """Picture frame: border in slot 1, canvas/glass in slot 0."""
    border = min(0.04, w * 0.06, h * 0.06)
    part.box_base((0, 0), (w, d * 0.5, h), 0.0, slot=1)
    part.box_base((0, d * 0.3), (w - border * 2, 0.006, h - border * 2), border, slot=0)


def build_clock(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    radius = max(w, h) / 2.0
    part.cylinder((0, 0, radius), radius, d * 0.6, slot=1, segments=20)
    part.cylinder((0, d * 0.35, radius), radius * 0.88, d * 0.2, slot=0, segments=20)


def build_plant(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    pot_h = h * 0.26
    pot_r = min(w, d) / 2.0 * 0.78
    part.cone((0, 0, pot_h / 2.0), pot_r * 0.72, pot_r, pot_h, slot=1, segments=14)
    part.cylinder((0, 0, pot_h + (h - pot_h) * 0.35), 0.022, (h - pot_h) * 0.7, slot=2, segments=6)

    # A few offset blobs read as foliage far more cheaply than real leaves.
    canopy_r = min(w, d) / 2.0
    for index in range(5):
        angle = 2 * math.pi * index / 5.0
        radius = canopy_r * (0.42 + 0.16 * (index % 3))
        part.sphere(
            (
                math.cos(angle) * canopy_r * 0.32,
                math.sin(angle) * canopy_r * 0.32,
                pot_h + (h - pot_h) * (0.55 + 0.12 * (index % 3)),
            ),
            radius,
            slot=0,
            subdivisions=2,
        )


def build_vase(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    radius = min(w, d) / 2.0
    part.cone((0, 0, h * 0.35), radius * 0.6, radius, h * 0.7, slot=0, segments=16)
    part.cone((0, 0, h * 0.85), radius, radius * 0.72, h * 0.3, slot=0, segments=16)


def build_ceiling_fan(part: Part, w: float, d: float, h: float, params: Dict) -> None:
    part.cylinder((0, 0, h - 0.04), 0.09, 0.08, slot=1, segments=12)
    part.cylinder((0, 0, h * 0.55), 0.025, h * 0.5, slot=1, segments=8)
    for index in range(4):
        angle = 2 * math.pi * index / 4.0
        part.box(
            (math.cos(angle) * w * 0.28, math.sin(angle) * w * 0.28, h - 0.06),
            (w * 0.46 if index % 2 == 0 else 0.12, 0.12 if index % 2 == 0 else w * 0.46, 0.015),
            slot=0,
        )


#: Builder lookup, keyed by the names `assets.AssetVariant.builder` uses.
BUILDERS = {
    "build_box_furniture": build_box_furniture,
    "build_sofa": build_sofa,
    "build_sectional": build_sectional,
    "build_armchair": build_armchair,
    "build_chair": build_chair,
    "build_office_chair": build_office_chair,
    "build_stool": build_stool,
    "build_table": build_table,
    "build_tv_unit": build_tv_unit,
    "build_cabinet": build_cabinet,
    "build_bookshelf": build_bookshelf,
    "build_shelf": build_shelf,
    "build_bed": build_bed,
    "build_screen": build_screen,
    "build_rug": build_rug,
    "build_cushion": build_cushion,
    "build_curtain": build_curtain,
    "build_blinds": build_blinds,
    "build_frame": build_frame,
    "build_clock": build_clock,
    "build_plant": build_plant,
    "build_vase": build_vase,
    "build_ceiling_fan": build_ceiling_fan,
}


def _add_legs(part: Part, span_x: float, span_y: float, height: float, slot: int = 1) -> None:
    if height <= 0.01:
        return
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            part.cylinder(
                (sx * span_x / 2.0, sy * span_y / 2.0, height / 2.0),
                0.022, height, slot=slot, segments=8,
            )


# ---------------------------------------------------------------------------
# Object construction
# ---------------------------------------------------------------------------


def build_object(scene_object, materials: Sequence[bpy.types.Material], builder_name: str,
                 params: Optional[Dict] = None) -> Optional[bpy.types.Object]:
    """Construct one scene-graph object and place it in the world.

    Returns the created Blender object, or ``None`` if the builder failed —
    a single bad item must not abort the whole scene.
    """
    builder = BUILDERS.get(builder_name, build_box_furniture)
    dimensions = scene_object.dimensions

    part = Part()
    try:
        builder(part, dimensions.width, dimensions.depth, dimensions.height, params or {})
    except Exception as exc:  # noqa: BLE001 - reported, then fall back
        print(f"[FURNITURE] ! {scene_object.id} builder {builder_name} failed: {exc}")
        try:
            part.bm.free()
        except Exception:
            pass
        part = Part()
        build_box_furniture(part, dimensions.width, dimensions.depth, dimensions.height, {})

    obj = part.to_object(f"{scene_object.category}_{scene_object.id}", materials)

    obj.location = (scene_object.position.x, scene_object.position.y, scene_object.position.z)
    obj.rotation_euler = (0.0, 0.0, math.radians(scene_object.rotation_z))

    # Keep provenance queryable inside the .blend for debugging.
    obj["archx3d_id"] = scene_object.id
    obj["archx3d_category"] = scene_object.category
    obj["archx3d_confidence"] = round(scene_object.confidence, 3)
    obj["archx3d_asset"] = scene_object.asset
    if scene_object.uncertain:
        obj["archx3d_uncertain"] = True

    return obj
