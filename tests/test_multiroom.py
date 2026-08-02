"""
Tests for multi-image, multi-room reconstruction.

Covers room segmentation, image classification and routing, image-to-room
assignment, and the guarantee that furniture cannot land in the wrong room.
All model calls are faked, so these run without an API key.
"""

from __future__ import annotations

import math

import pytest

from vision import assignment, classify, fusion, grounding, observe
from vision import rooms as room_seg
from vision.schema import Finish


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_room_geometry():
    """10 x 6 m split at x=5 by a wall with a 0.9 m doorway."""
    return {
        "walls": [
            {"start": [0.0, 0.0], "end": [10.0, 0.0]},
            {"start": [10.0, 0.0], "end": [10.0, 6.0]},
            {"start": [10.0, 6.0], "end": [0.0, 6.0]},
            {"start": [0.0, 6.0], "end": [0.0, 0.0]},
            # Divider, broken by a door opening between y=2.5 and y=3.4
            {"start": [5.0, 0.0], "end": [5.0, 2.5]},
            {"start": [5.0, 3.4], "end": [5.0, 6.0]},
        ]
    }


def _payload(room_type, objects, room_conf=0.9):
    return {
        "room": {"room_type": room_type, "style": "modern", "confidence": room_conf},
        "camera": {"horizon_y": 0.45, "field_of_view": "wide"},
        "finishes": {
            "wall": {"material": "paint_matte", "color_hex": "#EDE7DD", "confidence": 0.85},
            "floor": {"material": "wood", "color_hex": "#C08E5C", "confidence": 0.8},
            "ceiling": {"material": "gypsum", "color_hex": "#F7F6F3", "confidence": 0.7},
        },
        "lights": [
            {"kind": "ceiling_light", "bbox": [0.45, 0.05, 0.55, 0.12],
             "mounting": "ceiling", "count": 1, "color_temperature": "warm",
             "brightness": "moderate", "is_on": True, "confidence": 0.8}
        ],
        "objects": objects,
        "relationships": [],
    }


def _obj(oid, category, bbox, conf=0.9, **kw):
    base = {
        "id": oid, "category": category, "label": category.replace("_", " "),
        "bbox": bbox, "size_bucket": "medium", "support": "floor",
        "material": "fabric", "color_hex": "#8A8A8A", "confidence": conf,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Room segmentation
# ---------------------------------------------------------------------------


def test_segments_two_rooms_through_a_doorway(two_room_geometry):
    result = room_seg.segment_rooms(two_room_geometry["walls"], wall_thickness=0.15)

    assert result.ok
    assert len(result.regions) == 2, "the divider must separate the plan into two rooms"

    for region in result.regions:
        # Each half is ~5 x 6 m; allow for the wall thickness and grid margin.
        assert 20.0 < region.area < 32.0, f"{region.id} area {region.area} is implausible"

    # They share a doorway, so they must be recorded as connected.
    assert result.regions[1].id in result.regions[0].connected_to


def test_single_room_plan_yields_one_region(rect_geometry):
    result = room_seg.segment_rooms(rect_geometry["walls"], wall_thickness=0.15)
    assert len(result.regions) == 1
    assert result.regions[0].area > 15.0


def test_unenclosed_plan_falls_back_to_whole_plan():
    """Three walls do not enclose anything; the caller must still get a room."""
    walls = [
        {"start": [0, 0], "end": [6, 0]},
        {"start": [6, 0], "end": [6, 4]},
        {"start": [6, 4], "end": [0, 4]},
    ]
    result = room_seg.segment_rooms(walls, wall_thickness=0.15)
    assert not result.ok

    region = room_seg.fallback_region(walls)
    assert region.area > 0
    assert region.contains((3.0, 2.0))


def test_region_polygon_supports_containment(two_room_geometry):
    result = room_seg.segment_rooms(two_room_geometry["walls"], wall_thickness=0.15)
    left = min(result.regions, key=lambda r: r.centroid[0])
    right = max(result.regions, key=lambda r: r.centroid[0])

    assert left.contains((2.5, 3.0))
    assert not left.contains((7.5, 3.0))
    assert right.contains((7.5, 3.0))
    assert not right.contains((2.5, 3.0))


def test_area_plausibility_prefers_sensible_rooms():
    assert room_seg.area_plausibility("living_room", 30.0) == 1.0
    assert room_seg.area_plausibility("bathroom", 30.0) < 0.5
    assert room_seg.area_plausibility("bathroom", 5.0) == 1.0


# ---------------------------------------------------------------------------
# Image classification
# ---------------------------------------------------------------------------


def _write(path, painter):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (480, 320), (255, 255, 255))
    painter(ImageDraw.Draw(image))
    image.save(path, "PNG")
    return str(path)


