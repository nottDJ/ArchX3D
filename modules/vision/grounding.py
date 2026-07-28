"""
ArchX3D — Metric grounding
==========================
Converts image-space observations into metric floor-plan coordinates.

The central idea
----------------
ArchX3D already knows the room *exactly* — walls, extents and ceiling height
come from the DXF. So the vision layer is never asked to solve the hard problem
("how big is this room and where is everything in 3D?"). It solves the much
easier one: "given a room I already have, where inside it does each observed
object sit?"

That reframing is where the accuracy comes from. Concretely:

1. A pinhole camera is fitted to the room using the model's horizon and
   field-of-view estimate, positioned to reproduce a plausible interior shot.
2. The **bottom edge** of each object's box is back-projected onto the floor
   plane (z = 0). Floor contact is the one image cue that maps directly to a
   plan position, and it needs no depth network.
3. Metric size comes from `catalog` priors modulated by the reported size
   bucket and the box's aspect ratio — never from model-supplied metres.
4. Wall-mounted objects are intersected against actual wall planes instead of
   the floor.
5. Anything that lands outside the room, or whose ray passes above the horizon
   (mathematically un-projectable), is clamped inside and flagged rather than
   silently accepted.

Every step degrades to an explicit, recorded fallback. Nothing here fails hard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog, geometry2d as g2
from .fusion import FusedLight, FusedObject
from .observe import ArchObservation, CameraObservation, OpeningObservation
from .schema import (
    ArchElement,
    BBox2D,
    Dimensions,
    Finish,
    LightSource,
    Opening,
    SceneObject,
    Vec3,
    Wall,
    clamp,
    normalise_hex,
)

#: Clearance kept between an object's footprint and the room boundary.
ROOM_MARGIN = 0.05

#: An object whose back-projected point lands further than this multiple of the
#: room diagonal is treated as un-projectable (the ray was near the horizon).
MAX_DEPTH_FACTOR = 1.6

#: A floor object within this distance of a wall is considered "against" it,
#: scaled by the category's wall affinity.
WALL_SNAP_BASE = 1.10


# ---------------------------------------------------------------------------
# Room frame
# ---------------------------------------------------------------------------


@dataclass
class RoomFrame:
    """The known-good geometry the vision layer places objects into."""

    polygon: List[Tuple[float, float]]
    bounds_min: Tuple[float, float]
    bounds_max: Tuple[float, float]
    ceiling_height: float
    walls: List[Wall] = field(default_factory=list)

    @property
    def center(self) -> Tuple[float, float]:
        return (
            (self.bounds_min[0] + self.bounds_max[0]) / 2.0,
            (self.bounds_min[1] + self.bounds_max[1]) / 2.0,
        )

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    @property
    def diagonal(self) -> float:
        return math.hypot(self.width, self.depth)

    def contains(self, point: Tuple[float, float]) -> bool:
        return g2.point_in_polygon(point, self.polygon)

    def clamp_inside(self, point: Tuple[float, float], margin: float = ROOM_MARGIN):
        return g2.shrink_polygon_to_bounds(point, self.polygon, margin)

    def nearest_wall(self, point: Tuple[float, float]) -> Tuple[Optional[Wall], float, Tuple[float, float]]:
        """Return the closest wall, the distance to it, and the contact point."""
        best: Tuple[Optional[Wall], float, Tuple[float, float]] = (None, float("inf"), point)
        for wall in self.walls:
            contact, distance = g2.closest_point_on_segment(point, wall.start, wall.end)
            if distance < best[1]:
                best = (wall, distance, contact)
        return best


def frame_from_region(region, all_walls: Sequence[Wall], ceiling_height: float) -> RoomFrame:
    """Build a `RoomFrame` for one segmented room.

    This is what confines an object to its own room: the frame carries only
    that room's polygon and bounding walls, so back-projection, wall snapping
    and clamping all operate inside the correct space. An object detected in a
    bedroom render is geometrically unable to land in the kitchen.
    """
    wanted = set(region.wall_ids)
    walls = [w for w in all_walls if w.id in wanted]

    if not walls:
        # Segmentation found the room but matched no wall segments to it;
        # synthesise its bounding box so placement still has something to
        # snap against.
        bmin, bmax = region.bounds_min, region.bounds_max
        corners = [
            (bmin[0], bmin[1]), (bmax[0], bmin[1]), (bmax[0], bmax[1]), (bmin[0], bmax[1])
        ]
        walls = [
            Wall(id=f"{region.id}_edge_{i}", start=corners[i],
                 end=corners[(i + 1) % 4], height=ceiling_height)
            for i in range(4)
        ]

    return RoomFrame(
        polygon=list(region.polygon),
        bounds_min=region.bounds_min,
        bounds_max=region.bounds_max,
        ceiling_height=ceiling_height,
        walls=walls,
    )


def ground_plan_view(
    entities: Sequence[FusedObject],
    plan_bounds_min: Tuple[float, float],
    plan_bounds_max: Tuple[float, float],
    room: RoomFrame,
) -> List[SceneObject]:
    """Place objects observed in a top-down furnished plan.

    A plan view needs no camera model at all: the image *is* the floor plane,
    so a normalised box maps linearly onto plan coordinates. That makes plan
    views the most positionally accurate input the pipeline accepts — more so
    than a perspective photograph, where depth has to be inferred.

    Image y runs downward while plan y runs upward, hence the flip.
    """
    width = plan_bounds_max[0] - plan_bounds_min[0]
    depth = plan_bounds_max[1] - plan_bounds_min[1]

    placed: List[SceneObject] = []

    for entity in entities:
        prior = catalog.get_prior(entity.category)
        if prior is None or entity.primary_bbox is None:
            continue

        dimensions, notes = resolve_dimensions(
            entity.category, entity.size_bucket, entity.primary_bbox, room
        )

        u, v = entity.primary_bbox.center
        x = plan_bounds_min[0] + u * width
        y = plan_bounds_max[1] - v * depth

        obj = SceneObject(
            id=entity.entity_id,
            category=entity.category,
            label=entity.label,
            group=entity.group,
            position=Vec3(x, y, 0.0),
            dimensions=dimensions,
            support=prior.support if prior.support != "on_object" else "floor",
            color_hex=entity.color_hex,
            material=entity.material if entity.material != "unknown"
            else _default_material(entity.category),
            confidence=entity.confidence,
            uncertain=entity.uncertain,
            bbox_2d=entity.primary_bbox,
            source_images=list(entity.source_images),
            observation_count=entity.observation_count,
            flags=list(notes) + ["placed_from_plan_view"],
        )

        # In a plan view the box's own aspect indicates which way the item
        # runs, which is a better orientation cue than any facing hint.
        if entity.primary_bbox.width < entity.primary_bbox.height:
            obj.rotation_z = 90.0

        placed.append(obj)

    return placed


def build_room_frame(geometry: Dict, ceiling_height: float) -> RoomFrame:
    """Derive a `RoomFrame` from the DXF-extracted ``geometry.json``.

    The floor region is taken as the bounding box of the wall segments. For the
    rectangular rooms this pipeline targets that is exact; for an L-shaped plan
    it over-covers, which is the safe direction — objects still get clamped by
    per-wall snapping and by validation.
    """
    walls_raw = geometry.get("walls") or []

    xs: List[float] = []
    ys: List[float] = []
    walls: List[Wall] = []

    for index, segment in enumerate(walls_raw):
        try:
            start = (float(segment["start"][0]), float(segment["start"][1]))
            end = (float(segment["end"][0]), float(segment["end"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        xs.extend([start[0], end[0]])
        ys.extend([start[1], end[1]])
        walls.append(
            Wall(id=f"wall_{index}", start=start, end=end, height=ceiling_height)
        )

    if not xs or not ys:
        # No usable geometry: fall back to a nominal room so the rest of the
        # pipeline can still run and report the problem.
        bounds_min, bounds_max = (0.0, 0.0), (5.0, 4.0)
    else:
        bounds_min, bounds_max = (min(xs), min(ys)), (max(xs), max(ys))

    polygon = [
        (bounds_min[0], bounds_min[1]),
        (bounds_max[0], bounds_min[1]),
        (bounds_max[0], bounds_max[1]),
        (bounds_min[0], bounds_max[1]),
    ]

    return RoomFrame(
        polygon=polygon,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        ceiling_height=ceiling_height,
        walls=walls,
    )


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------


@dataclass
class CameraPose:
    """A pinhole camera expressed in room coordinates."""

    x: float
    y: float
    height: float
    #: Heading in degrees, 0 = looking along +Y (matches `SceneObject.rotation_z`).
    yaw: float
    #: Positive tilts the view upward.
    pitch_deg: float
    vertical_fov_deg: float
    aspect: float

    @property
    def position(self) -> Tuple[float, float]:
        return (self.x, self.y)

    def forward(self) -> Tuple[float, float]:
        theta = math.radians(self.yaw)
        return (-math.sin(theta), math.cos(theta))

    def right(self) -> Tuple[float, float]:
        theta = math.radians(self.yaw)
        return (math.cos(theta), math.sin(theta))

    def ray(self, u: float, v: float) -> Tuple[float, float, float]:
        """World-space direction for a normalised image point (origin top-left)."""
        tan_half = math.tan(math.radians(self.vertical_fov_deg) / 2.0)

        x_cam = (u - 0.5) * 2.0 * tan_half * self.aspect
        z_cam = (0.5 - v) * 2.0 * tan_half
        y_cam = 1.0

        # Rotate about the camera's right axis to apply pitch.
        pitch = math.radians(self.pitch_deg)
        cos_p, sin_p = math.cos(pitch), math.sin(pitch)
        y_rot = y_cam * cos_p - z_cam * sin_p
        z_rot = y_cam * sin_p + z_cam * cos_p

        fx, fy = self.forward()
        rx, ry = self.right()

        return (rx * x_cam + fx * y_rot, ry * x_cam + fy * y_rot, z_rot)

    def intersect_floor(self, u: float, v: float) -> Optional[Tuple[float, float]]:
        """Where the ray through ``(u, v)`` meets the floor plane, if it does."""
        dx, dy, dz = self.ray(u, v)
        if dz > -1e-3:
            # At or above the horizon: no floor intersection exists.
            return None
        t = -self.height / dz
        return (self.x + t * dx, self.y + t * dy)

    def intersect_height(self, u: float, v: float, z: float) -> Optional[Tuple[float, float]]:
        """Where the ray meets a horizontal plane at height ``z``."""
        dx, dy, dz = self.ray(u, v)
        delta = z - self.height
        if abs(dz) < 1e-6 or (delta / dz) <= 0:
            return None
        t = delta / dz
        return (self.x + t * dx, self.y + t * dy)


def estimate_camera(room: RoomFrame, observation: CameraObservation, aspect: float = 16 / 9) -> CameraPose:
    """Fit a plausible camera for the reference shot.

    Interior photographs are overwhelmingly taken from near one end of a room
    looking down its long axis, because that is what fits the space in frame.
    That is the prior used here, refined by the model's horizon and FOV
    estimates. The camera is a *fitting device*, not a claim about where the
    photographer stood — what matters is that it reproduces consistent relative
    placement, which it does as long as every object in one image shares it.
    """
    width, depth = room.width, room.depth
    cx, cy = room.center

    # Stand back along whichever axis is longer.
    if depth >= width:
        yaw = 0.0  # looking +Y
        inset = min(0.9, depth * 0.12)
        position = (cx, room.bounds_min[1] + inset)
    else:
        yaw = 270.0  # looking +X
        inset = min(0.9, width * 0.12)
        position = (room.bounds_min[0] + inset, cy)

    # A corner shot is offset toward one side; anything else stays centred.
    if observation.facing_wall == "corner":
        if depth >= width:
            position = (room.bounds_min[0] + width * 0.22, position[1])
            yaw = 20.0
        else:
            position = (position[0], room.bounds_min[1] + depth * 0.22)
            yaw = 250.0

    fov_v = observation.vertical_fov_deg
    tan_half = math.tan(math.radians(fov_v) / 2.0)

    # The reported horizon fixes the pitch: a horizon above centre means the
    # camera is tilted down, which is the usual case for interior shots.
    pitch = -math.degrees(math.atan((0.5 - observation.horizon_y) * 2.0 * tan_half))

    return CameraPose(
        x=position[0],
        y=position[1],
        height=observation.eye_height_m,
        yaw=yaw,
        pitch_deg=clamp(pitch, -35.0, 25.0),
        vertical_fov_deg=fov_v,
        aspect=aspect,
    )


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def resolve_dimensions(
    category: str, size_bucket: str, bbox: Optional[BBox2D], room: RoomFrame
) -> Tuple[Dimensions, List[str]]:
    """Derive metric extents from priors, the size bucket and the box aspect.

    The model never supplies metres. The prior sets the scale, the bucket
    scales it, and the observed box aspect nudges the width-to-height ratio —
    damped, because a box's aspect also changes with viewing angle.
    """
    notes: List[str] = []
    prior = catalog.get_prior(category)
    if prior is None:
        return Dimensions(0.5, 0.5, 0.5), ["no prior for category"]

    multiplier = catalog.SIZE_BUCKETS.get(size_bucket, 1.0)
    width, depth, height = (value * multiplier for value in prior.typical)

    if bbox is not None and bbox.height > 0.02 and bbox.width > 0.005:
        expected_aspect = (prior.typical[0] / prior.typical[2]) if prior.typical[2] > 0 else 1.0
        observed_aspect = bbox.aspect
        if expected_aspect > 0 and observed_aspect > 0:
            ratio = clamp(observed_aspect / expected_aspect, 0.6, 1.7)
            # 50% blend: trust the prior as much as the observation.
            adjustment = 1.0 + (ratio - 1.0) * 0.5
            width *= adjustment
            if abs(adjustment - 1.0) > 0.12:
                notes.append(f"width adjusted x{adjustment:.2f} from box aspect")

    (w_lo, w_hi), (d_lo, d_hi), (h_lo, h_hi) = prior.limits
    width = clamp(width, w_lo, w_hi)
    depth = clamp(depth, d_lo, d_hi)
    height = clamp(height, h_lo, h_hi)

    # Nothing may exceed the room that contains it.
    max_width = max(0.2, room.width * 0.92)
    max_depth = max(0.2, room.depth * 0.92)
    if width > max_width:
        width = max_width
        notes.append("width clamped to room")
    if depth > max_depth:
        depth = max_depth
        notes.append("depth clamped to room")
    height = min(height, max(0.2, room.ceiling_height * 0.95))

    return Dimensions(width, depth, height), notes


# ---------------------------------------------------------------------------
# Object grounding
# ---------------------------------------------------------------------------


def ground_objects(
    fused: Sequence[FusedObject], room: RoomFrame, cameras: Dict[str, CameraPose]
) -> List[SceneObject]:
    """Place every fused object into the room, in metres."""
    placed: List[SceneObject] = []

    for entity in fused:
        camera = cameras.get(entity.primary_image) or next(iter(cameras.values()), None)
        obj = _ground_one(entity, room, camera)
        if obj is not None:
            placed.append(obj)

    return placed


def _ground_one(
    entity: FusedObject, room: RoomFrame, camera: Optional[CameraPose]
) -> Optional[SceneObject]:
    prior = catalog.get_prior(entity.category)
    if prior is None:
        return None

    dimensions, notes = resolve_dimensions(
        entity.category, entity.size_bucket, entity.primary_bbox, room
    )

    obj = SceneObject(
        id=entity.entity_id,
        category=entity.category,
        label=entity.label,
        group=entity.group,
        dimensions=dimensions,
        support=entity.support,
        color_hex=entity.color_hex,
        material=entity.material if entity.material != "unknown" else _default_material(entity.category),
        confidence=entity.confidence,
        uncertain=entity.uncertain,
        bbox_2d=entity.primary_bbox,
        source_images=list(entity.source_images),
        observation_count=entity.observation_count,
        flags=list(notes),
    )

    if entity.partially_visible:
        obj.flags.append("partially_visible")
    if entity.base_occluded:
        obj.flags.append("base_occluded")

    # Some categories are structurally fixed — a split AC unit, curtains, a
    # wall mirror. The model sometimes reports them as resting on whatever sits
    # below them in frame, or hanging from the ceiling. The catalog is the
    # better authority there, so the prior overrides the per-image guess and
    # the object is mounted properly instead of falling through to the floor.
    if catalog.support_is_fixed(entity.category) and entity.support != prior.support:
        obj.support = prior.support
        obj.flags.append(
            f"support corrected {entity.support} -> {prior.support} from catalog prior"
        )
        if prior.support == "wall":
            _place_on_wall(obj, entity, room, camera, prior)
        else:
            _place_on_ceiling(obj, entity, room, camera)
        return obj

    # Objects resting on other objects are positioned in relations.py, once
    # every supporting surface has a final location.
    if entity.support == "on_object":
        obj.position = Vec3(room.center[0], room.center[1], 0.0)
        obj.flags.append("awaiting_support_placement")
        return obj

    if entity.support == "wall":
        _place_on_wall(obj, entity, room, camera, prior)
        return obj

    if entity.support == "ceiling":
        _place_on_ceiling(obj, entity, room, camera)
        return obj

    _place_on_floor(obj, entity, room, camera, prior)
    return obj


def _place_on_floor(
    obj: SceneObject,
    entity: FusedObject,
    room: RoomFrame,
    camera: Optional[CameraPose],
    prior: catalog.ObjectPrior,
) -> None:
    """Back-project the box's bottom edge onto the floor plane."""
    point: Optional[Tuple[float, float]] = None

    if camera is not None and entity.primary_bbox is not None:
        u, _ = entity.primary_bbox.center
        v = entity.primary_bbox.y1  # bottom edge = floor contact
        candidate = camera.intersect_floor(u, v)

        if candidate is not None:
            distance = math.dist(camera.position, candidate)
            if distance <= room.diagonal * MAX_DEPTH_FACTOR:
                point = candidate
            else:
                obj.flags.append("depth_beyond_room_clamped")
        else:
            obj.flags.append("ray_above_horizon")

    if point is None:
        # Fall back to a wall-affine default rather than dropping the object.
        point = _fallback_position(room, prior)
        obj.flags.append("fallback_position")
        obj.confidence = round(obj.confidence * 0.85, 4)

    point = room.clamp_inside(point, margin=max(ROOM_MARGIN, min(obj.dimensions.depth, obj.dimensions.width) / 2.0))

    obj.position = Vec3(point[0], point[1], 0.0)
    obj.rotation_z = _initial_rotation(entity, room, camera, point, prior)

    _snap_to_wall(obj, room, prior)
    _record_distances(obj, room)


