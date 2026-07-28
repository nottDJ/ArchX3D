"""
Tests for the five evaluation axes.

Each test constructs a *known* difference and asserts that the right axis
notices it, quantifies it correctly, and — the part that matters — nominates
the right subsystem. A test that only checked "the score went down" would pass
just as happily for an engine that blamed the lighting for a missing sofa,
which is precisely the failure this phase exists to avoid.

Synthetic images throughout. A photograph would test the metrics' taste; a
constructed pair tests their behaviour, which is the thing that has to be
right.
"""

from __future__ import annotations

import pytest

from conftest import write_depth_pass, write_flat, write_index_pass
from evaluation import imaging
from evaluation.context import ViewContext
from evaluation.axes import colour as colour_axis
from evaluation.axes import layout as layout_axis
from evaluation.axes import lighting as lighting_axis
from evaluation.axes import material as material_axis
from evaluation.axes import objects as objects_axis
from evaluation.projection import Camera
from evaluation.schema import Subsystem
from render.passes import IndexMap

pytestmark = pytest.mark.skipif(
    not imaging.available(), reason="the pixel axes need numpy and Pillow"
)

SIZE = (64, 36)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def context(tmp_path, reference, render, passes=None, graph=None, viewpoint=None,
            index_map=None, room=None, viewpoint_id="img_a1", room_id="room_a"):
    """A ViewContext over images written to disk, as the engine builds one."""
    pair = imaging.load_pair(reference, render)
    for name, path in (passes or {}).items():
        size = pair.shape
        if name in ("material_id", "object_id", "depth", "normal"):
            pair.passes[name] = imaging.load_raw(path, size=size)
        else:
            pair.passes[name] = imaging.load_rgb(path, size=size)
    return ViewContext(
        viewpoint_id=viewpoint_id, room_id=room_id, viewpoint=viewpoint,
        graph=graph, room=room, pair=pair,
        index_map=index_map or IndexMap(
            {1: "sofa_1", 2: "table_1"},
            {1: "WallMaterial", 2: "M_wood_light_D8B98C"},
        ),
        camera=Camera.from_viewpoint(viewpoint) if viewpoint is not None else None,
        config=None,
    )


@pytest.fixture
def images(tmp_path):
    """A neutral reference and a matching render, ready to be perturbed."""
    def build(reference_colour=(180, 170, 160), render_colour=(180, 170, 160),
              reference_speckle=300, render_speckle=300, **extra):
        reference = write_flat(tmp_path / "reference.jpg", reference_colour,
                               SIZE, speckle=reference_speckle, seed=1)
        render = write_flat(tmp_path / "render.png", render_colour,
                            SIZE, speckle=render_speckle, seed=1)
        passes = {}
        for name, colour in extra.items():
            passes[name] = write_flat(tmp_path / f"{name}.png", colour, SIZE,
                                      speckle=render_speckle, seed=1)
        return reference, render, passes
    return build


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def test_identical_frames_score_near_perfect_and_find_nothing(tmp_path, images):
    reference, render, _ = images()
    score, findings = colour_axis.evaluate(context(tmp_path, reference, render))

    assert score.measured
    assert score.score > 0.95
    assert findings == []


def test_a_warm_render_is_reported_as_warmer(tmp_path, images):
    reference, render, _ = images(reference_colour=(150, 150, 150),
                                  render_colour=(210, 150, 90))
    score, findings = colour_axis.evaluate(context(tmp_path, reference, render))

    assert score.score < 0.8
    cast = next(f for f in findings if "reads" in f.summary)
    assert "warmer" in cast.summary
    assert cast.unit == "dE"
    assert cast.difference > 8.0


