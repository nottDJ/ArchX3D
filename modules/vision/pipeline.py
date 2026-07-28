"""
ArchX3D — Vision pipeline orchestrator
======================================
Reference images + DXF geometry → one validated, multi-room scene graph.

Stage order and why
-------------------
1. **Segment** the DXF into discrete rooms. Everything downstream is
   room-scoped, which is what makes "do not place objects in the wrong room"
   a structural guarantee rather than a check.
2. **Classify** each image locally (line-art detection) so technical drawings
   are routed away from appearance extraction before any model call.
3. **Observe** — one multimodal call per image, *in parallel*, each using the
   prompt appropriate to its class. The model's own classification refines the
   local profile in the same response, so routing costs no extra request.
4. **Assign** images to rooms by clustering on the room each depicts and
   matching those clusters to segmented regions.
5. Per room: **fuse → ground → relate**, inside that room's own frame.
6. Plan views are grounded across the whole plan and assigned by position.
7. **Match assets → validate → assemble.**

Failure of any single image degrades the result rather than failing the run.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import (
    appearance,
    assets,
    assignment,
    catalog,
    classify,
    fusion,
    grounding,
    observe,
    prompts,
    relations,
    rooms as room_seg,
    validate,
)
from .cache import ResponseCache
from .schema import (
    SCHEMA_VERSION,
    Finish,
    Room,
    SceneGraph,
    Vec3,
    ViewPoint,
    Wall,
    validate_graph,
)
from .vlm import DEFAULT_MODEL, FALLBACK_MODEL, GeminiBackend, VisionClient, VLMError

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


@dataclass
class PipelineConfig:
    model: str = DEFAULT_MODEL
    fallback_model: Optional[str] = FALLBACK_MODEL
    cache_dir: str = ".cache/vision"
    use_cache: bool = True
    max_images: int = 12
    wall_height: float = 3.0
    wall_thickness: float = 0.15
    include_uncertain: bool = False
    max_attempts: int = 3
    #: Concurrent model calls. Kept modest to stay clear of rate limits.
    max_workers: int = 4
    #: Doorway width used when separating rooms; see `rooms.segment_rooms`.
    gap_closing_m: float = room_seg.DEFAULT_GAP_CLOSING
    #: Treat the whole plan as one room (the pre-segmentation behaviour).
    single_room: bool = False


@dataclass
class PipelineResult:
    graph: SceneGraph
    ok: bool
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def analyse(
    image_paths: Sequence[str],
    geometry: Dict[str, Any],
    config: Optional[PipelineConfig] = None,
    log=print,
) -> PipelineResult:
    """Run the full vision pipeline and return a validated scene graph."""
    config = config or PipelineConfig()
    started = time.time()
    errors: List[str] = []
    warnings: List[str] = []

    images = list(image_paths)[: config.max_images]
    walls = _build_walls(geometry, config.wall_height, config.wall_thickness)

    # ---- 1. Segment the plan into rooms ---------------------------------
    regions, seg_stats = _segment(geometry, config, log)
    plan_min, plan_max = _plan_bounds(walls)

    # ---- 2. Classify images locally --------------------------------------
    profiles: Dict[str, classify.ImageProfile] = {}
    for index, path in enumerate(images):
        image_id = f"img{index}"
        profiles[image_id] = classify.profile_image(image_id, path)

    # ---- 3. Observe (parallel) -------------------------------------------
    observations, vision_stats, observe_errors = _observe_images(
        images, profiles, regions, config, log
    )
    errors.extend(observe_errors)

    log(f"[VISION] Image classes: "
        f"{', '.join(f'{k}x{v}' for k, v in sorted(classify.summarise(list(profiles.values()))['by_class'].items()))}")

    if not observations:
        log("[VISION] No usable observations; returning an unfurnished graph.")
        graph = _empty_graph(regions, walls, config, errors, time.time() - started)
        graph.diagnostics["images"] = [p.to_dict() for p in profiles.values()]
        return PipelineResult(graph=graph, ok=False, errors=errors)

    # ---- 4. Assign images to rooms ---------------------------------------
    assigned = assignment.assign(observations, profiles, regions)
    warnings.extend(assigned.warnings)

    log(f"[VISION] Rooms: {len(regions)} segmented, "
        f"{assigned.stats.get('rooms_with_imagery', 0)} with imagery, "
        f"{assigned.stats.get('rooms_unfurnished', 0)} left unfurnished")

    # ---- 5. Solve each room in its own frame -----------------------------
    all_objects = []
    all_lights = []
    all_openings = []
    all_architecture = []
    all_relationships = []
    room_records: List[Room] = []
    fusion_totals = {"before": 0, "after": 0, "corroborated": 0}

    all_viewpoints = []

    for group in assigned.groups:
        solved = _solve_room(group, walls, config, log)
        room_records.append(solved.room)
        all_objects.extend(solved.objects)
        all_lights.extend(solved.lights)
        all_openings.extend(solved.openings)
        all_architecture.extend(solved.architecture)
        all_relationships.extend(solved.relationships)
        all_viewpoints.extend(solved.viewpoints)
        stats = solved.stats
        for key, source in (("before", "objects_before_fusion"), ("after", "objects_after_fusion")):
            fusion_totals[key] += int(stats.get(source, 0) or 0)
        fusion_totals["corroborated"] += int(stats.get("multi_image_objects", 0) or 0)

    # ---- 6. Plan views span every room -----------------------------------
    if assigned.plan_observations:
        plan_objects = _solve_plan_views(
            assigned.plan_observations, regions, walls, plan_min, plan_max, config, log
        )
        all_objects.extend(plan_objects)

    # ---- 7. Technical drawings contribute geometry only ------------------
    if assigned.geometry_observations:
        extra_openings = _solve_geometry_views(
            assigned.geometry_observations, regions, walls, config, log
        )
        all_openings.extend(extra_openings)

    # ---- 8. Assets, room stamping, validation ----------------------------
    dominant_style = _dominant_style(room_records)
    builder_histogram = assets.assign_assets(all_objects, dominant_style)

    graph = _assemble(room_records, walls, all_objects, all_lights, all_openings,
                      all_architecture, all_relationships, regions, all_viewpoints)

    room_counts = assignment.stamp_room_ids(graph.objects, graph.lights, graph.openings, regions)

    # Validation runs per room, so a collision in one room cannot displace
    # furniture in another.
    validation_summary = _validate_per_room(graph, regions, walls, config, log)

    reoriented = relations.reapply_orientation(graph.objects, graph.relationships)
    if reoriented:
        log(f"[VISION] Re-oriented {reoriented} objects after validation moves")

    schema_problems = validate_graph(graph)
    if schema_problems:
        errors.extend(schema_problems[:10])

    elapsed = time.time() - started
    graph.provenance = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "schema_version": SCHEMA_VERSION,
        "images": [os.path.basename(p) for p in images],
        "image_count": len(images),
        "images_with_observations": len(observations),
        "vision": vision_stats,
        "elapsed_s": round(elapsed, 2),
        "include_uncertain": config.include_uncertain,
    }
    graph.diagnostics = {
        "segmentation": seg_stats,
        "images": [profiles[o.image_id].to_dict() for o in observations
                   if o.image_id in profiles],
        "image_summary": classify.summarise(list(profiles.values())),
        "assignment": assigned.stats,
        "fusion": fusion_totals,
        "objects_per_room": room_counts,
        "asset_builders": builder_histogram,
        "validation": validation_summary,
        "rejections": _merge_rejections(observations),
        "warnings": warnings,
        "errors": errors,
        "confidence": _confidence_summary(graph),
    }

    log(f"[VISION] Complete in {elapsed:.1f}s - "
        f"{len(graph.buildable_objects(config.include_uncertain))} objects across "
        f"{sum(1 for r in graph.rooms if any(o.room_id == r.id for o in graph.objects))} rooms, "
        f"{sum(1 for o in graph.objects if o.uncertain)} flagged uncertain")

    return PipelineResult(graph=graph, ok=True, errors=errors)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _segment(geometry, config: PipelineConfig, log):
    """Split the plan into rooms, or fall back to one whole-plan region."""
    wall_segments = geometry.get("walls") or []

    if config.single_room:
        region = room_seg.fallback_region(wall_segments)
        return [region], {"mode": "single_room (forced)", "rooms_kept": 1}

    result = room_seg.segment_rooms(
        wall_segments,
        wall_thickness=config.wall_thickness,
        gap_closing_m=config.gap_closing_m,
    )

    if not result.ok:
        reason = result.stats.get("reason", "no enclosed rooms found")
        log(f"[VISION] Room segmentation found nothing ({reason}); "
            "treating the plan as a single open space")
        region = room_seg.fallback_region(wall_segments)
        stats = dict(result.stats)
        stats["fallback"] = "whole-plan single region"
        return [region], stats

    log(f"[VISION] Segmented {len(result.regions)} rooms: "
        + ", ".join(f"{r.id} {r.area:.0f}m2" for r in result.regions[:6])
        + (" ..." if len(result.regions) > 6 else ""))
    return result.regions, result.stats


def _observe_images(images, profiles, regions, config: PipelineConfig, log):
    """Call the model once per image, concurrently, and parse each response."""
    errors: List[str] = []

    if not images:
        return [], {"reason": "no images supplied"}, ["no reference images supplied"]

    cache = ResponseCache(config.cache_dir, enabled=config.use_cache)

    try:
        backend = GeminiBackend(config.model)
        fallback = (
            GeminiBackend(config.fallback_model)
            if config.fallback_model and config.fallback_model != config.model
            else None
        )
    except VLMError as exc:
        return [], {"reason": str(exc)}, [str(exc)]

    client = VisionClient(
        backend=backend, cache=cache, max_attempts=config.max_attempts,
        fallback_backend=fallback,
    )

    total_area = sum(r.area for r in regions)
    hint = prompts.build_room_hint(
        width_m=max((r.width for r in regions), default=0.0),
        depth_m=max((r.depth for r in regions), default=0.0),
        ceiling_height_m=config.wall_height,
        wall_count=sum(len(r.wall_ids) for r in regions),
        room_type=(
            f"{len(regions)} enclosed rooms totalling {total_area:.0f} m2"
            if len(regions) > 1 else None
        ),
    )

    def work(item: Tuple[int, str]):
        index, path = item
        image_id = f"img{index}"
        profile = profiles[image_id]
        prompt = prompts.prompt_for_mode(profile.analysis_mode, hint)
        try:
            result = client.analyse_image(path, prompt)
        except VLMError as exc:
            return image_id, path, None, f"{os.path.basename(path)}: {exc}"
        return image_id, path, result, None

    started = time.time()
    workers = max(1, min(config.max_workers, len(images)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, enumerate(images)))
    wall_clock = time.time() - started

    observations: List[observe.ImageObservation] = []

    for image_id, path, result, error in results:
        if error:
            errors.append(error)
            log(f"[VISION] ! {error}")
            continue

        profile = profiles[image_id]
        # The model classifies in the same response, so routing is free.
        classify.merge_model_classification(profile, result.payload)

        observation = observe.parse_observation(
            result.payload, image_id, path, analysis_mode=profile.analysis_mode
        )
        observation.geometry_trust = profile.geometry_trust
        observations.append(observation)

        log(f"[VISION] {os.path.basename(path)} [{profile.image_class}/"
            f"{profile.analysis_mode}]"
            f"{' (cached)' if result.cached else f' ({result.latency_s:.1f}s)'}: "
            f"{len(observation.objects)} objects, {len(observation.lights)} lights, "
            f"{len(observation.relationships)} relationships"
            + (f", {sum(observation.rejected.values())} rejected" if observation.rejected else ""))

    stats = client.stats()
    stats["wall_clock_s"] = round(wall_clock, 2)
    stats["workers"] = workers
    # Sequential cost is the sum of per-call latencies; wall clock is what the
    # user waited. The ratio is the parallel speed-up actually achieved.
    if stats.get("total_latency_s", 0) and wall_clock > 0:
        stats["parallel_speedup"] = round(stats["total_latency_s"] / wall_clock, 2)

    return observations, stats, errors


@dataclass
class _RoomSolution:
    """Everything one room contributes to the graph.

    A named record rather than a tuple: the solver returns seven different
    collections and adding an eighth to a positional unpack is how the wrong
    list ends up in the wrong slot.
    """

    room: Room
    objects: List = field(default_factory=list)
    lights: List = field(default_factory=list)
    openings: List = field(default_factory=list)
    architecture: List = field(default_factory=list)
    relationships: List = field(default_factory=list)
    viewpoints: List = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


def _solve_room(group, walls, config: PipelineConfig, log) -> "_RoomSolution":
    """Fuse, ground and relate one room's imagery inside its own frame."""
    region = group.region
    room_frame = grounding.frame_from_region(region, walls, config.wall_height)

    room_record = Room(
        id=region.id,
        room_type=group.room_type,
        style=group.style,
        polygon=list(region.polygon),
        bounds_min=region.bounds_min,
        bounds_max=region.bounds_max,
        ceiling_height=config.wall_height,
        confidence=group.room_type_confidence,
        area=region.area,
        connected_to=list(region.connected_to),
        wall_ids=list(region.wall_ids),
        source_images=list(group.image_ids),
    )

    if not group.has_imagery:
        return _RoomSolution(room=room_record)

    fused = fusion.fuse(group.observations)
    room_record.room_type = fused.room_type if fused.room_type != "unknown" else group.room_type
    room_record.wall_finish = fused.wall_finish
    room_record.floor_finish = fused.floor_finish
    room_record.ceiling_finish = fused.ceiling_finish
    room_record.ceiling_type = fused.ceiling_type

    cameras = {
        obs.image_id: grounding.estimate_camera(
            room_frame, obs.camera, _aspect_of(obs.image_path)
        )
        for obs in group.observations
    }

    objects = grounding.ground_objects(fused.objects, room_frame, cameras)
    lights = grounding.ground_lights(fused.lights, room_frame, cameras)
    openings = grounding.ground_openings(fused.openings, room_frame, cameras)
    architecture = grounding.ground_architecture(fused.architecture, room_frame, cameras)

    # Namespace every id by room so two bedrooms cannot both own "bed_1".
    for entity in list(objects) + list(lights) + list(openings) + list(architecture):
        entity.id = f"{region.id}__{entity.id}"
    for obj in objects:
        obj.room_id = region.id
    for light in lights:
        light.room_id = region.id
    for opening in openings:
        opening.room_id = region.id

    relationships = relations.infer_relationships(
        _renamespace(fused.relationships, region.id), objects
    )
    relations.apply_relationships(objects, relationships, room_frame)

    # ---- Appearance ------------------------------------------------------
    # Style, palette and lighting are resolved here rather than in fusion
    # because they depend on the *grounded* room: the palette weighs furniture
    # by footprint, and the lighting environment needs the windows in place.
    surface_materials = [
        finish.material
        for finish in (room_record.wall_finish, room_record.floor_finish,
                       room_record.ceiling_finish)
        if finish is not None
    ]
    room_record.style, room_record.style_confidence = appearance.resolve_style(
        fused.style, objects, surface_materials
    )
    room_record.palette = appearance.derive_palette(room_record, objects, lights)
    room_record.lighting = appearance.derive_lighting(
        room_record, lights, openings, fused.lighting_environment
    )

    # Keep the fitted cameras. They were previously discarded once grounding
    # finished; retaining them lets the build be previewed from the same
    # vantage as each photograph, which is what makes the two comparable.
    viewpoints = [
        ViewPoint(
            image_id=obs.image_id,
            room_id=region.id,
            source_image=os.path.basename(obs.image_path or ""),
            position=Vec3(camera.x, camera.y, camera.height),
            yaw=camera.yaw,
            pitch_deg=camera.pitch_deg,
            vertical_fov_deg=camera.vertical_fov_deg,
            aspect=camera.aspect,
            confidence=obs.camera.confidence,
        )
        for obs in group.observations
        for camera in [cameras.get(obs.image_id)]
        if camera is not None
    ]

    log(f"[VISION]   {region.id} ({room_record.room_type}, {region.area:.0f}m2): "
        f"{len(objects)} objects, {len(lights)} lights from "
        f"{len(group.observations)} image(s)")

    return _RoomSolution(
        room=room_record,
        objects=objects,
        lights=lights,
        openings=openings,
        architecture=architecture,
        relationships=relationships,
        viewpoints=viewpoints,
        stats=fused.stats,
    )