def _place_on_wall(
    obj: SceneObject,
    entity: FusedObject,
    room: RoomFrame,
    camera: Optional[CameraPose],
    prior: catalog.ObjectPrior,
) -> None:
    """Intersect the view ray with candidate wall planes."""
    wall = _select_wall(entity.on_wall, room, camera)

    hit: Optional[Tuple[float, float, float]] = None
    if camera is not None and entity.primary_bbox is not None and wall is not None:
        u, v = entity.primary_bbox.center
        hit = _intersect_wall_plane(camera, u, v, wall, room.ceiling_height)

    if hit is not None:
        x, y, z = hit
    elif wall is not None:
        # Centre it on the wall at the catalog's mounting height.
        x, y = wall.midpoint
        z = prior.mount_height
        obj.flags.append("wall_position_defaulted")
    else:
        x, y = room.center
        z = prior.mount_height
        obj.flags.append("no_wall_resolved")
        obj.confidence = round(obj.confidence * 0.8, 4)

    if wall is not None:
        obj.wall_id = wall.id
        obj.rotation_z = wall.inward_normal_deg(room.center)
        # Sit the object just proud of the wall face so it never z-fights.
        inset = obj.dimensions.depth / 2.0 + 0.01
        heading = math.radians(obj.rotation_z)
        x += -math.sin(heading) * inset
        y += math.cos(heading) * inset

    # Keep the fixture wholly between floor and ceiling.
    half_height = obj.dimensions.height / 2.0
    z = clamp(z, half_height + 0.02, room.ceiling_height - half_height - 0.02)

    obj.position = Vec3(x, y, z - half_height)
    _record_distances(obj, room)


