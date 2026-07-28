"""
End-to-end tests for the vision pipeline, driven by a fake model backend.

These exercise every deterministic stage — parsing, fusion, grounding,
relationships, asset matching and validation — without touching the network,
so they run in CI and on a machine with no API key.
"""

from __future__ import annotations

import math

import pytest

from vision import catalog, fusion, grounding, observe, relations, validate
from vision.schema import BBox2D, ConfidencePolicy, Dimensions, SceneObject, Vec3, validate_graph


# ---------------------------------------------------------------------------
# Observation parsing
# ---------------------------------------------------------------------------


def test_parse_normalises_and_rejects(living_room_payload):
    obs = observe.parse_observation(living_room_payload, "img0", "ref.jpg")

    categories = [o.category for o in obs.objects]

    # Synonyms resolved through the catalog.
    assert "sectional" in categories
    assert "tv_unit" in categories

    # Unrecognisable category dropped despite its high stated confidence.
    assert obs.rejected.get("unrecognised_category") == 1
    assert all("blob" not in o.label for o in obs.objects)

    # A predicate the solver cannot enforce is not silently kept.
    assert obs.rejected.get("unenforceable_predicate") == 1
    # A relationship naming an object that does not exist is dropped.
    assert obs.rejected.get("relationship_dangling_reference") == 1
    assert all(r.subject != "ghost_9" for r in obs.relationships)


def test_low_confidence_is_flagged_not_dropped(living_room_payload):
    obs = observe.parse_observation(living_room_payload, "img0", "ref.jpg")

    stool = next((o for o in obs.objects if o.category == "stool"), None)
    assert stool is not None, "0.44 confidence sits in the review band, so it is kept"
    assert stool.uncertain is True


def test_below_floor_confidence_is_discarded(living_room_payload):
    payload = dict(living_room_payload)
    payload["objects"] = [
        {**living_room_payload["objects"][0], "confidence": 0.15},
    ]
    obs = observe.parse_observation(payload, "img0", "ref.jpg")

    assert obs.objects == []
    assert obs.rejected.get("below_confidence_floor") == 1


def test_parse_survives_garbage():
    for payload in ({}, {"objects": "not a list"}, {"objects": [None, 42]}, []):
        obs = observe.parse_observation(payload, "img0", "ref.jpg")
        assert obs.objects == []


def test_finish_rejects_material_wrong_for_surface():
    obs = observe.parse_observation(
        {"finishes": {"ceiling": {"material": "carpet", "color_hex": "#FFFFFF",
                                  "confidence": 0.9}}},
        "img0", "ref.jpg",
    )
    # Carpet cannot be a ceiling finish; it falls back rather than shading wrong.
    assert obs.ceiling_finish.material != "carpet"


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def test_fusion_does_not_duplicate_across_images(living_room_payload):
    """The same room from two angles must not yield two of every sofa."""
    obs_a = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    obs_b = observe.parse_observation(living_room_payload, "img1", "b.jpg")

    result = fusion.fuse([obs_a, obs_b])

    counts = {}
    for entity in result.objects:
        counts[entity.category] = counts.get(entity.category, 0) + 1

    assert counts["sectional"] == 1
    assert counts["dining_chair"] == 3, "three chairs seen twice are still three chairs"
    assert result.stats["objects_before_fusion"] == 2 * result.stats["objects_after_fusion"]


def test_corroboration_raises_confidence(living_room_payload):
    single = fusion.fuse([observe.parse_observation(living_room_payload, "img0", "a.jpg")])
    double = fusion.fuse([
        observe.parse_observation(living_room_payload, "img0", "a.jpg"),
        observe.parse_observation(living_room_payload, "img1", "b.jpg"),
    ])

    one = next(o for o in single.objects if o.category == "sectional")
    two = next(o for o in double.objects if o.category == "sectional")

    assert two.confidence > one.confidence
    assert two.observation_count == 2
    assert two.confidence <= 0.99


def test_light_counts_take_max_not_sum(living_room_payload):
    result = fusion.fuse([
        observe.parse_observation(living_room_payload, "img0", "a.jpg"),
        observe.parse_observation(living_room_payload, "img1", "b.jpg"),
    ])
    downlights = next(x for x in result.lights if x.kind == "recessed_light")
    assert downlights.count == 6, "six downlights seen twice are still six"