def test_a_cast_is_blamed_on_the_light_when_the_albedo_is_right(tmp_path, images):
    """The albedo pass earning its keep: same surfaces, wrong light."""
    reference, render, passes = images(
        reference_colour=(150, 150, 150), render_colour=(210, 150, 90),
        albedo=(150, 150, 150),
    )
    _score, findings = colour_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    cast = next(f for f in findings if "reads" in f.summary)
    assert cast.subsystem == Subsystem.LIGHTING_ENVIRONMENT
    assert "albedo" in cast.why


def test_a_cast_is_blamed_on_the_finish_when_the_albedo_is_wrong_too(tmp_path, images):
    reference, render, passes = images(
        reference_colour=(150, 150, 150), render_colour=(210, 150, 90),
        albedo=(215, 155, 95),
    )
    _score, findings = colour_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    cast = next(f for f in findings if "reads" in f.summary)
    assert cast.subsystem == Subsystem.SURFACE_FINISH


def test_without_an_albedo_pass_the_attribution_is_admitted_as_uncertain(tmp_path, images):
    reference, render, _ = images(reference_colour=(150, 150, 150),
                                  render_colour=(210, 150, 90))
    _score, findings = colour_axis.evaluate(context(tmp_path, reference, render))
    cast = next(f for f in findings if "reads" in f.summary)
    assert cast.confidence <= 0.5
    assert "cannot be separated" in cast.why