def test_line_art_is_detected_as_cad(tmp_path):
    def painter(draw):
        for x in range(20, 460, 28):
            draw.line((x, 20, x, 300), fill=(0, 0, 0), width=1)
        for y in range(20, 300, 28):
            draw.line((20, y, 460, y), fill=(0, 0, 0), width=1)

    profile = classify.profile_image("img0", _write(tmp_path / "cad.png", painter))

    assert profile.image_class == "cad_drawing"
    assert profile.analysis_mode == "geometry"
    assert profile.contributes_appearance is False


def test_blank_image_is_not_mistaken_for_a_drawing(tmp_path):
    """A featureless image has no strokes, so it is not line art."""
    profile = classify.profile_image("img0", _write(tmp_path / "flat.png", lambda d: None))

    assert profile.image_class != "cad_drawing"
    assert profile.analysis_mode == "full"


def test_colourful_photo_defers_to_the_model(tmp_path):
    def painter(draw):
        for i in range(0, 480, 6):
            draw.rectangle((i, 0, i + 6, 320), fill=(i % 255, (i * 3) % 255, (i * 7) % 255))

    profile = classify.profile_image("img0", _write(tmp_path / "photo.png", painter))
    assert profile.image_class == "unknown"
    assert profile.analysis_mode == "full"


def test_model_classification_refines_the_profile(tmp_path):
    profile = classify.profile_image("img0", _write(tmp_path / "flat.png", lambda d: None))
    classify.merge_model_classification(
        profile,
        {"image_class": {"type": "furnished_floorplan", "medium": "render",
                         "room_type": "unknown", "confidence": 0.9}},
    )
    assert profile.image_class == "furnished_floorplan"
    assert profile.analysis_mode == "layout"


def test_local_cad_signal_overrides_the_model(tmp_path):
    """A model claiming a blueprint is a photograph must not be believed."""
    def painter(draw):
        for x in range(20, 460, 24):
            draw.line((x, 10, x, 310), fill=(10, 10, 10), width=1)
        for y in range(20, 300, 24):
            draw.line((10, y, 470, y), fill=(10, 10, 10), width=1)

    profile = classify.profile_image("img0", _write(tmp_path / "cad.png", painter))
    classify.merge_model_classification(
        profile,
        {"image_class": {"type": "interior_photograph", "medium": "photo", "confidence": 0.95}},
    )

    assert profile.image_class == "cad_drawing"
    assert profile.contributes_appearance is False
    assert any("kept precedence" in note for note in profile.notes)


# ---------------------------------------------------------------------------
# Analysis-mode enforcement
# ---------------------------------------------------------------------------


def test_cad_image_contributes_no_appearance():
    payload = _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])])
    obs = observe.parse_observation(payload, "img0", "cad.png", analysis_mode="geometry")

    assert obs.objects == [], "a drawing has no furniture"
    assert obs.lights == [], "a drawing has no lighting"
    assert obs.wall_finish.confidence == 0.0, "a drawing has no wall colour"
    assert obs.floor_finish.confidence == 0.0
    assert obs.rejected.get("appearance_from_technical_drawing")


def test_exterior_image_contributes_nothing():
    payload = _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])])
    obs = observe.parse_observation(payload, "img0", "ext.jpg", analysis_mode="skip")

    assert obs.objects == []
    assert obs.lights == []
    assert obs.openings == []


def test_plan_view_keeps_layout_but_drops_lighting():
    payload = _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])])
    obs = observe.parse_observation(payload, "img0", "plan.png", analysis_mode="layout")

    assert len(obs.objects) == 1, "a plan view is exactly what layout comes from"
    assert obs.lights == [], "a plan's fixtures are diagrammatic"
    # Palette signal survives at reduced weight rather than being discarded.
    assert 0.0 < obs.floor_finish.confidence < 0.8


