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

import math
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
    tiling,
    validate,
)
from .cache import ResponseCache
from .schema import (
    SCHEMA_VERSION,
    Finish,
    Opening,
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
    #: Analyse large, dense images in overlapping tiles. Costs one model call
    #: per tile, and recovers detail a single downscaled pass cannot resolve.
    tile_large_images: bool = True
    #: Procedurally furnish rooms that no reference image covered. Rooms with
    #: observed contents are never touched, so this only ever adds to an
    #: otherwise-empty room.
    furnish: bool = True


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

    # ---- 1b. Classify rooms from the drawing itself ----------------------
    # Before any image is looked at. The design philosophy puts CAD metadata,
    # blocks, layers and text above image understanding, and this is where
    # that ordering is enforced: rooms the drawing names are already typed by
    # the time imagery is considered. It also improves image assignment, which
    # matches an image's declared room type against each region's.
    cad_document, cad_results = _classify_from_cad(geometry, regions, log)

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
        # No imagery does not mean no understanding. The drawing's own labels,
        # blocks and layers still identify the rooms; this path previously
        # returned every room as "unknown", which was the single largest
        # source of unidentified rooms in practice.
        log("[VISION] No usable observations; returning an unfurnished graph "
            "with CAD-derived room types.")
        graph = _empty_graph(regions, walls, config, errors,
                             time.time() - started, cad_results)
        graph.diagnostics["images"] = [p.to_dict() for p in profiles.values()]
        graph.diagnostics["segmentation"] = seg_stats
        graph.diagnostics["room_classification"] = _classification_diagnostics(cad_results)

        # No imagery is the case procedural furnishing exists for. The rooms
        # are typed from the drawing, so they can be furnished from their types
        # — a DXF on its own should still yield a furnished building, not a
        # correct but empty shell.
        graph.diagnostics["furnishing"] = _furnish_empty_rooms(
            graph, config, "modern", log
        )
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
    plan_registrations: List[Dict[str, Any]] = []
    if assigned.plan_observations:
        plan_objects, plan_registrations = _solve_plan_views(
            assigned.plan_observations, regions, walls, plan_min, plan_max,
            config, log, document=cad_document,
        )
        all_objects.extend(plan_objects)

    # ---- 7. Technical drawings contribute geometry only ------------------
    if assigned.geometry_observations:
        extra_openings = _solve_geometry_views(
            assigned.geometry_observations, regions, walls, config, log
        )
        all_openings.extend(extra_openings)

    # ---- 8. Final room classification, now with imagery as one more signal -
    # Re-run over every room with the vision evidence merged in. CAD still
    # outranks it, so a drawing that names a room keeps that name; but a room
    # the drawing left anonymous can now be identified from what was seen in
    # it, and a room with neither stays honestly unknown.
    final_results = _reclassify_with_vision(
        geometry, regions, room_records, all_objects, cad_document, log
    )
    _stamp_classifications(room_records, final_results or cad_results)

    # ---- 9. Assets, room stamping, validation ----------------------------
    dominant_style = _dominant_style(room_records)
    builder_histogram = assets.assign_assets(all_objects, dominant_style)

    # Openings the drawing states outrank openings a photograph suggested, and
    # they are the only complete set — one image sees one corner of one room.
    all_openings = _merge_cad_openings(cad_document, walls, all_openings, log)

    graph = _assemble(room_records, walls, all_objects, all_lights, all_openings,
                      all_architecture, all_relationships, regions, all_viewpoints)

    room_counts = assignment.stamp_room_ids(graph.objects, graph.lights, graph.openings, regions)

    # ---- 10. Procedurally furnish rooms nothing was observed in ----------
    # Runs *before* validation so generated furniture is collision-checked and
    # contained exactly like observed furniture — there is no second, weaker
    # standard for it. Rooms with observed contents are skipped inside
    # `furnish`, so an observation always wins over a convention.
    furnish_summary = _furnish_empty_rooms(graph, config, dominant_style, log)

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
        "room_classification": _classification_diagnostics(
            final_results or cad_results
        ),
        "cad": (cad_document.stats if cad_document is not None
                else {"available": False}),
        "furnishing": furnish_summary,
        "images": [profiles[o.image_id].to_dict() for o in observations
                   if o.image_id in profiles],
        "image_summary": classify.summarise(list(profiles.values())),
        "assignment": assigned.stats,
        "registration": {
            "plan_views": plan_registrations,
            "registered": sum(1 for r in plan_registrations if r.get("registered")),
            "total": len(plan_registrations),
        },
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


# ---------------------------------------------------------------------------
# Semantic room classification
# ---------------------------------------------------------------------------
#
# The `semantic` package is imported lazily and defensively. It is a hard
# requirement for good results and a soft one for running at all: a checkout
# without it should still produce a correct architectural shell rather than
# failing to import.


def _semantic():
    """The semantic package, or ``None`` when it is unavailable."""
    try:
        import semantic  # noqa: PLC0415 - deliberately lazy

        return semantic
    except ImportError:
        return None


def _merge_cad_openings(document, walls, observed, log):
    """Add the drawing's doors and windows to whatever the imagery found.

    The drawing is authoritative here and the imagery is not, for a reason that
    is structural rather than a matter of degree: a plan shows *every* opening
    in the building, positioned exactly, while a photograph shows the two or
    three in one corner of one room and gives their position only by inference.

    So CAD openings are taken wholesale, and an observed opening is dropped
    when the drawing already declares one in the same place — the drawing's
    version has a real width and a real host wall.
    """
    if document is None:
        return observed

    try:
        from cad import openings as cad_openings  # noqa: PLC0415
    except ImportError:  # pragma: no cover - cad package is optional
        return observed

    try:
        found = cad_openings.from_document(document)
    except Exception as exc:  # pragma: no cover - never fail the build for this
        log(f"[OPENINGS] ! could not read openings from the drawing: {exc}")
        return observed

    if not found:
        return observed

    converted: List[Opening] = []
    for index, item in enumerate(found):
        wall_id = _nearest_wall_id(item.x, item.y, walls)
        converted.append(Opening(
            id=f"cad_{item.kind}_{index}",
            kind=item.kind,
            wall_id=wall_id,
            position=Vec3(item.x, item.y, item.sill_height + item.height / 2.0),
            width=item.width,
            height=item.height,
            sill_height=item.sill_height,
            confidence=item.confidence,
            uncertain=False,
        ))

    # An observed opening within half a metre of a stated one is the same
    # opening seen twice; cutting both would widen the hole.
    kept = [
        o for o in observed
        if not any(_close(o.position.x, o.position.y, c.position.x, c.position.y, 0.5)
                   for c in converted)
    ]
    dropped = len(observed) - len(kept)

    log(f"[OPENINGS] {cad_openings.summarise(found)}"
        + (f"; {dropped} image-derived opening(s) superseded" if dropped else ""))
    return converted + kept


def _nearest_wall_id(x: float, y: float, walls) -> str:
    """Host wall for an opening, by distance to the wall's centre line.

    The host determines the angle the cutter is rotated to, so a wrong answer
    cuts a slot across the wall instead of through it.
    """
    best_id, best_distance = "", float("inf")
    for wall in walls or []:
        distance = _point_to_wall(x, y, wall)
        if distance < best_distance:
            best_id, best_distance = wall.id, distance
    return best_id


def _point_to_wall(x: float, y: float, wall) -> float:
    x0, y0 = wall.start[0], wall.start[1]
    x1, y1 = wall.end[0], wall.end[1]
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(x - x0, y - y0)
    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length_sq))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))