def test_room_type_from_furniture_evidence(living_room_payload):
    result = fusion.fuse([observe.parse_observation(living_room_payload, "img0", "a.jpg")])
    assert result.room_type == "living_room"
    assert result.room_type_confidence > 0.5


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_room_frame_from_geometry(rect_geometry):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    assert room.width == pytest.approx(6.0)
    assert room.depth == pytest.approx(4.0)
    assert len(room.walls) == 4
    assert room.contains((3.0, 2.0))
    assert not room.contains((7.0, 2.0))


def test_dimensions_come_from_priors_not_the_model(rect_geometry):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    dims, _ = grounding.resolve_dimensions("sofa", "medium", None, room)

    typical = catalog.get_prior("sofa").typical
    assert dims.width == pytest.approx(typical[0], abs=0.01)
    assert dims.height == pytest.approx(typical[2], abs=0.01)


def test_size_bucket_scales_dimensions(rect_geometry):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    small, _ = grounding.resolve_dimensions("sofa", "small", None, room)
    large, _ = grounding.resolve_dimensions("sofa", "large", None, room)
    assert large.width > small.width


def test_dimensions_clamped_to_room(rect_geometry):
    tiny = {"walls": [
        {"start": [0, 0], "end": [2, 0]}, {"start": [2, 0], "end": [2, 2]},
        {"start": [2, 2], "end": [0, 2]}, {"start": [0, 2], "end": [0, 0]},
    ]}
    room = grounding.build_room_frame(tiny, 2.4)
    dims, notes = grounding.resolve_dimensions("sectional", "very_large", None, room)

    assert dims.width <= room.width
    assert "width clamped to room" in notes


def test_back_projection_puts_nearer_objects_closer(rect_geometry):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    camera = grounding.estimate_camera(room, observe.CameraObservation(horizon_y=0.45))

    near = camera.intersect_floor(0.5, 0.95)  # low in frame = close
    far = camera.intersect_floor(0.5, 0.60)   # near horizon = distant

    assert near is not None and far is not None
    assert math.dist(camera.position, near) < math.dist(camera.position, far)


def test_ray_above_horizon_has_no_floor_hit(rect_geometry):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    camera = grounding.estimate_camera(room, observe.CameraObservation(horizon_y=0.5))
    assert camera.intersect_floor(0.5, 0.05) is None


def test_grounded_objects_land_inside_the_room(rect_geometry, living_room_payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}

    objects = grounding.ground_objects(fused.objects, room, cameras)

    for obj in objects:
        if obj.support != "floor":
            continue
        assert room.contains((obj.position.x, obj.position.y)), f"{obj.id} escaped the room"


def test_wall_affine_objects_snap_to_a_wall(rect_geometry, living_room_payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}

    objects = grounding.ground_objects(fused.objects, room, cameras)
    tv_unit = next(o for o in objects if o.category == "tv_unit")

    # A media console has wall_affinity 0.95, so it must end up against a wall.
    assert tv_unit.distance_to_nearest_wall < 0.6


def test_wall_mounted_objects_get_a_wall(rect_geometry, living_room_payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}

    objects = grounding.ground_objects(fused.objects, room, cameras)
    tv = next(o for o in objects if o.category == "tv")

    assert tv.wall_id, "a wall-mounted TV must resolve to a wall"
    assert tv.position.z > 0.4, "a mounted TV is not on the floor"


def test_downlights_are_distributed_not_stacked(rect_geometry, living_room_payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}

    lights = grounding.ground_lights(fused.lights, room, cameras)
    recessed = [x for x in lights if x.kind == "recessed_light"]

    assert len(recessed) == 6
    positions = {(round(x.position.x, 2), round(x.position.y, 2)) for x in recessed}
    assert len(positions) == 6, "downlights must be spread over a grid"