def test_full_mode_keeps_everything():
    payload = _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])])
    obs = observe.parse_observation(payload, "img0", "photo.jpg", analysis_mode="full")

    assert len(obs.objects) == 1
    assert len(obs.lights) == 1
    assert obs.wall_finish.confidence > 0


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def _regions(geometry):
    return room_seg.segment_rooms(geometry["walls"], wall_thickness=0.15).regions


def test_images_of_different_rooms_map_to_different_regions(two_room_geometry):
    regions = _regions(two_room_geometry)

    living = observe.parse_observation(
        _payload("living_room", [
            _obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9]),
            _obj("t1", "tv_unit", [0.7, 0.6, 0.95, 0.8]),
        ]), "img0", "living.jpg")
    bedroom = observe.parse_observation(
        _payload("bedroom", [
            _obj("b1", "bed", [0.2, 0.45, 0.8, 0.95]),
            _obj("w1", "wardrobe", [0.05, 0.2, 0.2, 0.8]),
        ]), "img1", "bed.jpg")

    profiles = {
        "img0": classify.ImageProfile("img0", "living.jpg"),
        "img1": classify.ImageProfile("img1", "bed.jpg"),
    }
    result = assignment.assign([living, bedroom], profiles, regions)

    furnished = [g for g in result.groups if g.has_imagery]
    assert len(furnished) == 2
    assert {g.room_type for g in furnished} == {"living_room", "bedroom"}
    # Each room got its own region.
    assert len({g.region.id for g in furnished}) == 2


def test_two_views_of_one_room_share_a_region(two_room_geometry):
    regions = _regions(two_room_geometry)

    views = [
        observe.parse_observation(
            _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])]),
            f"img{i}", f"v{i}.jpg")
        for i in range(2)
    ]
    profiles = {f"img{i}": classify.ImageProfile(f"img{i}", f"v{i}.jpg") for i in range(2)}

    result = assignment.assign(views, profiles, regions)
    furnished = [g for g in result.groups if g.has_imagery]

    assert len(furnished) == 1, "both views describe one room, so one group"
    assert len(furnished[0].observations) == 2
    assert set(furnished[0].image_ids) == {"img0", "img1"}


def test_rooms_without_imagery_are_reported(two_room_geometry):
    """A room no image covers is reported, so it can be furnished procedurally.

    The warning no longer says "left unfurnished": since Stage 7 those rooms
    *are* furnished, from their room type rather than from an image. Claiming
    otherwise would send a user looking for a missing upload when nothing is
    missing.
    """
    regions = _regions(two_room_geometry)

    only_living = observe.parse_observation(
        _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])]),
        "img0", "living.jpg")
    profiles = {"img0": classify.ImageProfile("img0", "living.jpg")}

    result = assignment.assign([only_living], profiles, regions)

    assert result.stats["rooms_with_imagery"] == 1
    assert result.stats["rooms_unfurnished"] == 1
    assert any("no reference imagery" in w for w in result.warnings)


def test_plan_views_are_not_reported_as_missing_imagery(two_room_geometry):
    """A furnished-floorplan upload must not be reported as "no imagery".

    Plan views span the whole plan and are never assigned to a region, so the
    old message told a user who had supplied a floor plan that they had
    supplied nothing — pointing them at their upload instead of at the real
    failure, which is that the plan did not register to the DXF.
    """
    regions = _regions(two_room_geometry)

    plan = observe.parse_observation(
        _payload("unknown", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])]),
        "img0", "plan.png", analysis_mode="layout")
    profile = classify.ImageProfile("img0", "plan.png")
    profile.analysis_mode = "layout"

    result = assignment.assign([plan], {"img0": profile}, regions)

    assert result.stats["plan_views"] == 1
    warnings = " ".join(result.warnings)
    assert "plan view" in warnings
    assert "interior" in warnings


def test_room_for_point_resolves_correctly(two_room_geometry):
    regions = _regions(two_room_geometry)
    left = assignment.room_for_point((2.5, 3.0), regions)
    right = assignment.room_for_point((7.5, 3.0), regions)

    assert left is not None and right is not None
    assert left.id != right.id
    assert assignment.room_for_point((50.0, 50.0), regions) is None


# ---------------------------------------------------------------------------
# The core guarantee: furniture stays in its own room
# ---------------------------------------------------------------------------