def _renamespace(relationships, room_id: str):
    """Rewrite fused relationship ids into the room's namespace."""
    out = []
    for relation in relationships:
        clone = type(relation)(
            subject=f"{room_id}__{relation.subject}",
            predicate=relation.predicate,
            object=f"{room_id}__{relation.object}",
            confidence=relation.confidence,
        )
        out.append(clone)
    return out


def _solve_plan_views(plan_observations, regions, walls, plan_min, plan_max, config, log):
    """Place furniture read off a top-down furnished plan."""
    fused = fusion.fuse(plan_observations)
    whole_plan = grounding.RoomFrame(
        polygon=[
            (plan_min[0], plan_min[1]), (plan_max[0], plan_min[1]),
            (plan_max[0], plan_max[1]), (plan_min[0], plan_max[1]),
        ],
        bounds_min=plan_min,
        bounds_max=plan_max,
        ceiling_height=config.wall_height,
        walls=walls,
    )

    objects = grounding.ground_plan_view(fused.objects, plan_min, plan_max, whole_plan)

    kept = []
    for obj in objects:
        region = assignment.room_for_point((obj.position.x, obj.position.y), regions)
        if region is None:
            # A plan-view detection that falls outside every room is most
            # likely a legend, title block or dimension annotation.
            continue
        obj.room_id = region.id
        obj.id = f"{region.id}__plan_{obj.id}"
        kept.append(obj)

    log(f"[VISION] Plan views: {len(kept)}/{len(objects)} objects mapped into rooms")
    return kept