def _place_on_ceiling(
    obj: SceneObject, entity: FusedObject, room: RoomFrame, camera: Optional[CameraPose]
) -> None:
    point: Optional[Tuple[float, float]] = None

    if camera is not None and entity.primary_bbox is not None:
        u, v = entity.primary_bbox.center
        point = camera.intersect_height(u, v, room.ceiling_height)
        if point is not None and math.dist(camera.position, point) > room.diagonal * MAX_DEPTH_FACTOR:
            point = None

    if point is None:
        point = room.center
        obj.flags.append("ceiling_position_defaulted")

    point = room.clamp_inside(point, margin=max(ROOM_MARGIN, obj.dimensions.width / 2.0))
    obj.position = Vec3(point[0], point[1], room.ceiling_height - obj.dimensions.height)
    _record_distances(obj, room)


# ---------------------------------------------------------------------------
# Orientation & snapping
# ---------------------------------------------------------------------------


def _initial_rotation(
    entity: FusedObject,
    room: RoomFrame,
    camera: Optional[CameraPose],
    point: Tuple[float, float],
    prior: catalog.ObjectPrior,
) -> float:
    """Best-guess heading before relationship constraints are applied."""
    if camera is None:
        return g2.heading_toward(point, room.center)

    facing = entity.facing
    if facing == "toward_camera":
        return (camera.yaw + 180.0) % 360.0
    if facing == "away_from_camera":
        return camera.yaw % 360.0
    if facing == "left":
        return (camera.yaw + 90.0) % 360.0
    if facing == "right":
        return (camera.yaw - 90.0) % 360.0

    # No usable hint: face the room centre, which is right far more often than
    # it is wrong for furniture arranged around a living space.
    if prior.orientation == "free":
        return 0.0
    return g2.heading_toward(point, room.center)


