"""
ArchX3D — Procedural Furnishing
===============================
Turns a semantically-typed scene graph into a furnished one.

Where this sits
---------------
This is Stage 7. It consumes the scene graph and nothing else — not the DXF,
not the reference images. That is the point: by the time furnishing runs, the
building has already been understood, and every decision here follows from the
room's *type*, *size* and *shape* rather than from re-reading the inputs.

The rule it obeys
-----------------
Procedural furniture is a design decision, not an observation. So:

* a room that already has observed objects is left alone entirely — an
  observation always beats a convention;
* everything generated is flagged ``procedural`` and carries a confidence
  reflecting convention rather than evidence;
* nothing that could not be placed is quietly forgotten; every rejection is
  reported with its reason.

Style still comes from the imagery. Colour and material for each item are
taken from the room's palette when the vision layer supplied one, so a
procedurally furnished room still looks like the reference photographs even
though its layout was not derived from them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import placement as place
from . import programme as prog

#: Confidence assigned to procedurally generated items.
#:
#: Deliberately below ``ConfidencePolicy.ACCEPT`` would be wrong — these are
#: not uncertain *detections*, they are certain *decisions*, and flagging them
#: uncertain would cause the generator to withhold them. The value states
#: "this is a convention we are confident in", and the ``procedural`` flag is
#: what distinguishes it from evidence.
PROCEDURAL_CONFIDENCE = 0.70

#: Distance kept between furniture and the room outline, metres. Absorbs the
#: raster segmentation's boundary error as well as wall thickness.
WALL_MARGIN_M = 0.12


@dataclass
class RoomFurnishing:
    """What was generated for one room."""

    room_id: str
    room_type: str
    placed: List[str] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    lights: List[str] = field(default_factory=list)
    skipped_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_type": self.room_type,
            "placed": list(self.placed),
            "rejected": [{"category": c, "reason": r} for c, r in self.rejected],
            "lights": list(self.lights),
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class FurnishReport:
    """Everything procedural furnishing did, for diagnostics."""

    rooms: List[RoomFurnishing] = field(default_factory=list)
    objects_created: int = 0
    lights_created: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objects_created": self.objects_created,
            "lights_created": self.lights_created,
            "rooms_furnished": sum(1 for r in self.rooms if r.placed),
            "rooms_skipped": sum(1 for r in self.rooms if r.skipped_reason),
            "total_rejections": sum(len(r.rejected) for r in self.rooms),
            "rooms": [r.to_dict() for r in self.rooms],
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def furnish(graph, *, overwrite: bool = False, log=print) -> FurnishReport:
    """Furnish every room in ``graph`` that has no observed contents.

    Mutates ``graph`` in place, appending to ``objects`` and ``lights``.
    Returns a report describing what was done and what could not be.
    """
    from vision import catalog
    from vision.schema import (
        Dimensions, LightSource, SceneObject, Vec3,
    )

    report = FurnishReport()

    observed_rooms = {o.room_id for o in graph.objects if o.room_id}
    lit_rooms = {lt.room_id for lt in graph.lights if lt.room_id}

    for room in graph.rooms:
        record = RoomFurnishing(room_id=room.id, room_type=room.room_type)
        report.rooms.append(record)

        if room.id in observed_rooms and not overwrite:
            record.skipped_reason = (
                "room already has observed objects; observation beats convention"
            )
            continue

        space = _space_for(room, graph)
        if space is None:
            record.skipped_reason = "room outline too small or malformed to furnish"
            continue

        area = room.area or _polygon_area(space.polygon)
        planned = prog.plan_room(room.room_type, area)
        if not planned:
            # Two very different situations, and conflating them hides a real
            # defect: an unfurnishable *type* is by design, whereas a
            # furnishable type with too little floor almost always means
            # segmentation lost part of the room. Saying which is which points
            # at the actual problem.
            record.skipped_reason = _why_nothing_planned(room.room_type, area)
        else:
            _furnish_room(
                graph, room, space, planned, record, report,
                catalog, SceneObject, Dimensions, Vec3,
            )

        if room.id not in lit_rooms:
            _light_room(
                graph, room, space, record, report, catalog, LightSource, Vec3
            )

    log(f"[FURNISH] {report.objects_created} objects and {report.lights_created} "
        f"lights across {sum(1 for r in report.rooms if r.placed or r.lights)} rooms"
        + (f"; {sum(len(r.rejected) for r in report.rooms)} items would not fit"
           if any(r.rejected for r in report.rooms) else ""))

    for record in report.rooms:
        if record.placed:
            log(f"[FURNISH]   {record.room_id} ({record.room_type}): "
                + ", ".join(record.placed[:8])
                + (" ..." if len(record.placed) > 8 else ""))
        elif record.skipped_reason:
            log(f"[FURNISH]   {record.room_id} ({record.room_type}): "
                f"{record.skipped_reason}")

    return report


# ---------------------------------------------------------------------------
# Per-room furnishing
# ---------------------------------------------------------------------------


def _furnish_room(
    graph, room, space, planned, record, report,
    catalog, SceneObject, Dimensions, Vec3,
) -> None:
    """Place one room's programme and append the results to the graph."""
    solver = place.Solver(space)
    placed_by_category: Dict[str, place.Placement] = {}
    palette = getattr(room, "palette", None)
    created_objects: List[Any] = []
    created_relations: List[Any] = []

    for item in planned:
        prior = catalog.get_prior(item.category)
        if prior is None:
            record.rejected.append((item.category, "no metric prior in the catalog"))
            continue

        # A dependent item is meaningless without its carrier.
        if item.requires and item.requires not in placed_by_category:
            record.rejected.append((
                item.category,
                f"requires {item.requires}, which was not placed",
            ))
            continue

        dimensions = _dimensions_for(prior, space)

        if item.on_surface:
            carrier = placed_by_category.get(item.requires)
            if carrier is None:
                record.rejected.append((item.category, "no surface to stand on"))
                continue
            result = solver.place_on_surface(item.category, dimensions, carrier)
        else:
            near = None
            if item.relation_target and item.relation_target in placed_by_category:
                near = placed_by_category[item.relation_target].position
            result = solver.place(
                item.category, dimensions, prior.wall_affinity,
                wall_clearance=prior.wall_clearance,
                prefer_near=near,
            )

        if result is None:
            failure = next(
                (r.reason for r in reversed(solver.rejections)
                 if r.category == item.category),
                "could not be placed",
            )
            record.rejected.append((item.category, failure))
            continue

        placed_by_category.setdefault(item.category, result)

        base_z = 0.0
        if item.on_surface:
            carrier_prior = catalog.get_prior(item.requires)
            base_z = carrier_prior.surface_height if carrier_prior else 0.75

        obj = SceneObject(
            id=f"{room.id}__proc_{item.key}",
            category=item.category,
            label=f"{item.category.replace('_', ' ')} (generated)",
            group=prior.group,
            room_id=room.id,
            position=Vec3(result.position[0], result.position[1], base_z),
            rotation_z=result.rotation_z,
            dimensions=Dimensions(*dimensions),
            support="on_object" if item.on_surface else prior.support,
            support_id=(
                f"{room.id}__proc_{item.requires}_0" if item.on_surface else ""
            ),
            color_hex=_colour_for(item.category, prior.group, palette),
            material=_material_for(item.category),
            confidence=PROCEDURAL_CONFIDENCE,
            uncertain=False,
            flags=["procedural", f"placed: {result.reason}"],
            source_images=[],
            observation_count=0,
        )
        graph.objects.append(obj)
        record.placed.append(item.category)
        report.objects_created += 1

        if item.relation and item.relation_target in placed_by_category:
            graph.relationships.append(_relationship(
                obj.id,
                item.relation,
                f"{room.id}__proc_{item.relation_target}_0",
            ))
            created_relations.append(graph.relationships[-1])
            created_objects.append(obj)

    # Relations are enforced after the whole room is placed, not during it.
    # The solver treats every item independently, which is right for deciding
    # *whether* something fits but wrong for deciding where a dependent item
    # goes: it would leave bedside tables wherever a wall happened to be free
    # and scatter dining chairs around the perimeter instead of around the
    # table. `relations` already knows how to flank, surround and centre, so
    # the pair-wise arrangement is delegated to it.
    _apply_relations(graph, room, space, created_objects, created_relations)