def test_lights_that_are_off_get_no_power(rect_geometry, living_room_payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(living_room_payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}

    lights = grounding.ground_lights(fused.lights, room, cameras)
    lamp = next(x for x in lights if x.kind == "floor_lamp")
    assert lamp.power_w == 0.0


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def _solved_scene(rect_geometry, payload):
    room = grounding.build_room_frame(rect_geometry, 3.0)
    obs = observe.parse_observation(payload, "img0", "a.jpg")
    fused = fusion.fuse([obs])
    cameras = {"img0": grounding.estimate_camera(room, obs.camera)}
    objects = grounding.ground_objects(fused.objects, room, cameras)
    rels = relations.infer_relationships(fused.relationships, objects)
    relations.apply_relationships(objects, rels, room)
    return room, objects, rels


def test_sofa_is_rotated_to_face_the_tv(rect_geometry, living_room_payload):
    from vision import geometry2d as g2

    room, objects, _ = _solved_scene(rect_geometry, living_room_payload)
    sofa = next(o for o in objects if o.category == "sectional")
    tv_unit = next(o for o in objects if o.category == "tv_unit")

    wanted = g2.heading_toward(
        (sofa.position.x, sofa.position.y), (tv_unit.position.x, tv_unit.position.y)
    )
    # Either the constraint was applied, or it was explicitly refused because
    # the wall it backs onto forbids the rotation.
    applied = g2.angle_between_deg(sofa.rotation_z, wanted) < 1.0
    refused = any("limited_by_wall" in flag for flag in sofa.flags)
    assert applied or refused


def test_vase_sits_on_the_coffee_table_surface(rect_geometry, living_room_payload):
    room, objects, _ = _solved_scene(rect_geometry, living_room_payload)
    vase = next(o for o in objects if o.category == "flower_vase")
    table = next(o for o in objects if o.category == "coffee_table")

    assert vase.support == "on_object"
    assert vase.support_id == table.id
    assert vase.position.z > table.position.z
    assert "awaiting_support_placement" not in vase.flags


def test_chairs_are_arranged_around_the_table(rect_geometry, living_room_payload):
    room, objects, _ = _solved_scene(rect_geometry, living_room_payload)
    chairs = [o for o in objects if o.category == "dining_chair"]
    table = next(o for o in objects if o.category == "dining_table")

    assert len(chairs) == 3
    for chair in chairs:
        distance = chair.position.planar_distance_to(table.position)
        assert 0.15 < distance < 2.5, f"{chair.id} is not seated at the table"
        assert any("arranged_around" in flag for flag in chair.flags)

    # They must not all be in the same seat.
    seats = {(round(c.position.x, 2), round(c.position.y, 2)) for c in chairs}
    assert len(seats) == 3


def test_rug_is_centred_under_the_coffee_table(rect_geometry, living_room_payload):
    room, objects, _ = _solved_scene(rect_geometry, living_room_payload)
    rug = next(o for o in objects if o.category == "rug")
    table = next(o for o in objects if o.category == "coffee_table")

    assert rug.position.planar_distance_to(table.position) < 0.01
    assert rug.position.z == pytest.approx(0.0)


def test_implied_relationships_are_marked_lower_confidence(rect_geometry):
    """A bed and a bedside table with no stated relationship still pair up."""
    payload = {
        "room": {"room_type": "bedroom", "style": "modern", "confidence": 0.9},
        "camera": {"horizon_y": 0.45, "field_of_view": "normal"},
        "objects": [
            {"id": "bed_1", "category": "bed", "label": "double bed",
             "bbox": [0.2, 0.45, 0.8, 0.95], "size_bucket": "medium",
             "support": "floor", "confidence": 0.95},
            {"id": "bt_1", "category": "bedside_table", "label": "nightstand",
             "bbox": [0.05, 0.5, 0.18, 0.7], "size_bucket": "medium",
             "support": "floor", "confidence": 0.8},
        ],
        "relationships": [],
    }
    room, objects, rels = _solved_scene(rect_geometry, payload)

    beside = [r for r in rels if r.predicate == "beside"]
    assert beside, "bedside table should be inferred as beside the bed"
    assert beside[0].confidence <= relations.IMPLIED_CONFIDENCE
    assert beside[0].satisfied


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _graph_with(objects, rect_geometry):
    from vision.schema import SceneGraph

    room = grounding.build_room_frame(rect_geometry, 3.0)
    graph = SceneGraph(walls=room.walls, objects=objects)
    return graph, room


def test_overlapping_objects_are_separated(rect_geometry):
    a = SceneObject(id="a", category="armchair", position=Vec3(3.0, 2.0, 0.0),
                    dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.9, support="floor")
    b = SceneObject(id="b", category="armchair", position=Vec3(3.05, 2.05, 0.0),
                    dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.5, support="floor")

    graph, room = _graph_with([a, b], rect_geometry)
    report = validate.validate_and_correct(graph, room)

    from vision import geometry2d as g2

    assert g2.rect_overlap(a.footprint_corners(), b.footprint_corners()) <= 0.1
    assert any(i.kind == "overlap" for i in report.issues)


def test_less_confident_object_absorbs_most_of_the_movement(rect_geometry):
    a = SceneObject(id="a", category="armchair", position=Vec3(3.0, 2.0, 0.0),
                    dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.95, support="floor")
    b = SceneObject(id="b", category="armchair", position=Vec3(3.1, 2.0, 0.0),
                    dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.45, support="floor")
    start_a, start_b = (a.position.x, a.position.y), (b.position.x, b.position.y)

    graph, room = _graph_with([a, b], rect_geometry)
    validate.validate_and_correct(graph, room)

    moved_a = math.dist(start_a, (a.position.x, a.position.y))
    moved_b = math.dist(start_b, (b.position.x, b.position.y))
    assert moved_b > moved_a


def test_rug_under_table_is_not_treated_as_a_collision(rect_geometry):
    rug = SceneObject(id="rug", category="rug", position=Vec3(3.0, 2.0, 0.0),
                      dimensions=Dimensions(2.4, 1.7, 0.02), confidence=0.9, support="floor")
    table = SceneObject(id="table", category="coffee_table", position=Vec3(3.0, 2.0, 0.0),
                        dimensions=Dimensions(1.2, 0.6, 0.42), confidence=0.9, support="floor")

    graph, room = _graph_with([rug, table], rect_geometry)
    report = validate.validate_and_correct(graph, room)

    assert not any(i.kind == "overlap" for i in report.issues)
    assert rug.position.x == pytest.approx(3.0)
    assert table.position.x == pytest.approx(3.0)


def test_floating_object_is_dropped_to_the_floor(rect_geometry):
    obj = SceneObject(id="a", category="armchair", position=Vec3(3.0, 2.0, 0.8),
                      dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.9, support="floor")

    graph, room = _graph_with([obj], rect_geometry)
    report = validate.validate_and_correct(graph, room)

    assert obj.position.z == pytest.approx(0.0)
    assert any(i.kind == "floating_object" and i.corrected for i in report.issues)


def test_object_outside_room_is_pulled_back_in(rect_geometry):
    obj = SceneObject(id="a", category="armchair", position=Vec3(9.0, 6.0, 0.0),
                      dimensions=Dimensions(0.9, 0.9, 0.9), confidence=0.9, support="floor")

    graph, room = _graph_with([obj], rect_geometry)
    report = validate.validate_and_correct(graph, room)

    assert room.contains((obj.position.x, obj.position.y))
    assert any(i.kind == "outside_room" and i.corrected for i in report.issues)


def test_absurdly_large_object_is_withheld_not_reshaped(rect_geometry):
    obj = SceneObject(id="a", category="sectional", position=Vec3(3.0, 2.0, 0.0),
                      dimensions=Dimensions(5.5, 3.8, 0.9), confidence=0.9, support="floor")

    graph, room = _graph_with([obj], rect_geometry)
    report = validate.validate_and_correct(graph, room)

    assert "a" in report.withheld
    assert obj.uncertain is True
    assert obj not in graph.buildable_objects()


def test_object_through_ceiling_is_trimmed(rect_geometry):
    obj = SceneObject(id="a", category="wardrobe", position=Vec3(3.0, 2.0, 0.0),
                      dimensions=Dimensions(1.8, 0.6, 3.2), confidence=0.9, support="floor")

    graph, room = _graph_with([obj], rect_geometry)
    validate.validate_and_correct(graph, room)

    assert obj.position.z + obj.dimensions.height <= room.ceiling_height + 0.01


# ---------------------------------------------------------------------------
# Asset matching
# ---------------------------------------------------------------------------


def test_asset_matching_distinguishes_proportions():
    from vision import assets

    wide = assets.match_asset("sectional", (3.4, 2.2, 0.85), "modern", "fabric", "#6E6A63")
    compact = assets.match_asset("sectional", (2.3, 1.4, 0.85), "scandinavian", "fabric", "#DDD5C8")

    assert wide.variant is not None and compact.variant is not None
    assert wide.variant.key != compact.variant.key


def test_asset_matching_respects_material():
    from vision import assets

    marble = assets.match_asset("coffee_table", (1.1, 1.1, 0.42), "contemporary", "marble", "#E8E4DC")
    assert marble.variant is not None
    assert "marble" in marble.variant.materials or marble.variant.params.get("round")


def test_unknown_category_falls_back_to_generic():
    from vision import assets

    match = assets.match_asset("nonexistent", (1, 1, 1), "modern", "wood", "#FFFFFF")
    assert match.variant is None
    assert assets.builder_for("generic_box") == "build_box_furniture"


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def test_full_pipeline_produces_a_valid_graph(
    tmp_path, rect_geometry, living_room_payload, fake_backend_factory, monkeypatch
):
    from vision import pipeline, vlm

    image = tmp_path / "ref.jpg"
    _write_test_jpeg(image)

    backend = fake_backend_factory([living_room_payload])
    monkeypatch.setattr(vlm, "GeminiBackend", lambda *a, **k: backend)
    monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)

    config = pipeline.PipelineConfig(cache_dir=str(tmp_path / "cache"), wall_height=3.0)
    result = pipeline.analyse([str(image)], rect_geometry, config, log=lambda *a: None)

    assert result.ok
    assert validate_graph(result.graph) == [], "graph must be structurally valid"

    graph = result.graph
    assert graph.room.room_type == "living_room"
    assert len(graph.buildable_objects()) >= 8
    # The fixture describes a "light oak" floor. It must resolve to the timber
    # family, and — now that the taxonomy has a species tier — to the specific
    # species rather than being flattened to generic wood.
    from vision.catalog import material_family

    assert material_family(graph.floor.material) == "wood"
    assert graph.floor.material == "light_oak"
    assert graph.ceiling_type == "recessed"
    assert graph.diagnostics["confidence"]["objects"] > 0

    # Round-trips through JSON without loss.
    path = tmp_path / "graph.json"
    graph.save(str(path))
    from vision.schema import SceneGraph

    reloaded = SceneGraph.load(str(path))
    assert len(reloaded.objects) == len(graph.objects)
    # Serialisation rounds to 4 dp (0.1 mm) to keep the JSON readable.
    assert reloaded.objects[0].position.x == pytest.approx(
        graph.objects[0].position.x, abs=1e-4
    )