def _snap_to_wall(obj: SceneObject, room: RoomFrame, prior: catalog.ObjectPrior) -> None:
    """Push a wall-affine object flush against its nearest wall.

    Back-projection puts objects roughly right but rarely flush, and a sofa
    floating 30 cm off the wall reads immediately as wrong. Snapping is gated
    on the category's affinity so a dining table in open space is left alone.
    """
    if prior.wall_affinity < 0.4:
        return

    wall, distance, contact = room.nearest_wall((obj.position.x, obj.position.y))
    if wall is None:
        return

    threshold = WALL_SNAP_BASE * prior.wall_affinity + obj.dimensions.depth / 2.0
    if distance > threshold:
        return

    heading = wall.inward_normal_deg(room.center)
    offset = obj.dimensions.depth / 2.0 + prior.wall_clearance + wall.thickness / 2.0

    obj.position = Vec3(
        contact[0] - math.sin(math.radians(heading)) * offset,
        contact[1] + math.cos(math.radians(heading)) * offset,
        obj.position.z,
    )
    obj.wall_id = wall.id
    if prior.orientation in ("face_room", "face_target"):
        obj.rotation_z = heading
    obj.flags.append(f"snapped_to_{wall.id}")


def _record_distances(obj: SceneObject, room: RoomFrame) -> None:
    _, distance, _ = room.nearest_wall((obj.position.x, obj.position.y))
    obj.distance_to_nearest_wall = round(distance, 3)
    obj.distance_to_room_center = round(
        math.dist((obj.position.x, obj.position.y), room.center), 3
    )