def _apply_relations(graph, room, space, objects, relations_created) -> None:
    """Arrange dependent items around the things they belong to.

    Delegates to ``vision.relations``, which already implements the predicates
    (``beside``, ``surrounds``, ``centered_under``, ``faces``, ``on_top_of``)
    and the geometry for each. Duplicating that here would be a second, worse
    implementation of solved work.

    Every object in the room is passed as context, not only the dependent
    ones, because a relation resolves against its target and the target must
    be findable by id.
    """
    if not relations_created:
        return

    try:
        from vision import grounding, relations
        from vision.schema import Wall
    except ImportError:  # pragma: no cover - vision always present in practice
        return

    frame = grounding.RoomFrame(
        polygon=list(space.polygon),
        bounds_min=space.bounds_min,
        bounds_max=space.bounds_max,
        ceiling_height=space.ceiling_height,
        walls=[
            Wall(id=f"{room.id}_w{i}", start=start, end=end,
                 height=space.ceiling_height)
            for i, (start, end) in enumerate(space.walls)
        ],
    )

    room_objects = [o for o in graph.objects if o.room_id == room.id]
    relations.apply_relationships(room_objects, relations_created, frame)


def _light_room(
    graph, room, space, record, report, catalog, LightSource, Vec3
) -> None:
    """Give a room its luminaires.

    Runs even for rooms with no furniture programme: an unlit room renders
    black, and a staircase or store still needs a fitting.
    """
    area = room.area or _polygon_area(space.polygon)
    planned = prog.plan_lighting(room.room_type, area)
    if not planned:
        return

    placed_categories = set(record.placed)
    ceiling = room.ceiling_height or space.ceiling_height
    centroid = space.centroid

    # Ceiling fittings spread across the room rather than stacking at its
    # centre, so a long room is lit along its length.
    ceiling_items = [p for p in planned if p.requires == ""]
    positions = _spread(centroid, space, len(ceiling_items))

    index = 0
    for item in planned:
        if item.requires and item.requires not in placed_categories:
            continue

        prior = catalog.LIGHT_TYPES.get(item.kind)
        if prior is None:
            continue

        if prior.mounting == "ceiling":
            x, y = positions[min(index, len(positions) - 1)] if positions else centroid
            z = min(prior.default_height, max(0.5, ceiling - 0.1))
            index += 1
        elif prior.mounting == "wall":
            x, y = _wall_point(space)
            z = prior.default_height
        else:
            # Floor and table fittings sit beside their related furniture; the
            # centroid is a safe fallback that never lands inside a wall.
            x, y = centroid
            z = prior.default_height

        graph.lights.append(LightSource(
            id=f"{room.id}__proc_{item.key}",
            kind=item.kind,
            room_id=room.id,
            position=Vec3(x, y, z),
            mounting=prior.mounting,
            color_temperature_k=float(prior.cct_k),
            power_w=prior.power_w,
            size=prior.size,
            confidence=PROCEDURAL_CONFIDENCE,
            uncertain=False,
            source_images=[],
        ))
        record.lights.append(item.kind)
        report.lights_created += 1