def _solve_geometry_views(geometry_observations, regions, walls, config, log):
    """Extract openings from technical drawings. No appearance, by design."""
    fused = fusion.fuse(geometry_observations)
    whole_plan = grounding.RoomFrame(
        polygon=[(r.centroid[0], r.centroid[1]) for r in regions] or [(0, 0)],
        bounds_min=min((r.bounds_min for r in regions), default=(0.0, 0.0)),
        bounds_max=max((r.bounds_max for r in regions), default=(1.0, 1.0)),
        ceiling_height=config.wall_height,
        walls=walls,
    )
    openings = grounding.ground_openings(fused.openings, whole_plan, {})
    for index, opening in enumerate(openings):
        opening.id = f"cad__{opening.id}_{index}"

    log(f"[VISION] Technical drawings: {len(openings)} openings verified "
        "(no materials, furniture or lighting taken)")
    return openings


def _validate_per_room(graph: SceneGraph, regions, walls, config, log):
    """Validate each room independently, then merge the reports."""
    merged = {
        "total_issues": 0, "corrected": 0, "uncorrected": 0,
        "by_kind": {}, "withheld_objects": [], "issues": [],
    }

    by_room: Dict[str, List] = {}
    for obj in graph.objects:
        by_room.setdefault(obj.room_id, []).append(obj)

    for region in regions:
        room_objects = by_room.get(region.id, [])
        room_lights = [x for x in graph.lights if x.room_id == region.id]
        if not room_objects and not room_lights:
            continue

        frame = grounding.frame_from_region(region, walls, config.wall_height)
        scoped = SceneGraph(
            rooms=[graph.room_by_id(region.id) or Room(id=region.id)],
            walls=frame.walls,
            objects=room_objects,
            lights=room_lights,
        )
        report = validate.validate_and_correct(scoped, frame).to_dict()

        merged["total_issues"] += report["total_issues"]
        merged["corrected"] += report["corrected"]
        merged["uncorrected"] += report["uncorrected"]
        merged["withheld_objects"].extend(report["withheld_objects"])
        merged["issues"].extend(report["issues"])
        for kind, count in report["by_kind"].items():
            merged["by_kind"][kind] = merged["by_kind"].get(kind, 0) + count

    log(f"[VISION] Validation: {merged['corrected']} corrected, "
        f"{merged['uncorrected']} unresolved, "
        f"{len(merged['withheld_objects'])} withheld")
    return merged


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _build_walls(geometry, ceiling_height: float, thickness: float) -> List[Wall]:
    walls: List[Wall] = []
    for index, segment in enumerate(geometry.get("walls") or []):
        try:
            start = (float(segment["start"][0]), float(segment["start"][1]))
            end = (float(segment["end"][0]), float(segment["end"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        walls.append(
            Wall(id=f"wall_{index}", start=start, end=end,
                 height=ceiling_height, thickness=thickness)
        )
    return walls


def _plan_bounds(walls: Sequence[Wall]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    if not walls:
        return (0.0, 0.0), (5.0, 4.0)
    xs = [p for w in walls for p in (w.start[0], w.end[0])]
    ys = [p for w in walls for p in (w.start[1], w.end[1])]
    return (min(xs), min(ys)), (max(xs), max(ys))


def _dominant_style(room_records: Sequence[Room]) -> str:
    """Scene-wide style, weighted by area *and* per-room confidence."""
    return appearance.dominant_style(room_records)[0]


def _assemble(room_records, walls, objects, lights, openings, architecture,
              relationships, regions, viewpoints=None) -> SceneGraph:
    """Build the scene graph document from the solved rooms."""
    primary = max(room_records, key=lambda r: r.area) if room_records else Room()

    # Graph-level finishes mirror the largest observed room, so consumers that
    # do not yet read per-room finishes still get something sensible.
    floor = primary.floor_finish or Finish(
        material="wood", color_hex=catalog.get_material("wood").color_hex
    )
    ceiling = primary.ceiling_finish or Finish(material="paint_matte", color_hex="#F6F5F2")
    wall_finish = primary.wall_finish or Finish(material="paint_matte", color_hex="#EFEDE8")

    for wall in walls:
        owner = next((r for r in room_records if wall.id in r.wall_ids), None)
        wall.finish = (owner.wall_finish if owner and owner.wall_finish else wall_finish)
        wall.observed = wall.finish.confidence > 0

    ordered = sorted(room_records, key=lambda r: -r.area)

    return SceneGraph(
        schema_version=SCHEMA_VERSION,
        rooms=ordered or [Room()],
        walls=walls,
        floor=floor,
        ceiling=ceiling,
        ceiling_type=primary.ceiling_type,
        openings=openings,
        architecture=architecture,
        lights=lights,
        objects=objects,
        relationships=relationships,
        viewpoints=list(viewpoints or []),
    )


def _empty_graph(regions, walls, config, errors, elapsed) -> SceneGraph:
    """A structurally valid, unfurnished graph for the failure path."""
    graph = SceneGraph(
        rooms=[
            Room(
                id=r.id, polygon=list(r.polygon), bounds_min=r.bounds_min,
                bounds_max=r.bounds_max, ceiling_height=config.wall_height,
                area=r.area, connected_to=list(r.connected_to), wall_ids=list(r.wall_ids),
            )
            for r in regions
        ] or [Room()],
        walls=walls,
        floor=Finish(material="wood", color_hex=catalog.get_material("wood").color_hex),
        ceiling=Finish(material="paint_matte", color_hex="#F6F5F2"),
    )
    graph.provenance = {"elapsed_s": round(elapsed, 2), "images": []}
    graph.diagnostics = {"errors": errors, "reason": "no usable observations"}
    return graph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def discover_images(path: str, limit: int = 12) -> List[str]:
    """Resolve a file or directory into a list of image paths."""
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        return []
    found = [
        os.path.join(path, name)
        for name in sorted(os.listdir(path))
        if name.lower().endswith(IMAGE_EXTENSIONS)
    ]
    return found[:limit]


def _aspect_of(path: str, default: float = 16 / 9) -> float:
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            if img.height:
                return img.width / img.height
    except Exception:
        pass
    return default


def _merge_rejections(observations) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for observation in observations:
        for reason, count in observation.rejected.items():
            merged[reason] = merged.get(reason, 0) + count
    return merged


def _confidence_summary(graph: SceneGraph) -> Dict[str, Any]:
    values = [obj.confidence for obj in graph.objects]
    if not values:
        return {"objects": 0}
    ordered = sorted(values)
    return {
        "objects": len(values),
        "mean": round(sum(values) / len(values), 3),
        "median": round(ordered[len(ordered) // 2], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "accepted": sum(1 for v in values if v >= 0.65),
        "uncertain": sum(1 for obj in graph.objects if obj.uncertain),
    }