def _close(ax: float, ay: float, bx: float, by: float, tolerance: float) -> bool:
    return math.hypot(ax - bx, ay - by) <= tolerance


def _cad_document(geometry: Dict[str, Any]):
    """The CAD model embedded in ``geometry.json``, when there is one.

    A geometry file written by the legacy extractor has no ``cad`` key. That
    is not an error — it just means only tiers 5 and 6 are available.
    """
    try:
        from cad.schema import CadDocument  # noqa: PLC0415

        return CadDocument.from_geometry_json(geometry)
    except (ImportError, ValueError, TypeError, KeyError):
        return None


def _classify_from_cad(geometry, regions, log):
    """Classify every region from the drawing alone, before imagery is used.

    Returns ``(cad_document, {room_id: RoomClassification})``. Region records
    are stamped in place so image-to-room assignment can use them.
    """
    semantic = _semantic()
    if semantic is None:
        log("[SEMANTIC] semantic package unavailable; room typing will rely on "
            "imagery alone")
        return None, {}

    document = _cad_document(geometry)

    inputs = semantic.build_inputs(document, regions)
    results = {r.room_id: r for r in semantic.classify_plan(inputs)}

    # Stamping the regions is what lets `assignment` match a bedroom photo to
    # the region the drawing already calls a bedroom, rather than guessing
    # from floor area alone.
    for region in regions:
        result = results.get(region.id)
        if result and result.room_type != "unknown":
            region.room_type = result.room_type
            region.room_type_confidence = result.confidence

    identified = sum(1 for r in results.values() if r.room_type != "unknown")
    if document is None:
        log(f"[SEMANTIC] No CAD model in geometry.json (legacy extractor); "
            f"{identified}/{len(results)} rooms typed from geometry alone")
    else:
        log(f"[SEMANTIC] CAD evidence: {identified}/{len(results)} rooms identified "
            f"from {len(document.room_labels())} labels, {len(document.blocks)} blocks")
        for result in sorted(results.values(), key=lambda r: -r.confidence)[:8]:
            if result.room_type != "unknown":
                head = result.reasons[0] if result.reasons else "no stated reason"
                log(f"[SEMANTIC]   {result.room_id}: {result.room_type} "
                    f"{result.confidence:.0%} - {head}")

    return document, results