def _fallback_position(room: RoomFrame, prior: catalog.ObjectPrior) -> Tuple[float, float]:
    """Where to put an object we could not project."""
    if prior.wall_affinity >= 0.6 and room.walls:
        longest = max(room.walls, key=lambda w: w.length)
        heading = math.radians(longest.inward_normal_deg(room.center))
        contact = longest.midpoint
        offset = prior.typical[1] / 2.0 + prior.wall_clearance
        return (
            contact[0] - math.sin(heading) * offset,
            contact[1] + math.cos(heading) * offset,
        )
    return room.center


def _select_wall(
    hint: str, room: RoomFrame, camera: Optional[CameraPose]
) -> Optional[Wall]:
    """Resolve a camera-relative wall label onto an actual wall segment."""
    if not room.walls:
        return None
    if camera is None:
        return max(room.walls, key=lambda w: w.length)

    offsets = {"back": 0.0, "left": 90.0, "right": -90.0, "front": 180.0}
    if hint not in offsets:
        # Unknown: the wall the camera looks at is the best default.
        hint = "back"

    target_heading = (camera.yaw + offsets[hint]) % 360.0

    # The wall we want faces back toward the camera, i.e. its inward normal is
    # opposite the direction the camera is looking at it.
    wanted_normal = (target_heading + 180.0) % 360.0

    best, best_score = None, float("inf")
    for wall in room.walls:
        delta = g2.angle_between_deg(wall.inward_normal_deg(room.center), wanted_normal)
        # Prefer long walls when several share an orientation.
        score = delta - min(wall.length, 6.0) * 2.0
        if score < best_score:
            best, best_score = wall, score

    return best