# ---------------------------------------------------------------------------
# Geometry and appearance
# ---------------------------------------------------------------------------


def _space_for(room, graph) -> Optional[place.RoomSpace]:
    """Build the solver's view of a room from the scene graph."""
    polygon = [tuple(p) for p in (room.polygon or [])]
    if len(polygon) < 3:
        # Fall back to the bounding box when segmentation gave no outline.
        x0, y0 = room.bounds_min
        x1, y1 = room.bounds_max
        if x1 - x0 < 0.5 or y1 - y0 < 0.5:
            return None
        polygon = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    # Inset so furniture does not sit inside the wall faces.
    inner = place.shrink_polygon(polygon, WALL_MARGIN_M)
    if _polygon_area(inner) < 1.0:
        return None

    xs = [p[0] for p in inner]
    ys = [p[1] for p in inner]

    walls: List[Tuple[place.Point, place.Point]] = []
    for i in range(len(inner)):
        walls.append((inner[i], inner[(i + 1) % len(inner)]))

    doors = [
        ((o.position.x, o.position.y), o.width)
        for o in graph.openings
        if o.room_id == room.id and o.kind in ("door", "archway")
    ]
    windows = [
        ((o.position.x, o.position.y), o.width)
        for o in graph.openings
        if o.room_id == room.id and o.kind == "window"
    ]

    return place.RoomSpace(
        polygon=inner,
        bounds_min=(min(xs), min(ys)),
        bounds_max=(max(xs), max(ys)),
        ceiling_height=room.ceiling_height or 3.0,
        walls=walls,
        doors=doors,
        windows=windows,
    )


def _dimensions_for(prior, space: place.RoomSpace) -> Tuple[float, float, float]:
    """The item's metric size, shrunk if the room cannot take the typical one.

    A 2.1 m sofa in a 2.4 m room is not a sofa, it is a wall. Scaling down to
    the room's shortest usable run keeps small rooms furnished with plausibly
    sized pieces instead of rejecting everything.
    """
    width, depth, height = prior.typical

    usable = min(
        space.bounds_max[0] - space.bounds_min[0],
        space.bounds_max[1] - space.bounds_min[1],
    )
    if usable <= 0:
        return width, depth, height

    # Leave circulation beside the item.
    limit = max(0.4, usable - place.MIN_CIRCULATION_M)
    if width > limit:
        scale = limit / width
        (min_w, max_w), (min_d, _), (min_h, _) = prior.limits
        width = max(min_w, width * scale)
        depth = max(min_d, depth * scale)
        height = max(min_h, height * (0.5 + 0.5 * scale))

    return round(width, 3), round(depth, 3), round(height, 3)