def _reclassify_with_vision(geometry, regions, room_records, objects, document, log):
    """Re-run classification with the imagery's findings folded in.

    Vision is tier 6, below every CAD tier, so this can only *add* to what the
    drawing established — it identifies rooms CAD left anonymous and corrobates
    the ones it named, but cannot overturn an explicit room label.
    """
    semantic = _semantic()
    if semantic is None:
        return {}

    by_id = {room.id: room for room in room_records}
    categories: Dict[str, List[str]] = {}
    for obj in objects:
        if obj.room_id:
            categories.setdefault(obj.room_id, []).append(obj.category)

    vision_by_room = {}
    for region in regions:
        record = by_id.get(region.id)
        room_type = record.room_type if record else ""
        confidence = record.confidence if record else 0.0
        detected = categories.get(region.id, [])
        if room_type in ("", "unknown") and not detected:
            continue
        vision_by_room[region.id] = (room_type, confidence, detected)

    if not vision_by_room:
        return {}

    inputs = semantic.build_inputs(document, regions, vision_by_room=vision_by_room)
    results = {r.room_id: r for r in semantic.classify_plan(inputs)}

    summary = semantic.summarise(list(results.values()))
    log(f"[SEMANTIC] Final: {summary['identified']}/{summary['rooms']} rooms "
        f"identified, {summary['confident']} confidently, "
        f"mean confidence {summary['mean_confidence']:.2f}"
        + (f", {summary['conflicts']} conflict(s)" if summary["conflicts"] else ""))

    return results