def _intersect_wall_plane(
    camera: CameraPose, u: float, v: float, wall: Wall, ceiling_height: float
) -> Optional[Tuple[float, float, float]]:
    """Intersect a view ray with the infinite plane containing ``wall``."""
    heading = math.radians(wall.angle_deg)
    normal = (-math.sin(heading), math.cos(heading))

    dx, dy, dz = camera.ray(u, v)
    denominator = dx * normal[0] + dy * normal[1]
    if abs(denominator) < 1e-6:
        return None  # Ray runs parallel to the wall.

    px, py = wall.start
    numerator = (px - camera.x) * normal[0] + (py - camera.y) * normal[1]
    t = numerator / denominator
    if t <= 0:
        return None  # Wall is behind the camera.

    x = camera.x + t * dx
    y = camera.y + t * dy
    z = camera.height + t * dz

    # Reject hits beyond the wall's actual extent or outside the room's height.
    _, distance = g2.closest_point_on_segment((x, y), wall.start, wall.end)
    if distance > 0.35:
        return None
    if not -0.1 <= z <= ceiling_height + 0.1:
        return None

    return (x, y, z)


def _default_material(category: str) -> str:
    """A sensible material when the model did not name one."""
    return {
        "sofa": "fabric", "sectional": "fabric", "armchair": "fabric",
        "cushion": "fabric", "pillow": "fabric", "curtains": "fabric",
        "rug": "carpet", "carpet": "carpet",
        "coffee_table": "wood", "dining_table": "wood", "study_table": "wood",
        "side_table": "wood", "bedside_table": "wood", "console_table": "wood",
        "tv_unit": "wood", "cabinet": "wood", "wardrobe": "wood",
        "bookshelf": "wood", "shelves": "wood", "sideboard": "wood", "bed": "wood",
        "tv": "plastic", "monitor": "plastic", "laptop": "plastic",
        "refrigerator": "metal", "microwave": "metal", "washing_machine": "metal",
        "mirror": "glass", "painting": "wood", "flower_vase": "ceramic",
        "plant": "plastic",
    }.get(category, "unknown")