def test_a_colour_difference_is_localised_to_the_material_that_owns_it(tmp_path):
    """The material-ID pass is what turns a score into an address."""
    from PIL import Image

    reference = write_flat(tmp_path / "reference.jpg", (180, 175, 170), SIZE, speckle=200)
    # Left half matches; right half — the wood — is badly off.
    render_image = Image.new("RGB", SIZE, (180, 175, 170))
    pixels = render_image.load()
    for x in range(SIZE[0] // 2, SIZE[0]):
        for y in range(SIZE[1]):
            pixels[x, y] = (40, 90, 190)
    render = str(tmp_path / "render.png")
    render_image.save(render)

    passes = {"material_id": write_index_pass(tmp_path / "mid.png", 1, SIZE, split=2)}
    _score, findings = colour_axis.evaluate(context(tmp_path, reference, render, passes))

    localised = [f for f in findings if f.materials]
    assert localised, "the material-ID pass should localise the difference"
    named = localised[0]
    assert named.materials == ["M_wood_light_D8B98C"]
    assert "wood light" in named.summary
    assert named.subsystem == Subsystem.SURFACE_FINISH


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


def test_a_dark_render_is_reported_as_darker(tmp_path, images):
    reference, render, _ = images(reference_colour=(200, 200, 200),
                                  render_colour=(90, 90, 90))
    score, findings = lighting_axis.evaluate(context(tmp_path, reference, render))

    exposure = next(f for f in findings if "darker" in f.summary)
    assert score.score < 0.7
    assert exposure.difference > 0.06
    assert exposure.axis == "lighting"


def test_darkness_is_blamed_on_the_light_when_the_surfaces_are_right(tmp_path, images):
    """The spec's example, and the reason the albedo pass exists."""
    reference, render, passes = images(reference_colour=(200, 200, 200),
                                       render_colour=(90, 90, 90),
                                       albedo=(200, 200, 200))
    _score, findings = lighting_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    exposure = next(f for f in findings if "darker" in f.summary)
    assert exposure.subsystem == Subsystem.LIGHTING_ENVIRONMENT
    assert exposure.confidence >= 0.8


def test_darkness_is_blamed_on_the_finish_when_the_surfaces_are_dark_too(tmp_path, images):
    reference, render, passes = images(reference_colour=(200, 200, 200),
                                       render_colour=(90, 90, 90),
                                       albedo=(85, 85, 85))
    _score, findings = lighting_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    exposure = next(f for f in findings if "darker" in f.summary)
    assert exposure.subsystem == Subsystem.SURFACE_FINISH


def test_a_warm_light_is_reported_with_its_recorded_temperature(tmp_path, images):
    from vision.schema import LightingEnvironment, Room

    reference, render, _ = images(reference_colour=(160, 160, 180),
                                  render_colour=(210, 160, 110))
    room = Room(id="room_a", lighting=LightingEnvironment(color_temperature_k=2200.0))
    _score, findings = lighting_axis.evaluate(
        context(tmp_path, reference, render, room=room)
    )
    warmth = next(f for f in findings if "warmer" in f.summary)
    assert warmth.evidence["recorded_color_temperature_k"] == 2200.0
    assert "2200" in warmth.remedy


def test_matching_lighting_produces_no_findings(tmp_path, images):
    reference, render, _ = images()
    score, findings = lighting_axis.evaluate(context(tmp_path, reference, render))
    assert score.score > 0.9
    assert findings == []


# ---------------------------------------------------------------------------
# Material
# ---------------------------------------------------------------------------


def test_a_desaturated_material_region_is_named(tmp_path):
    """The spec's case: a walnut floor rendered too grey."""
    from PIL import Image

    reference_image = Image.new("RGB", SIZE, (190, 185, 180))
    pixels = reference_image.load()
    for x in range(SIZE[0] // 2, SIZE[0]):
        for y in range(SIZE[1]):
            pixels[x, y] = (170, 95, 30)          # a saturated wood
    reference = str(tmp_path / "reference.jpg")
    reference_image.save(reference)

    render = write_flat(tmp_path / "render.png", (150, 145, 142), SIZE)
    albedo = write_flat(tmp_path / "albedo.png", (150, 145, 142), SIZE)
    passes = {
        "albedo": albedo,
        "material_id": write_index_pass(tmp_path / "mid.png", 1, SIZE, split=2),
    }

    _score, findings = material_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    washed = [f for f in findings if "desaturated" in f.summary]
    assert washed, "a grey render of a saturated material should be reported"
    assert washed[0].materials == ["M_wood_light_D8B98C"]
    assert washed[0].subsystem == Subsystem.MATERIAL_SPECIES


def test_flat_surfaces_against_a_textured_reference_are_reported(tmp_path, images):
    reference, render, passes = images(reference_speckle=900, render_speckle=0,
                                       albedo=(180, 170, 160))
    score, findings = material_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )
    texture = next(f for f in findings if "texture" in f.summary)
    assert "less" in texture.summary
    assert texture.subsystem == Subsystem.MATERIAL_SPECIES
    assert score.score < 0.9


def test_the_material_axis_says_which_source_it_used(tmp_path, images):
    """Without an albedo pass the measurement is weaker, and must admit it."""
    reference, render, _ = images()
    with_beauty, _ = material_axis.evaluate(context(tmp_path, reference, render))

    reference, render, passes = images(albedo=(180, 170, 160))
    with_albedo, _ = material_axis.evaluate(
        context(tmp_path, reference, render, passes)
    )

    assert with_beauty.detail["source"] == "beauty render"
    assert with_albedo.detail["source"] == "albedo"
    assert with_albedo.confidence > with_beauty.confidence


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def displaced_graph(offset_x=0.0, offset_y=0.0, per_object=None):
    """A graph whose objects sit a known distance from their detections."""
    from vision.schema import BBox2D, Dimensions, Room, SceneGraph, SceneObject, Vec3, ViewPoint

    viewpoint = ViewPoint(image_id="img_a1", room_id="room_a",
                          position=Vec3(0.0, 0.0, 1.6), yaw=0.0, pitch_deg=-15.0,
                          vertical_fov_deg=60.0, aspect=16 / 9)
    camera = Camera.from_viewpoint(viewpoint)

    objects = []
    for index, u in enumerate((0.3, 0.5, 0.7, 0.45)):
        box = BBox2D(x0=u - 0.05, y0=0.55, x1=u + 0.05, y1=0.8 + index * 0.02)
        implied = camera.ground_position(box)
        shift = (per_object or {}).get(index, (0.0, 0.0))
        objects.append(SceneObject(
            id=f"obj_{index}", category="coffee_table", room_id="room_a",
            position=Vec3(implied[0] - offset_x - shift[0],
                          implied[1] - offset_y - shift[1], 0.0),
            dimensions=Dimensions(0.8, 0.5, 0.4),
            bbox_2d=box, source_images=["img_a1"], confidence=0.9,
        ))

    return SceneGraph(rooms=[Room(id="room_a", area=20.0)], objects=objects,
                      viewpoints=[viewpoint]), viewpoint


def test_a_displaced_object_is_reported_in_centimetres(tmp_path, images):
    graph, viewpoint = displaced_graph(per_object={0: (0.0, 0.38)})
    reference, render, _ = images()

    score, findings = layout_axis.evaluate(
        context(tmp_path, reference, render, graph=graph, viewpoint=viewpoint)
    )
    moved = [f for f in findings if f.objects == ["obj_0"]]
    assert moved, "a 38 cm displacement should be reported"
    assert moved[0].difference == pytest.approx(0.38, abs=0.02)
    assert moved[0].unit == "m"
    assert "38 cm" in moved[0].summary
    assert moved[0].subsystem == Subsystem.SCENE_GRAPH_TRANSFORM
    assert score.detail["displacement"]["measured_objects"] == 4


def test_objects_where_the_graph_says_produce_no_displacement_findings(tmp_path, images):
    graph, viewpoint = displaced_graph()
    reference, render, _ = images()

    score, findings = layout_axis.evaluate(
        context(tmp_path, reference, render, graph=graph, viewpoint=viewpoint)
    )
    assert [f for f in findings if f.objects] == []
    assert score.detail["displacement"]["mean_m"] < 0.01


def test_a_shared_offset_is_blamed_on_the_camera_not_the_furniture(tmp_path, images):
    """One CameraFit finding beats fifteen 'move this sofa' findings."""
    graph, viewpoint = displaced_graph(offset_x=0.6, offset_y=0.9)
    reference, render, _ = images()

    score, findings = layout_axis.evaluate(
        context(tmp_path, reference, render, graph=graph, viewpoint=viewpoint)
    )
    camera_findings = [f for f in findings if f.subsystem == Subsystem.CAMERA_FIT]
    assert len(camera_findings) == 1
    assert camera_findings[0].difference == pytest.approx(
        (0.6 ** 2 + 0.9 ** 2) ** 0.5, abs=0.05
    )
    assert score.detail["displacement"]["systematic"]["systematic"]
    # The objects themselves are correct relative to each other, so none of
    # them should be blamed individually.
    assert [f for f in findings if f.objects] == []


def test_one_object_out_of_place_is_not_blamed_on_the_camera(tmp_path, images):
    graph, viewpoint = displaced_graph(per_object={2: (0.9, 0.9)})
    reference, render, _ = images()

    _score, findings = layout_axis.evaluate(
        context(tmp_path, reference, render, graph=graph, viewpoint=viewpoint)
    )
    assert not [f for f in findings if f.subsystem == Subsystem.CAMERA_FIT]
    assert [f.objects for f in findings if f.objects] == [["obj_2"]]


def test_layout_falls_back_to_visual_mass_without_a_camera(tmp_path, images):
    reference, render, _ = images()
    score, _findings = layout_axis.evaluate(context(tmp_path, reference, render))

    assert score.measured
    assert score.detail["displacement"]["measured_objects"] == 0
    assert score.confidence < 0.7          # a weaker claim, and it says so


def test_the_depth_pass_is_reported_as_evidence(tmp_path, images):
    reference, render, _ = images()
    passes = {"depth": write_depth_pass(tmp_path / "depth.png", 3.0, SIZE)}
    score, _ = layout_axis.evaluate(context(tmp_path, reference, render, passes))

    profile = score.detail["depth_profile"]
    assert profile["median_m"] == pytest.approx(3.0, abs=0.1)
    assert profile["surfaces"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


def object_graph(**overrides):
    from vision.schema import Dimensions, Room, SceneGraph, SceneObject, Vec3

    def make(object_id, **kwargs):
        settings = dict(id=object_id, category="plant", room_id="room_a",
                        position=Vec3(1.0, 1.0, 0.0),
                        dimensions=Dimensions(0.5, 0.5, 1.2),
                        source_images=["img_a1"], confidence=0.9,
                        asset="plant_tall", asset_score=0.9)
        settings.update(kwargs)
        return SceneObject(**settings)

    objects = [make("sofa_1", category="sofa",
                    dimensions=Dimensions(2.0, 0.9, 0.8))]
    objects.extend(make(key, **value) for key, value in overrides.items())
    return SceneGraph(rooms=[Room(id="room_a", area=20.0)], objects=objects)


def test_a_low_confidence_object_is_reported_as_omitted_with_its_reason(tmp_path):
    """The spec's example: plant omitted, confidence below threshold."""
    graph = object_graph(plant_1={"confidence": 0.44, "uncertain": True})
    score, findings = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=graph)
    )

    omitted = next(f for f in findings if "Plant omitted" in f.summary)
    assert omitted.subsystem == Subsystem.OBJECT_DETECTION
    assert "0.44" in omitted.why
    assert "threshold" in omitted.why
    assert score.score < 1.0
    assert score.detail["missing"][0]["id"] == "plant_1"