def test_objects_are_confined_to_their_assigned_room(two_room_geometry):
    """A bedroom render's furniture must be physically unable to reach the
    living room, because it is grounded inside the bedroom's own frame."""
    from vision.pipeline import _build_walls

    regions = _regions(two_room_geometry)
    walls = _build_walls(two_room_geometry, 3.0, 0.15)

    bedroom_obs = observe.parse_observation(
        _payload("bedroom", [
            _obj("b1", "bed", [0.2, 0.45, 0.8, 0.95]),
            _obj("w1", "wardrobe", [0.02, 0.15, 0.18, 0.85]),
            _obj("n1", "bedside_table", [0.82, 0.55, 0.95, 0.72]),
        ]), "img0", "bed.jpg")

    profiles = {"img0": classify.ImageProfile("img0", "bed.jpg")}
    result = assignment.assign([bedroom_obs], profiles, regions)
    group = next(g for g in result.groups if g.has_imagery)

    frame = grounding.frame_from_region(group.region, walls, 3.0)
    fused = fusion.fuse(group.observations)
    cameras = {"img0": grounding.estimate_camera(frame, bedroom_obs.camera)}
    objects = grounding.ground_objects(fused.objects, frame, cameras)

    assert objects, "the bedroom should be furnished"
    for obj in objects:
        if obj.support != "floor":
            continue
        assert group.region.contains((obj.position.x, obj.position.y)), (
            f"{obj.category} escaped {group.region.id} to "
            f"({obj.position.x:.2f}, {obj.position.y:.2f})"
        )


def test_room_frames_carry_only_their_own_walls(two_room_geometry):
    from vision.pipeline import _build_walls

    regions = _regions(two_room_geometry)
    walls = _build_walls(two_room_geometry, 3.0, 0.15)

    frames = [grounding.frame_from_region(r, walls, 3.0) for r in regions]
    for frame, region in zip(frames, regions):
        assert frame.walls, f"{region.id} has no bounding walls"
        assert len(frame.walls) <= len(walls)
        # The frame's polygon is the room's, not the whole plan's.
        assert frame.width < 9.0, "a half-plan room must not span the full width"


# ---------------------------------------------------------------------------
# Plan-view grounding
# ---------------------------------------------------------------------------


def test_plan_view_maps_boxes_straight_onto_the_floor(two_room_geometry):
    """A top-down plan needs no camera: image space *is* plan space."""
    from vision.pipeline import _build_walls

    walls = _build_walls(two_room_geometry, 3.0, 0.15)
    frame = grounding.RoomFrame(
        polygon=[(0, 0), (10, 0), (10, 6), (0, 6)],
        bounds_min=(0.0, 0.0), bounds_max=(10.0, 6.0),
        ceiling_height=3.0, walls=walls,
    )

    # A sofa drawn in the top-left quarter of the plan image.
    obs = observe.parse_observation(
        _payload("living_room", [_obj("s1", "sofa", [0.15, 0.15, 0.35, 0.30])]),
        "img0", "plan.png", analysis_mode="layout")
    fused = fusion.fuse([obs])

    objects = grounding.ground_plan_view(fused.objects, (0.0, 0.0), (10.0, 6.0), frame)

    assert len(objects) == 1
    sofa = objects[0]
    # Image x 0.25 → plan x 2.5; image y 0.225 (from top) → plan y 6 - 1.35.
    assert sofa.position.x == pytest.approx(2.5, abs=0.3)
    assert sofa.position.y == pytest.approx(4.65, abs=0.3)
    assert "placed_from_plan_view" in sofa.flags


# ---------------------------------------------------------------------------
# Full multi-room pipeline
# ---------------------------------------------------------------------------