# ---------------------------------------------------------------------------
# Lights, openings and architecture
# ---------------------------------------------------------------------------


def ground_lights(
    fused: Sequence[FusedLight], room: RoomFrame, cameras: Dict[str, CameraPose]
) -> List[LightSource]:
    """Turn observed luminaires into positioned Blender-ready lights."""
    camera = next(iter(cameras.values()), None)
    lights: List[LightSource] = []

    # Larger rooms need proportionally more power for the same perceived level.
    area_scale = clamp(room.width * room.depth / 20.0, 0.6, 2.6)

    for entity in fused:
        prior = catalog.get_light_prior(entity.kind)

        if entity.count > 1 or prior.mounting == "ceiling" and entity.kind in ("recessed_light", "led_strip", "cove_light", "spotlight"):
            lights.extend(_distribute_ceiling_lights(entity, prior, room, area_scale))
            continue

        position = _single_light_position(entity, prior, room, camera)
        lights.append(
            LightSource(
                id=entity.entity_id,
                kind=entity.kind,
                position=Vec3(position[0], position[1], position[2]),
                mounting=prior.mounting,
                color_temperature_k=entity.cct_k,
                power_w=prior.power_w * entity.brightness * area_scale * (1.0 if entity.is_on else 0.0),
                size=prior.size,
                confidence=entity.confidence,
                uncertain=entity.uncertain,
                source_images=list(entity.source_images),
            )
        )

    return lights


def _distribute_ceiling_lights(
    entity: FusedLight, prior: catalog.LightPrior, room: RoomFrame, area_scale: float
) -> List[LightSource]:
    """Lay ``count`` fixtures out on a regular ceiling grid.

    Downlights are installed on a grid in practice, and a grid is far closer to
    the truth than stacking every instance at one back-projected point.
    """
    count = max(1, entity.count)
    columns = max(1, int(round(math.sqrt(count * max(room.width, 1e-3) / max(room.depth, 1e-3)))))
    rows = max(1, math.ceil(count / columns))

    lights: List[LightSource] = []
    index = 0
    for row in range(rows):
        for column in range(columns):
            if index >= count:
                break
            x = room.bounds_min[0] + room.width * (column + 0.5) / columns
            y = room.bounds_min[1] + room.depth * (row + 0.5) / rows
            lights.append(
                LightSource(
                    id=f"{entity.kind}_{index + 1}",
                    kind=entity.kind,
                    position=Vec3(x, y, min(prior.default_height, room.ceiling_height - 0.05)),
                    mounting=prior.mounting,
                    color_temperature_k=entity.cct_k,
                    power_w=prior.power_w * entity.brightness * area_scale
                    * (1.0 if entity.is_on else 0.0),
                    size=prior.size,
                    confidence=entity.confidence,
                    uncertain=entity.uncertain,
                    source_images=list(entity.source_images),
                )
            )
            index += 1

    return lights


def _single_light_position(
    entity: FusedLight, prior: catalog.LightPrior, room: RoomFrame, camera: Optional[CameraPose]
) -> Tuple[float, float, float]:
    height = min(prior.default_height, room.ceiling_height - 0.05)

    if camera is not None and entity.primary_bbox is not None:
        u, v = entity.primary_bbox.center
        if prior.mounting == "ceiling":
            point = camera.intersect_height(u, v, height)
        elif prior.mounting == "floor":
            point = camera.intersect_floor(u, entity.primary_bbox.y1)
            height = 0.0
        else:
            point = camera.intersect_floor(u, min(0.98, entity.primary_bbox.y1 + 0.12))
        if point is not None and math.dist(camera.position, point) <= room.diagonal * MAX_DEPTH_FACTOR:
            clamped = room.clamp_inside(point, margin=0.15)
            return (clamped[0], clamped[1], height)

    return (room.center[0], room.center[1], height)