#: Which palette role each object group draws its colour from. Keeps a
#: procedurally furnished room consistent with the reference imagery's mood
#: even though its layout came from convention.
_PALETTE_ROLE = {
    "furniture": "furniture",
    "casework": "furniture",
    "appliance": "secondary",
    "decor": "decor",
    "fixture": "secondary",
}

_DEFAULT_COLOURS = {
    "furniture": "#B5AFA5",
    "decor": "#9AA5AE",
    "appliance": "#C9CDD2",
    "fixture": "#D8D8D8",
}

_MATERIALS = {
    "sofa": "fabric", "sectional": "fabric", "armchair": "fabric",
    "bed": "fabric", "rug": "carpet", "curtains": "fabric",
    "coffee_table": "wood", "dining_table": "wood", "side_table": "wood",
    "study_table": "wood", "console_table": "wood", "bedside_table": "wood",
    "wardrobe": "wood", "bookshelf": "wood", "cabinet": "wood",
    "sideboard": "wood", "shelves": "wood", "dining_chair": "wood",
    "stool": "wood", "kitchen_counter": "stone", "kitchen_island": "stone",
    "refrigerator": "metal", "oven": "metal", "microwave": "metal",
    "washing_machine": "metal", "tv": "plastic", "monitor": "plastic",
    "mirror": "glass", "painting": "canvas", "plant": "foliage",
}


def _colour_for(category: str, group: str, palette) -> str:
    if palette is not None:
        role = _PALETTE_ROLE.get(group, "furniture")
        colour = getattr(palette, role, None)
        if isinstance(colour, str) and colour.startswith("#"):
            return colour
    return _DEFAULT_COLOURS.get(group, "#B5AFA5")


def _material_for(category: str) -> str:
    return _MATERIALS.get(category, "unknown")


def _relationship(subject: str, predicate: str, target: str):
    from vision.schema import Relationship

    return Relationship(
        subject=subject, predicate=predicate, object=target,
        confidence=PROCEDURAL_CONFIDENCE,
    )


def _spread(centroid: place.Point, space: place.RoomSpace, count: int) -> List[place.Point]:
    """``count`` points spread along the room's longer axis."""
    if count <= 0:
        return []
    if count == 1:
        return [centroid]

    x0, y0 = space.bounds_min
    x1, y1 = space.bounds_max
    horizontal = (x1 - x0) >= (y1 - y0)

    points: List[place.Point] = []
    for i in range(count):
        t = (i + 1) / (count + 1)
        if horizontal:
            candidate = (x0 + (x1 - x0) * t, centroid[1])
        else:
            candidate = (centroid[0], y0 + (y1 - y0) * t)
        # Never hang a light outside the room outline.
        if not place.point_in_polygon(candidate, space.polygon):
            candidate = centroid
        points.append(candidate)
    return points


def _wall_point(space: place.RoomSpace) -> place.Point:
    """A point just inside the longest wall, for wall-mounted fittings."""
    if not space.walls:
        return space.centroid

    start, end = max(space.walls, key=lambda w: math.dist(w[0], w[1]))
    mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    centroid = space.centroid
    # Nudge inward so the fitting is inside the room, not in the wall.
    dx, dy = centroid[0] - mid[0], centroid[1] - mid[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return mid
    return (mid[0] + dx / length * 0.1, mid[1] + dy / length * 0.1)


def _why_nothing_planned(room_type: str, area: float) -> str:
    """Explain an empty programme in terms a person can act on."""
    if room_type in prog.UNFURNISHED_TYPES:
        return f"room type {room_type!r} is deliberately left unfurnished"

    programme = prog.programme_for(room_type)
    if not programme:
        return f"no furniture programme is defined for room type {room_type!r}"

    smallest = min(item.min_area for item in programme)
    return (
        f"room measures {area:.1f} m2 but the smallest item in the "
        f"{room_type.replace('_', ' ')} programme needs {smallest:.1f} m2 "
        "- the room outline is probably incomplete"
    )


def _polygon_area(polygon: Sequence[place.Point]) -> float:
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for i, (x0, y0) in enumerate(polygon):
        x1, y1 = polygon[(i + 1) % len(polygon)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0
