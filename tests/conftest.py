"""Shared fixtures for the ArchX3D vision tests."""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES_DIR = os.path.join(REPO_ROOT, "modules")
for path in (REPO_ROOT, MODULES_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def rect_geometry():
    """A 6 x 4 m rectangular room, in the shape ``dxf_extractor`` emits."""
    return {
        "walls": [
            {"start": [0.0, 0.0], "end": [6.0, 0.0]},
            {"start": [6.0, 0.0], "end": [6.0, 4.0]},
            {"start": [6.0, 4.0], "end": [0.0, 4.0]},
            {"start": [0.0, 4.0], "end": [0.0, 0.0]},
        ],
        "metadata": {"segment_count": 4},
    }


@pytest.fixture
def living_room_payload():
    """A realistic model response for a living room, used to drive the pipeline."""
    return {
        "room": {"room_type": "living_room", "style": "modern", "confidence": 0.9},
        "camera": {
            "height_bucket": "eye_level",
            "horizon_y": 0.46,
            "field_of_view": "wide",
            "facing_wall": "long",
            "confidence": 0.7,
        },
        "finishes": {
            "wall": {"material": "paint_matte", "color_hex": "#EDE7DD",
                     "finish": "matte", "confidence": 0.88},
            "floor": {"material": "light oak", "color_hex": "#D8B98C",
                      "finish": "satin", "confidence": 0.85},
            "ceiling": {"material": "gypsum", "color_hex": "#F7F6F3",
                        "ceiling_type": "recessed", "confidence": 0.75},
        },
        "openings": [
            {"kind": "window", "bbox": [0.05, 0.15, 0.30, 0.70], "on_wall": "left",
             "size_bucket": "large", "sill_bucket": "low", "confidence": 0.85}
        ],
        "architecture": [],
        "lights": [
            {"kind": "pendant_light", "bbox": [0.40, 0.18, 0.47, 0.30],
             "mounting": "ceiling", "count": 1, "color_temperature": "warm",
             "brightness": "moderate", "is_on": True, "confidence": 0.82},
            {"kind": "downlight", "bbox": [0.55, 0.05, 0.60, 0.09],
             "mounting": "ceiling", "count": 6, "color_temperature": "neutral",
             "brightness": "moderate", "is_on": True, "confidence": 0.7},
            {"kind": "floor lamp", "bbox": [0.20, 0.30, 0.28, 0.62],
             "mounting": "floor", "count": 1, "color_temperature": "warm",
             "brightness": "dim", "is_on": False, "confidence": 0.66},
        ],
        "objects": [
            {"id": "sofa_1", "category": "sectional", "label": "large grey fabric sectional",
             "bbox": [0.00, 0.48, 0.46, 0.95], "size_bucket": "large", "support": "floor",
             "on_wall": "left", "facing": "right", "material": "fabric",
             "color_hex": "#6E6A63", "confidence": 0.96},
            {"id": "coffee_1", "category": "coffee_table", "label": "light oak coffee table",
             "bbox": [0.23, 0.66, 0.50, 0.93], "size_bucket": "medium", "support": "floor",
             "on_wall": "none", "facing": "unknown", "material": "wood_light",
             "color_hex": "#D2AB78", "confidence": 0.93},
            {"id": "tv_unit_1", "category": "tv_unit", "label": "low oak media console",
             "bbox": [0.76, 0.60, 1.00, 0.84], "size_bucket": "large", "support": "floor",
             "on_wall": "right", "facing": "left", "material": "wood_light",
             "color_hex": "#CBA97A", "confidence": 0.9},
            {"id": "tv_1", "category": "tv", "label": "wall mounted flat screen",
             "bbox": [0.84, 0.10, 1.00, 0.40], "size_bucket": "large", "support": "wall",
             "on_wall": "right", "facing": "left", "material": "plastic",
             "color_hex": "#20242A", "confidence": 0.88},
            {"id": "dining_1", "category": "dining_table", "label": "round wooden dining table",
             "bbox": [0.36, 0.50, 0.50, 0.62], "size_bucket": "small", "support": "floor",
             "on_wall": "none", "facing": "unknown", "material": "wood",
             "color_hex": "#C9A47A", "confidence": 0.8},
            {"id": "chair_1", "category": "dining_chair", "label": "wooden dining chair",
             "bbox": [0.34, 0.50, 0.39, 0.63], "size_bucket": "medium", "support": "floor",
             "material": "wood", "color_hex": "#C4A276", "confidence": 0.72},
            {"id": "chair_2", "category": "dining_chair", "label": "wooden dining chair",
             "bbox": [0.40, 0.50, 0.45, 0.63], "size_bucket": "medium", "support": "floor",
             "material": "wood", "color_hex": "#C4A276", "confidence": 0.7},
            {"id": "chair_3", "category": "dining_chair", "label": "wooden dining chair",
             "bbox": [0.46, 0.50, 0.51, 0.63], "size_bucket": "medium", "support": "floor",
             "material": "wood", "color_hex": "#C4A276", "confidence": 0.68},
            {"id": "rug_1", "category": "rug", "label": "large pale rug",
             "bbox": [0.10, 0.72, 0.70, 1.00], "size_bucket": "large", "support": "floor",
             "material": "carpet", "color_hex": "#BDB5A6", "confidence": 0.84},
            {"id": "plant_1", "category": "plant", "label": "tall potted fig",
             "bbox": [0.27, 0.32, 0.37, 0.66], "size_bucket": "large", "support": "floor",
             "material": "plastic", "color_hex": "#4F6B3A", "confidence": 0.87},
            {"id": "vase_1", "category": "flower_vase", "label": "small dark vase",
             "bbox": [0.36, 0.62, 0.40, 0.68], "size_bucket": "small", "support": "on_object",
             "support_target": "coffee_1", "material": "ceramic",
             "color_hex": "#2E2A26", "confidence": 0.71},
            {"id": "curtain_1", "category": "curtains", "label": "floor length beige curtains",
             "bbox": [0.02, 0.08, 0.14, 0.78], "size_bucket": "large", "support": "wall",
             "on_wall": "left", "material": "fabric", "color_hex": "#B9AC98",
             "confidence": 0.8},
            {"id": "blurry_1", "category": "stool", "label": "possible stool, very blurry",
             "bbox": [0.62, 0.55, 0.66, 0.62], "size_bucket": "small", "support": "floor",
             "material": "unknown", "color_hex": "#8A8A8A", "confidence": 0.44},
            {"id": "junk_1", "category": "unidentifiable blob", "label": "unclear",
             "bbox": [0.5, 0.5, 0.52, 0.52], "size_bucket": "small", "support": "floor",
             "confidence": 0.9},
        ],
        "relationships": [
            {"subject": "sofa_1", "predicate": "faces", "object": "tv_unit_1", "confidence": 0.9},
            {"subject": "rug_1", "predicate": "centered_under", "object": "coffee_1",
             "confidence": 0.85},
            {"subject": "vase_1", "predicate": "on_top_of", "object": "coffee_1",
             "confidence": 0.8},
            {"subject": "chair_1", "predicate": "surrounds", "object": "dining_1",
             "confidence": 0.8},
            {"subject": "chair_2", "predicate": "surrounds", "object": "dining_1",
             "confidence": 0.8},
            {"subject": "chair_3", "predicate": "surrounds", "object": "dining_1",
             "confidence": 0.8},
            {"subject": "sofa_1", "predicate": "teleports_to", "object": "tv_1",
             "confidence": 0.9},
            {"subject": "ghost_9", "predicate": "faces", "object": "tv_1", "confidence": 0.5},
        ],
    }


class FakeBackend:
    """A `VisionBackend` that replays canned payloads instead of calling out.

    Thread-safe, because the pipeline analyses images concurrently. Payloads
    are handed out in order; with several workers, *which* image receives
    which payload is not deterministic, so tests should assert on the set of
    outcomes rather than on a specific image-to-payload pairing.
    """

    def __init__(self, payloads, name="fake:test"):
        import threading

        self.name = name
        self._payloads = list(payloads)
        self.calls = 0
        self._lock = threading.Lock()

    def generate_json(self, prompt, image_bytes, mime_type):
        import json

        with self._lock:
            payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
            self.calls += 1
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)