def _stamp_classifications(room_records, results) -> None:
    """Write classification outcomes onto the ``Room`` records."""
    for room in room_records:
        result = results.get(room.id)
        if result is None:
            continue
        # A classification that found nothing must not erase a room type the
        # imagery established on its own.
        if result.room_type == "unknown" and room.room_type not in ("", "unknown"):
            continue
        room.room_type = result.room_type
        room.confidence = result.confidence
        room.specific_type = result.specific_type
        room.type_reasons = list(result.reasons)
        room.type_decided_by = result.decided_by
        room.type_conflicts = [c.detail for c in result.conflicts]
        room.runner_up_type = result.runner_up
        room.runner_up_confidence = result.runner_up_confidence


def _classification_diagnostics(results) -> Dict[str, Any]:
    """Per-room classification detail for the diagnostics block."""
    semantic = _semantic()
    if not results:
        return {"rooms": 0, "reason": "no classification was run"}

    values = list(results.values())
    summary = semantic.summarise(values) if semantic else {"rooms": len(values)}
    summary["rooms_detail"] = [
        {
            "room_id": r.room_id,
            "room_type": r.room_type,
            "specific_type": r.specific_type,
            "confidence": round(r.confidence, 3),
            "decided_by": r.decided_by,
            "runner_up": r.runner_up,
            "reasons": r.reasons,
            "conflicts": [c.to_dict() for c in r.conflicts],
        }
        for r in sorted(values, key=lambda x: -x.confidence)
    ]
    return summary


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

    tile_dir = os.path.join(config.cache_dir, "tiles")

    def work(item: Tuple[int, str]):
        index, path = item
        image_id = f"img{index}"
        profile = profiles[image_id]
        prompt = prompts.prompt_for_mode(profile.analysis_mode, hint)

        # A large, dense image is analysed in overlapping tiles. The model
        # sees a fixed internal resolution, so a whole sheet gets downscaled
        # until its furniture is a few pixels across — one real run returned
        # 11 objects for an entire house. Tiling restores the detail at the
        # cost of one call per tile.
        if config.tile_large_images and tiling.should_tile(path, profile.analysis_mode):
            tiles = tiling.plan_tiles(path, tile_dir)
            if len(tiles) > 1:
                return _analyse_tiled(
                    client, tiles, prompt, image_id, path, log
                )

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


def _registration():
    """The registration package, or ``None`` when it is unavailable.

    Imported lazily and defensively for the same reason as ``semantic``: a
    checkout without it should still produce a model, falling back to the
    full-frame assumption rather than failing to import.
    """
    try:
        import registration  # noqa: PLC0415 - deliberately lazy

        return registration
    except ImportError:
        return None


def _solve_plan_views(plan_observations, regions, walls, plan_min, plan_max,
                      config, log, document=None):
    """Place furniture read off a top-down furnished plan.

    Each observation is registered to the drawing first. Registration is
    per-image rather than per-batch because two uploaded sheets may show the
    same building at different scales, or different floors, and one transform
    cannot describe both.
    """
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

    registrations = _register_plan_views(document, plan_observations, plan_min,
                                         plan_max, log)

    kept: List = []
    total = 0
    diagnostics: List[Dict[str, Any]] = []

    # Fused per registration group rather than all at once: objects from a
    # sheet that registered cleanly must not be merged with objects from one
    # that did not, because fusion would average their positions and the good
    # sheet would inherit the bad one's error.
    for observation in plan_observations:
        result = registrations.get(observation.image_id)
        fused = fusion.fuse([observation])

        transform = getattr(result, "transform", None)
        assumed = not getattr(result, "registered", False)

        objects = grounding.ground_plan_view(
            fused.objects, plan_min, plan_max, whole_plan,
            transform=transform, assumed=assumed,
        )
        total += len(objects)

        tolerance = _plan_tolerance(result)
        mapped = []
        for obj in objects:
            region = assignment.room_for_point(
                (obj.position.x, obj.position.y), regions, tolerance_m=tolerance,
            )
            if region is None:
                # A plan-view detection that falls outside every room is
                # usually a legend, title block or dimension annotation — or,
                # far more seriously, a sign that the image never registered.
                continue
            obj.room_id = region.id
            obj.id = f"{region.id}__plan_{obj.id}"
            mapped.append(obj)

        kept.extend(mapped)
        if result is not None:
            diagnostics.append(result.to_dict())
        _report_plan_view(observation, result, len(objects), len(mapped), log)

    log(f"[VISION] Plan views: {len(kept)}/{total} objects mapped into rooms")
    return kept, diagnostics