def test_a_stand_in_asset_is_reported_as_a_substitution(tmp_path):
    graph = object_graph(plant_1={"asset": "box", "asset_score": 0.2})
    score, findings = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=graph)
    )
    substituted = next(f for f in findings if "stand-in" in f.summary)
    assert substituted.subsystem == Subsystem.ASSET_PLACEMENT
    assert substituted.objects == ["plant_1"]
    # Half a failure, not a whole one: the object is there, wearing the wrong
    # shape.
    assert 0.5 < score.score < 1.0


def test_everything_built_scores_perfectly(tmp_path):
    score, findings = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=object_graph())
    )
    assert score.score == 1.0
    assert findings == []


def test_the_object_axis_needs_no_images_at_all(tmp_path):
    """It is a graph comparison, which is why it survives a missing photograph."""
    score, _ = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=object_graph())
    )
    assert score.measured


def test_an_object_outside_this_photograph_is_not_missing_from_it(tmp_path):
    graph = object_graph(plant_1={"source_images": ["img_zz"]})
    score, findings = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=graph)
    )
    assert score.detail["observed"] == 1        # only the sofa
    assert findings == []


def test_room_scope_sees_what_no_single_photograph_framed(tmp_path):
    graph = object_graph(plant_1={"source_images": ["img_zz"], "uncertain": True,
                                  "confidence": 0.3})
    score, findings = objects_axis.evaluate(
        ViewContext(room_id="room_a", graph=graph), scope="room"
    )
    assert score.detail["observed"] == 2
    assert any("omitted" in f.summary for f in findings)


def test_a_graph_with_no_objects_is_unmeasured_not_perfect(tmp_path):
    from vision.schema import Room, SceneGraph

    graph = SceneGraph(rooms=[Room(id="room_a")], objects=[])
    score, _ = objects_axis.evaluate(
        ViewContext(viewpoint_id="img_a1", room_id="room_a", graph=graph)
    )
    assert not score.measured
    assert "no objects" in score.reason


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_every_pixel_axis_reports_why_it_could_not_run(tmp_path):
    """A missing reference must produce an explanation, not a zero."""
    render = write_flat(tmp_path / "render.png", (150, 150, 150), SIZE)
    ctx = context(tmp_path, str(tmp_path / "absent.jpg"), render)

    for axis_module in (colour_axis, material_axis, lighting_axis, layout_axis):
        score, findings = axis_module.evaluate(ctx)
        assert not score.measured
        assert score.reason
        assert findings == []