@pytest.fixture
def fake_backend_factory():
    return FakeBackend


def write_photo_like(path, seed: int = 0) -> str:
    """Write an image that the line-art detector correctly treats as content.

    Deliberately colourful and textured: a greyscale or flat image would trip
    the CAD heuristic, which is the right behaviour but the wrong fixture.
    """
    from PIL import Image

    width, height = 320, 240
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (
                (x * 3 + seed * 37) % 256,
                (y * 5 + seed * 61) % 256,
                (x * y // 7 + seed * 13) % 256,
            )
    image.save(path, "JPEG", quality=90)
    return str(path)


@pytest.fixture
def photo_like():
    return write_photo_like


# ---------------------------------------------------------------------------
# Render evaluation pipeline
# ---------------------------------------------------------------------------


def build_preview_graph():
    """A two-room graph with three fitted viewpoints.

    Deliberately asymmetric — two viewpoints in one room, one in the other —
    because the invalidation rules the render pipeline exists to enforce are
    all about *which* previews a change reaches, and a symmetric fixture
    cannot tell "this room" apart from "this viewpoint".
    """
    from vision.schema import (
        ColourPalette,
        Dimensions,
        Finish,
        LightingEnvironment,
        LightSource,
        Room,
        SceneGraph,
        SceneObject,
        Vec3,
        ViewPoint,
        Wall,
    )

    def room(room_id, x0, x1, style="modern"):
        return Room(
            id=room_id,
            room_type="living_room" if room_id == "room_a" else "kitchen",
            style=style,
            bounds_min=(x0, 0.0),
            bounds_max=(x1, 4.0),
            area=(x1 - x0) * 4.0,
            ceiling_height=3.0,
            style_confidence=0.8,
            wall_ids=[f"{room_id}_w0"],
            wall_finish=Finish(material="paint_matte", color_hex="#EDE7DD"),
            floor_finish=Finish(material="wood_light", color_hex="#D8B98C"),
            palette=ColourPalette(primary="#EDE7DD", accent="#8899AA"),
            lighting=LightingEnvironment(ambient=0.5, time_of_day="day"),
        )

    def viewpoint(image_id, room_id, x, yaw=0.0):
        return ViewPoint(
            image_id=image_id,
            room_id=room_id,
            source_image=f"{image_id}.jpg",
            position=Vec3(x, 1.0, 1.6),
            yaw=yaw,
            pitch_deg=-5.0,
            vertical_fov_deg=55.0,
            aspect=16 / 9,
            confidence=0.8,
        )

    return SceneGraph(
        rooms=[room("room_a", 0.0, 6.0), room("room_b", 6.0, 10.0)],
        walls=[
            Wall(id="room_a_w0", start=(0.0, 0.0), end=(6.0, 0.0)),
            Wall(id="room_b_w0", start=(6.0, 0.0), end=(10.0, 0.0)),
        ],
        floor=Finish(material="wood_light", color_hex="#D8B98C"),
        ceiling=Finish(material="gypsum", color_hex="#F7F6F3"),
        objects=[
            SceneObject(id="sofa_1", category="sofa", room_id="room_a",
                        position=Vec3(2.0, 1.0, 0.0),
                        dimensions=Dimensions(2.0, 0.9, 0.8),
                        material="fabric", color_hex="#6E6A63", confidence=0.9),
            SceneObject(id="table_1", category="dining_table", room_id="room_b",
                        position=Vec3(8.0, 2.0, 0.0),
                        dimensions=Dimensions(1.4, 0.8, 0.75),
                        material="wood", color_hex="#C9A47A", confidence=0.9),
        ],
        lights=[
            LightSource(id="lamp_1", kind="pendant_light", room_id="room_a",
                        position=Vec3(2.0, 2.0, 2.6), power_w=60.0),
            LightSource(id="lamp_2", kind="downlight", room_id="room_b",
                        position=Vec3(8.0, 2.0, 2.8), power_w=40.0),
        ],
        viewpoints=[
            viewpoint("img_a1", "room_a", 1.0),
            viewpoint("img_a2", "room_a", 4.0, yaw=180.0),
            viewpoint("img_b1", "room_b", 8.0),
        ],
    )


@pytest.fixture
def preview_graph():
    return build_preview_graph()


# ---------------------------------------------------------------------------
# Reconstruction evaluation
# ---------------------------------------------------------------------------


def write_flat(path, colour, size=(64, 36), speckle=0, seed=0):
    """A solid image, optionally speckled to give it texture energy.

    Synthetic rather than photographic on purpose: an axis test has to show
    that a *known* difference produces the right finding, and only a
    constructed pair makes the expected answer unambiguous. A real photograph
    would test the metric's taste instead of its behaviour.
    """
    import random

    from PIL import Image

    image = Image.new("RGB", size, tuple(colour))
    if speckle:
        rng = random.Random(seed)
        pixels = image.load()
        for _ in range(speckle):
            x = rng.randrange(size[0])
            y = rng.randrange(size[1])
            jitter = rng.randint(-60, 60)
            pixels[x, y] = tuple(
                max(0, min(255, channel + jitter)) for channel in colour
            )
    image.save(str(path))
    return str(path)


def write_index_pass(path, index, size=(64, 36), split=None):
    """An ID pass: one index everywhere, or two split down the middle."""
    from PIL import Image

    from render import passes as passes_mod

    def encode(value):
        return tuple(int(round(c * 255)) for c in passes_mod.encode_index(value))

    image = Image.new("RGB", size, encode(index))
    if split is not None:
        pixels = image.load()
        for x in range(size[0] // 2, size[0]):
            for y in range(size[1]):
                pixels[x, y] = encode(split)
    image.save(str(path))
    return str(path)


def write_depth_pass(path, metres, size=(64, 36), depth_range=20.0):
    """A depth pass at a uniform distance."""
    from PIL import Image

    from render import passes as passes_mod

    value = int(round(passes_mod.encode_depth(metres, depth_range) * 255))
    Image.new("RGB", size, (value, value, value)).save(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# Planning and optimisation
# ---------------------------------------------------------------------------


def make_finding(**overrides):
    """One evaluation finding, with plausible defaults.

    Findings are the planner's only input, so tests build them directly rather
    than by running an evaluation: the question under test is what the planner
    does with a *given* finding, and generating one through the pixel axes
    would make the input a variable rather than a fixture.
    """
    from evaluation.schema import Finding, Subsystem

    settings = dict(
        axis="lighting",
        code="exposure",
        summary="Render is darker than the reference",
        subsystem=Subsystem.LIGHTING_ENVIRONMENT,
        difference=0.2, severity=0.5, confidence=0.8,
        why="because the measurement says so",
        room="room_a", viewpoint="img_a1",
        evidence={"reference_luminance": 0.42, "render_luminance": 0.19},
    )
    settings.update(overrides)
    return Finding(**settings)


def make_evaluation(findings=(), score=0.62, axis_scores=None, room_scores=None,
                    unmeasured=()):
    """A complete EvaluationResult carrying the given findings.

    Assembled the way the engine assembles one — findings hang off a
    ViewpointEvaluation, and ``result.findings`` collects and merges them — so
    the planner's adapter is exercised against the real shape rather than a
    convenient stand-in.
    """
    from evaluation.schema import (
        AXES, AxisScore, BuildingSummary, EvaluationResult, RoomEvaluation,
        ScoreSet, ViewpointEvaluation,
    )

    axis_scores = axis_scores or {
        "colour": 0.55, "material": 0.40, "lighting": 0.50,
        "layout": 0.60, "objects": 0.90,
    }
    axes = {}
    for axis in AXES:
        if axis in unmeasured:
            axes[axis] = AxisScore.unmeasured(axis, "not measured in this fixture")
        else:
            axes[axis] = AxisScore(axis=axis, score=axis_scores.get(axis, 0.5),
                                   measured=True, confidence=0.8)

    findings = list(findings)
    viewpoint = ViewpointEvaluation(
        viewpoint_id="img_a1", room="room_a", axes=dict(axes),
        findings=[f for f in findings if f.viewpoint],
        totals=ScoreSet(score=score, confidence=0.8, weight_used=1.0),
    )
    room = RoomEvaluation(
        room_id="room_a", room_type="living_room", axes=dict(axes),
        findings=[f for f in findings if not f.viewpoint],
        totals=ScoreSet(score=score, confidence=0.8, weight_used=1.0),
    )
    building = BuildingSummary(
        totals=ScoreSet(score=score, confidence=0.8,
                        measured_axes=[a for a in AXES if a not in unmeasured],
                        unmeasured_axes=list(unmeasured), weight_used=1.0),
        axes=axes,
        room_scores=room_scores or {"room_a": 0.60, "room_b": 0.65},
        findings=findings,
    )
    return EvaluationResult(building=building, rooms=[room], viewpoints=[viewpoint])


@pytest.fixture
def finding():
    return make_finding


@pytest.fixture
def evaluation():
    return make_evaluation


@pytest.fixture
def lighting_findings():
    """The spec's example: three complaints about one room's light."""
    from evaluation.schema import Subsystem

    return [
        make_finding(summary="Render is darker than the reference", severity=0.7,
                     evidence={"reference_luminance": 0.42, "render_luminance": 0.19}),
        make_finding(code="warmth",
                     summary="Render's light is warmer than the reference",
                     severity=0.5, evidence={"warmth_difference": 0.12}),
        make_finding(code="contrast",
                     summary="Render's lighting is flatter than the reference",
                     severity=0.4,
                     evidence={"reference_contrast": 0.21, "render_contrast": 0.13}),
    ]


@pytest.fixture
def evaluation_project(tmp_path, preview_graph):
    """A complete, self-contained build for the evaluation engine to read.

    Reference photographs, renders, every auxiliary pass, and a manifest that
    ties them together — everything Phase 2 would have produced, without
    needing Blender to produce it.
    """
    pytest.importorskip("PIL.Image", reason="the evaluation engine needs Pillow")
    pytest.importorskip("numpy")

    from render import passes as passes_mod
    from render.manifest import Manifest, RenderRecord

    preview = tmp_path / "output" / "preview"
    references = tmp_path / "reference_images"
    for directory in (preview, references):
        directory.mkdir(parents=True, exist_ok=True)

    # Objects are recorded against the images their viewpoints stand in for,
    # so the object and layout axes have provenance to work from.
    for obj in preview_graph.objects:
        obj.source_images = ["img_a1", "img_a2"] if obj.room_id == "room_a" else ["img_b1"]

    manifest = Manifest(root=str(preview))
    for viewpoint in preview_graph.viewpoints:
        room_dir = preview / viewpoint.room_id
        room_dir.mkdir(exist_ok=True)
        number = 1 + sorted(
            v.image_id for v in preview_graph.viewpoints
            if v.room_id == viewpoint.room_id
        ).index(viewpoint.image_id)
        stem = room_dir / f"viewpoint_{number:02d}.png"

        # The render is a little darker and less saturated than the reference,
        # so several axes have a known, signed difference to find.
        write_flat(references / f"{viewpoint.image_id}.jpg", (200, 170, 140), speckle=400)
        write_flat(stem, (150, 140, 135), speckle=60)

        passes = {}
        passes["albedo"] = write_flat(
            preview / viewpoint.room_id / f"viewpoint_{number:02d}_albedo.png",
            (160, 150, 145), speckle=40)
        passes["depth"] = write_depth_pass(
            preview / viewpoint.room_id / f"viewpoint_{number:02d}_depth.png", 3.0)
        passes["normal"] = write_flat(
            preview / viewpoint.room_id / f"viewpoint_{number:02d}_normal.png",
            (128, 0, 128))
        passes["material_id"] = write_index_pass(
            preview / viewpoint.room_id / f"viewpoint_{number:02d}_material_id.png",
            1, split=2)
        passes["object_id"] = write_index_pass(
            preview / viewpoint.room_id / f"viewpoint_{number:02d}_object_id.png",
            1, split=2)

        manifest.upsert(RenderRecord(
            viewpoint_id=viewpoint.image_id,
            room=viewpoint.room_id,
            image=f"{viewpoint.room_id}/viewpoint_{number:02d}.png",
            source_image=f"{viewpoint.image_id}.jpg",
            camera_hash="cam", scene_hash="scene", room_hash="room",
            width=64, height=36, timestamp="2026-01-01T00:00:00Z", render_ms=10,
            passes={
                name: os.path.relpath(path, str(preview)).replace("\\", "/")
                for name, path in passes.items()
            },
        ))

    manifest.stats = {
        "pass_index": {
            "objects": {"1": "sofa_1", "2": "table_1"},
            "materials": {"1": "WallMaterial", "2": "M_wood_light_D8B98C"},
        }
    }
    manifest_path = preview / "manifest.json"
    manifest.save(str(manifest_path))

    return {
        "base_dir": str(tmp_path),
        "graph": preview_graph,
        "manifest_path": str(manifest_path),
        "manifest": Manifest.load(str(manifest_path)),
        "preview_dir": str(preview),
        "references": str(references),
        "output_dir": str(tmp_path / "output" / "evaluation"),
        "passes": passes_mod.ALL_PASSES,
    }