#: How much of a fit's own scatter to allow when attributing a detection to a
#: room. Residuals are a spread, not a bound, so a little over one standard
#: deviation keeps the typical near-miss without reaching a room away.
PLAN_TOLERANCE_RESIDUAL_FACTOR = 1.5

#: Never claim a detection further out than this, however loose the fit. Past
#: it the nearest room stops being evidence of anything.
PLAN_TOLERANCE_CEILING_M = 2.0

#: Registration is never perfect, so some slack is always warranted.
PLAN_TOLERANCE_FLOOR_M = 0.35


def _plan_tolerance(result) -> float:
    """How far outside a room a plan detection may fall and still be placed.

    Derived from the transform's own residual rather than fixed, because the
    two failure modes pull in opposite directions and a single constant cannot
    serve both. Too tight and every detection is lost to sub-metre fitting
    noise; too loose and detections are dragged into neighbouring rooms with
    full confidence, which is the more damaging error because nothing
    downstream can tell it happened.

    A fit that measured itself to 0.1 m gets 0.35 m of slack; one that admits
    1.3 m gets close to the 2 m ceiling. An assumed transform — the legacy
    full-frame stretch — gets the ceiling too, since it has no measured error
    to reason from and its placements are already flagged as assumed.
    """
    if result is None or not getattr(result, "registered", False):
        return PLAN_TOLERANCE_CEILING_M

    residual = getattr(result, "residual_mean_m", 0.0) or 0.0
    if residual <= 0.0:
        return PLAN_TOLERANCE_CEILING_M

    scaled = residual * PLAN_TOLERANCE_RESIDUAL_FACTOR
    return max(PLAN_TOLERANCE_FLOOR_M, min(PLAN_TOLERANCE_CEILING_M, scaled))


def _register_plan_views(document, plan_observations, plan_min, plan_max, log):
    """Fit an image → plan transform for every plan-view image."""
    registration = _registration()
    if registration is None:
        log("[REGISTER] registration package unavailable; plan views fall back "
            "to the assumption that each image is one plan filling the frame")
        return {}

    results = registration.register_plan_views(
        document, plan_observations, plan_min, plan_max
    )

    fitted = sum(1 for r in results.values() if r.registered)
    log(f"[REGISTER] {fitted}/{len(results)} plan view(s) registered to the drawing")
    return results


def _report_plan_view(observation, result, produced: int, mapped: int, log) -> None:
    """Say what registration achieved for one sheet, and what it cost.

    The old code reported a single blanket warning whenever most detections
    were lost, which named the symptom and not the cause. With a registration
    result in hand the diagnosis is available: whether the transform was
    measured or assumed, how well the labels agreed, and — for a composite
    sheet — which part of the frame the drawing actually occupies.
    """
    if result is None:
        return

    log(f"[REGISTER]   {result.explain()}")
    for warning in result.warnings:
        log(f"[REGISTER]   ! {warning}")

    if not produced:
        return

    lost = produced - mapped
    if lost <= produced * 0.5:
        return

    if result.registered:
        # The transform is trustworthy, so the detections that fell outside
        # every room genuinely are outside every room: annotations, a legend,
        # or furniture drawn on a part of the sheet this drawing does not
        # cover. That is a different problem from a failed registration and
        # must not be reported as one.
        log(f"[REGISTER]   ! {lost} of {produced} detections from "
            f"{observation.image_id} fell outside every room despite a good "
            f"registration ({result.confidence:.0%}). They are most likely "
            "annotations, or belong to another plan on the same sheet.")
    else:
        log(f"[REGISTER]   ! {lost} of {produced} detections from "
            f"{observation.image_id} fell outside every room, and this image "
            "was never registered to the drawing. "
            f"{result.reason} Label the plan's rooms legibly, crop the image "
            "to a single plan, or rely on procedural furnishing.")


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