def ground_openings(
    observations: Sequence[OpeningObservation], room: RoomFrame, cameras: Dict[str, CameraPose]
) -> List[Opening]:
    """Attach observed doors and windows to concrete walls."""
    camera = next(iter(cameras.values()), None)
    size_scale = {"very_small": 0.6, "small": 0.8, "medium": 1.0, "large": 1.3, "very_large": 1.7}

    # Resolve each opening's wall first, then lay them out. Placing them one at
    # a time puts every opening at its wall's midpoint, so a wall with three
    # windows ends up with three coincident holes instead of a run of them.
    assigned: List[Tuple[OpeningObservation, Optional[Wall]]] = [
        (observation, _select_wall(observation.on_wall, room, camera))
        for observation in observations
    ]

    by_wall: Dict[str, List[int]] = {}
    for index, (_, wall) in enumerate(assigned):
        by_wall.setdefault(wall.id if wall else "", []).append(index)

    openings: List[Opening] = []

    for wall_key, indices in by_wall.items():
        wall = assigned[indices[0]][1]
        count = len(indices)

        for slot, index in enumerate(indices):
            observation = assigned[index][0]
            scale = size_scale.get(observation.size_bucket, 1.0)

            if observation.kind == "door":
                width, height = 0.9 * scale, 2.1
            elif observation.kind == "archway":
                width, height = 1.4 * scale, 2.2
            elif observation.kind == "niche":
                width, height = 0.6 * scale, 0.9 * scale
            else:
                width, height = 1.2 * scale, 1.4 * scale

            if wall is not None:
                # Space the openings evenly along the wall run, leaving the
                # ends solid so the structure still reads as a wall.
                usable = wall.length * 0.86
                width = min(width, max(0.4, usable / count * 0.88))
                fraction = 0.07 + 0.86 * (slot + 0.5) / count
                centre = (
                    wall.start[0] + (wall.end[0] - wall.start[0]) * fraction,
                    wall.start[1] + (wall.end[1] - wall.start[1]) * fraction,
                )
            else:
                centre = room.center

            openings.append(
                Opening(
                    id=observation.local_id,
                    kind=observation.kind,
                    wall_id=wall.id if wall else "",
                    position=Vec3(centre[0], centre[1], observation.sill_height),
                    width=width,
                    height=height,
                    sill_height=observation.sill_height,
                    confidence=observation.confidence,
                    uncertain=observation.uncertain,
                    source_images=[observation.image_id],
                )
            )

    return openings


def ground_architecture(
    observations: Sequence[ArchObservation], room: RoomFrame, cameras: Dict[str, CameraPose]
) -> List[ArchElement]:
    """Place columns, beams and similar structural features."""
    camera = next(iter(cameras.values()), None)
    elements: List[ArchElement] = []

    defaults = {
        "column": (0.35, 0.35, room.ceiling_height),
        "beam": (room.width, 0.30, 0.35),
        "staircase": (1.10, 2.60, room.ceiling_height),
        "partition": (2.00, 0.12, room.ceiling_height * 0.75),
        "false_ceiling": (room.width * 0.8, room.depth * 0.8, 0.20),
        "niche": (0.80, 0.25, 1.00),
    }

    for observation in observations:
        width, depth, height = defaults.get(observation.kind, (0.4, 0.4, 1.0))

        point: Optional[Tuple[float, float]] = None
        if camera is not None and observation.bbox is not None:
            point = camera.intersect_floor(observation.bbox.center[0], observation.bbox.y1)
            if point is not None and math.dist(camera.position, point) > room.diagonal * MAX_DEPTH_FACTOR:
                point = None
        if point is None:
            point = room.center

        point = room.clamp_inside(point, margin=max(width, depth) / 2.0)
        z = room.ceiling_height - height if observation.kind in ("beam", "false_ceiling") else 0.0

        material_prior = catalog.get_material(observation.material)
        elements.append(
            ArchElement(
                id=observation.local_id,
                kind=observation.kind,
                position=Vec3(point[0], point[1], z),
                dimensions=Dimensions(width, depth, height),
                finish=Finish(
                    material=observation.material,
                    color_hex=normalise_hex(observation.color_hex, material_prior.color_hex),
                    roughness=material_prior.roughness,
                    metallic=material_prior.metallic,
                    confidence=observation.confidence,
                ),
                confidence=observation.confidence,
                uncertain=observation.uncertain,
                source_images=[observation.image_id],
            )
        )

    return elements