def test_pipeline_caches_second_run(
    tmp_path, rect_geometry, living_room_payload, fake_backend_factory, monkeypatch
):
    from vision import pipeline, vlm

    image = tmp_path / "ref.jpg"
    _write_test_jpeg(image)

    backend = fake_backend_factory([living_room_payload])
    monkeypatch.setattr(vlm, "GeminiBackend", lambda *a, **k: backend)
    monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)

    config = pipeline.PipelineConfig(cache_dir=str(tmp_path / "cache"), wall_height=3.0)
    pipeline.analyse([str(image)], rect_geometry, config, log=lambda *a: None)
    assert backend.calls == 1

    pipeline.analyse([str(image)], rect_geometry, config, log=lambda *a: None)
    assert backend.calls == 1, "second run must be served entirely from cache"


def test_pipeline_survives_total_vision_failure(
    tmp_path, rect_geometry, fake_backend_factory, monkeypatch
):
    from vision import pipeline, vlm

    image = tmp_path / "ref.jpg"
    _write_test_jpeg(image)

    backend = fake_backend_factory([RuntimeError("model exploded")])
    monkeypatch.setattr(vlm, "GeminiBackend", lambda *a, **k: backend)
    monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)

    config = pipeline.PipelineConfig(
        cache_dir=str(tmp_path / "cache"), wall_height=3.0, max_attempts=1
    )
    result = pipeline.analyse([str(image)], rect_geometry, config, log=lambda *a: None)

    # No furniture, but a structurally valid room the Blender step can still build.
    assert result.ok is False
    assert result.graph.objects == []
    assert len(result.graph.walls) == 4
    assert result.errors


def _write_test_jpeg(path) -> None:
    from PIL import Image

    Image.new("RGB", (640, 360), (128, 128, 128)).save(path, "JPEG")


# ---------------------------------------------------------------------------
# Response repair
# ---------------------------------------------------------------------------


def test_json_extraction_handles_fences_and_truncation():
    from vision.vlm import extract_json_object

    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Here you go:\n{"a": 1}') == {"a": 1}
    assert extract_json_object("not json at all") is None

    # A truncated response should still yield the objects already emitted.
    truncated = '{"objects": [{"id": "a", "confidence": 0.9}, {"id": "b", "confi'
    recovered = extract_json_object(truncated)
    assert recovered is not None
    assert recovered["objects"][0]["id"] == "a"


def test_confidence_policy_bands():
    assert ConfidencePolicy.classify(0.9) == "accept"
    assert ConfidencePolicy.classify(0.5) == "uncertain"
    assert ConfidencePolicy.classify(0.2) == "discard"