class _TiledResult:
    """Stands in for a ``VisionClient`` result assembled from several tiles.

    Duck-typed rather than a real result object: the caller only reads
    ``payload``, ``cached`` and ``latency_s``, and inventing a parallel class
    hierarchy for a merged response would be more machinery than the
    difference deserves.
    """

    __slots__ = ("payload", "cached", "latency_s", "tiles")

    def __init__(self, payload, cached: bool, latency_s: float, tiles: int) -> None:
        self.payload = payload
        self.cached = cached
        self.latency_s = latency_s
        self.tiles = tiles


def _analyse_tiled(client, tiles, prompt, image_id, path, log):
    """Analyse one image as several tiles and merge the responses.

    A tile that fails is skipped rather than failing the image: eight good
    tiles are a better result than none, and the loss is reported.
    """
    collected = []
    failures = 0
    latency = 0.0
    all_cached = True

    for tile in tiles:
        try:
            result = client.analyse_image(tile.path, prompt)
        except VLMError:
            failures += 1
            continue
        collected.append((tile, result.payload))
        latency += getattr(result, "latency_s", 0.0) or 0.0
        all_cached = all_cached and bool(getattr(result, "cached", False))

    if not collected:
        return image_id, path, None, (
            f"{os.path.basename(path)}: all {len(tiles)} tiles failed"
        )

    merged = tiling.merge_payloads(collected)
    log(f"[VISION] {os.path.basename(path)}: analysed as {len(collected)}/"
        f"{len(tiles)} tiles -> {len(merged.get('objects') or [])} objects"
        + (f" ({failures} tile(s) failed)" if failures else ""))

    return image_id, path, _TiledResult(merged, all_cached, latency, len(collected)), None


def _furnish_empty_rooms(graph: SceneGraph, config, style: str, log) -> Dict[str, Any]:
    """Generate furniture for rooms no image furnished.

    Imported lazily and defensively for the same reason as ``semantic``: a
    checkout without the package should still produce a correct, if bare,
    architectural model rather than failing to import.
    """
    if not config.furnish:
        return {"enabled": False, "reason": "disabled by configuration"}

    try:
        import furnish as furnish_pkg  # noqa: PLC0415
    except ImportError:
        log("[FURNISH] furnish package unavailable; empty rooms stay empty")
        return {"enabled": False, "reason": "furnish package not importable"}

    before = len(graph.objects)
    report = furnish_pkg.furnish(graph, log=log)

    # Generated objects need assets like any other. Assigning only to the new
    # ones keeps the observed objects' existing matches untouched.
    new_objects = graph.objects[before:]
    if new_objects:
        assets.assign_assets(new_objects, style)

    summary = report.to_dict()
    summary["enabled"] = True
    return summary


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


def _empty_graph(regions, walls, config, errors, elapsed, cad_results=None) -> SceneGraph:
    """A structurally valid, unfurnished graph for the no-imagery path.

    Unfurnished, but not unidentified: the CAD classification still applies,
    so a plan whose rooms the drawing names comes back correctly typed even
    when no reference image was supplied or every model call failed.
    """
    cad_results = cad_results or {}

    rooms = []
    for region in regions:
        room = Room(
            id=region.id, polygon=list(region.polygon), bounds_min=region.bounds_min,
            bounds_max=region.bounds_max, ceiling_height=config.wall_height,
            area=region.area, connected_to=list(region.connected_to),
            wall_ids=list(region.wall_ids),
        )
        rooms.append(room)

    _stamp_classifications(rooms, cad_results)

    graph = SceneGraph(
        rooms=rooms or [Room()],
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