def test_multiroom_pipeline_assigns_furniture_to_the_right_rooms(
    tmp_path, two_room_geometry, fake_backend_factory, monkeypatch
):
    from vision import pipeline

    living_payload = _payload("living_room", [
        _obj("s1", "sectional", [0.05, 0.5, 0.5, 0.92]),
        _obj("t1", "tv_unit", [0.72, 0.58, 0.98, 0.80]),
        _obj("c1", "coffee_table", [0.3, 0.7, 0.6, 0.9]),
    ])
    bedroom_payload = _payload("bedroom", [
        _obj("b1", "bed", [0.2, 0.45, 0.85, 0.95]),
        _obj("w1", "wardrobe", [0.02, 0.15, 0.2, 0.85]),
    ])

    from conftest import write_photo_like

    paths = [
        write_photo_like(tmp_path / name, seed=index)
        for index, name in enumerate(("living.jpg", "bedroom.jpg"))
    ]

    backend = fake_backend_factory([living_payload, bedroom_payload])
    monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)

    config = pipeline.PipelineConfig(
        cache_dir=str(tmp_path / "cache"), wall_height=3.0, max_workers=2
    )
    result = pipeline.analyse(paths, two_room_geometry, config, log=lambda *a: None)

    assert result.ok
    graph = result.graph
    assert len(graph.rooms) == 2

    by_room = {}
    for obj in graph.objects:
        by_room.setdefault(obj.room_id, set()).add(obj.category)

    assert len(by_room) == 2, "each room must receive its own furniture"

    bedroom_room = next(r for r in graph.rooms if r.room_type == "bedroom")
    living_room = next(r for r in graph.rooms if r.room_type == "living_room")

    assert "bed" in by_room[bedroom_room.id]
    assert "bed" not in by_room.get(living_room.id, set()), "a bed must not reach the living room"
    assert "sectional" in by_room[living_room.id]
    assert "sectional" not in by_room.get(bedroom_room.id, set())

    # Ids are namespaced by room, so two rooms cannot collide.
    assert all(o.id.startswith(o.room_id + "__") for o in graph.objects)

    # Every object sits inside the polygon of the room it was assigned to.
    for obj in graph.objects:
        if obj.support != "floor":
            continue
        room = graph.room_by_id(obj.room_id)
        assert room is not None
        from vision import geometry2d as g2
        assert g2.point_in_polygon((obj.position.x, obj.position.y), room.polygon), (
            f"{obj.id} is outside {obj.room_id}"
        )


def test_parallel_and_sequential_agree(
    tmp_path, two_room_geometry, fake_backend_factory, monkeypatch
):
    """Concurrency must not change the result, only the wall-clock time."""
    from vision import pipeline

    payloads = [
        _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])]),
        _payload("bedroom", [_obj("b1", "bed", [0.2, 0.45, 0.8, 0.95])]),
    ]

    from conftest import write_photo_like

    paths = [write_photo_like(tmp_path / f"v{i}.jpg", seed=i) for i in range(2)]

    signatures = []
    for workers in (1, 4):
        backend = fake_backend_factory(payloads)
        monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)
        config = pipeline.PipelineConfig(
            cache_dir=str(tmp_path / f"cache{workers}"), wall_height=3.0, max_workers=workers
        )
        graph = pipeline.analyse(paths, two_room_geometry, config, log=lambda *a: None).graph
        signatures.append(sorted(
            (o.id, o.category, round(o.position.x, 3), round(o.position.y, 3))
            for o in graph.objects
        ))

    assert signatures[0] == signatures[1]


def test_cad_image_does_not_furnish_anything(
    tmp_path, two_room_geometry, fake_backend_factory, monkeypatch
):
    """A CAD upload must contribute geometry only, even end to end."""
    from PIL import Image, ImageDraw

    from vision import pipeline

    path = tmp_path / "plan.png"
    image = Image.new("RGB", (480, 320), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(20, 460, 22):
        draw.line((x, 15, x, 305), fill=(0, 0, 0), width=1)
    for y in range(15, 305, 22):
        draw.line((20, y, 460, y), fill=(0, 0, 0), width=1)
    image.save(path, "PNG")

    # The model wrongly returns furniture and wall colours for a blueprint.
    payload = _payload("living_room", [_obj("s1", "sofa", [0.1, 0.5, 0.5, 0.9])])
    backend = fake_backend_factory([payload])
    monkeypatch.setattr(pipeline, "GeminiBackend", lambda *a, **k: backend)

    config = pipeline.PipelineConfig(cache_dir=str(tmp_path / "cache"), wall_height=3.0)
    result = pipeline.analyse([str(path)], two_room_geometry, config, log=lambda *a: None)

    assert result.graph.objects == [], "a CAD drawing must not furnish the model"
    assert result.graph.lights == []
    profiles = result.graph.diagnostics.get("images", [])
    assert profiles and profiles[0]["image_class"] == "cad_drawing"
    assert profiles[0]["contributes_appearance"] is False
