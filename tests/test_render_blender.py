"""
End-to-end smoke test against a real Blender.

Opt-in: set ``ARCHX3D_RENDER_INTEGRATION=1`` to run it. Every behaviour worth
asserting about scheduling, caching and the manifest is pinned in
``test_render_pipeline`` with a fake executor and runs in under a second; this
file exists to check the one thing a fake cannot — that the settings in
``renderer``'s table are real properties on a real Blender, that a camera
rebuilt from a stored ViewPoint actually renders, and that the result is
reproducible to the byte.

    ARCHX3D_RENDER_INTEGRATION=1 python -m pytest tests/test_render_blender.py -v
"""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from render.preview import PreviewConfig, PreviewPipeline
from render.renderer import RenderSettings, blender_executable

pytestmark = pytest.mark.skipif(
    os.environ.get("ARCHX3D_RENDER_INTEGRATION") != "1",
    reason="set ARCHX3D_RENDER_INTEGRATION=1 to run the Blender smoke test",
)

BLENDER = blender_executable()

#: A scene of its own rather than the project's ``output/scene.blend``: the
#: test must not depend on someone having run the generator first, and a cube
#: exercises exactly the same render path as a furnished room.
_BUILD_SCENE = """
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 4, 1))
material = bpy.data.materials.new("Probe")
material.use_nodes = True
bpy.context.object.data.materials.append(material)
bpy.ops.object.light_add(type='SUN', location=(0, 0, 6))
bpy.context.object.data.energy = 5.0
bpy.ops.wm.save_as_mainfile(filepath=r"{path}")
"""


@pytest.fixture(scope="module")
def blend_file(tmp_path_factory):
    if not BLENDER:
        pytest.skip("no Blender executable found")
    directory = tmp_path_factory.mktemp("blend")
    path = str(directory / "probe.blend")
    script = directory / "build.py"
    script.write_text(_BUILD_SCENE.format(path=path), encoding="utf-8")

    subprocess.run(
        [BLENDER, "--background", "--factory-startup", "--python", str(script)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if not os.path.exists(path):
        pytest.skip("could not build a probe .blend")
    return path


@pytest.fixture
def config(tmp_path, blend_file):
    return PreviewConfig(
        base_dir=str(tmp_path),
        blend_path=blend_file,
        preview_dir=str(tmp_path / "preview"),
        manifest_path=str(tmp_path / "preview" / "manifest.json"),
        cache_path=str(tmp_path / "cache" / "hash.json"),
        geometry_path=str(tmp_path / "geometry.json"),
        # Small and cheap: this test is about the path working, not quality.
        settings=RenderSettings(width=160, height=90, samples=4),
        verbose=False,
    )


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_a_stored_viewpoint_renders_to_a_png(preview_graph, config):
    report = PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")

    assert report.rendered == 1, report.records[0].error if report.records else report.notes
    record = report.records[0]
    image = os.path.join(config.preview_dir, "room_a", "viewpoint_01.png")
    assert os.path.exists(image) and os.path.getsize(image) > 0
    assert (record.width, record.height) == (160, 90)
    # The probe .blend has no Ref_ cameras, so this exercises the rebuild path.
    assert record.camera_source == "graph"


def test_identical_scenes_produce_identical_images(preview_graph, config):
    """The contract in one assertion: same inputs, same bytes.

    Not merely the same pixels — the stamp metadata is off precisely so that a
    regression check can be a file comparison.
    """
    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")
    image = os.path.join(config.preview_dir, "room_a", "viewpoint_01.png")
    first = digest(image)

    config.force = True
    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")
    assert digest(image) == first


def test_a_second_pass_hits_the_cache_and_starts_no_blender(preview_graph, config):
    import time

    PreviewPipeline(preview_graph, config).render_scene()

    started = time.perf_counter()
    report = PreviewPipeline(preview_graph, config).render_scene()
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert report.cached == 3 and report.rendered == 0
    # A cached pass touches no process at all; the budget is 500 ms.
    assert elapsed_ms < 500


def test_every_viewpoint_of_the_graph_renders_in_one_process(preview_graph, config):
    report = PreviewPipeline(preview_graph, config).render_scene()
    assert report.rendered == 3
    assert report.failed == 0
    # One beauty render plus its passes, per viewpoint.
    per_view = 1 + len(config.settings.passes)
    for room, views in (("room_a", 2), ("room_b", 1)):
        assert len(os.listdir(os.path.join(config.preview_dir, room))) == views * per_view


# ---------------------------------------------------------------------------
# Auxiliary passes
# ---------------------------------------------------------------------------


def test_every_requested_pass_is_written_and_recorded(preview_graph, config):
    from render import passes as passes_mod
    from render.manifest import Manifest

    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")

    record = Manifest.load(config.manifest_path).record_for("img_a1")
    assert set(record.passes) == set(passes_mod.DEFAULT_PASSES)
    for relative in record.passes.values():
        path = os.path.join(os.path.dirname(config.manifest_path), relative)
        assert os.path.exists(path) and os.path.getsize(path) > 0


def test_the_depth_pass_decodes_to_believable_metres(preview_graph, config):
    """The codec's contract against a real render: the probe cube stands 4 m
    ahead of a camera 1 m inside the room, so nothing may decode as 40 m."""
    numpy = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    from render import passes as passes_mod

    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")
    path = passes_mod.pass_filename(
        os.path.join(config.preview_dir, "room_a", "viewpoint_01.png"), "depth")

    plane = numpy.asarray(Image.open(path).convert("RGB"))[..., 0]
    surfaces = plane[plane > 0]
    assert surfaces.size > 0, "the probe scene should be visible from this camera"
    metres = passes_mod.decode_depth(surfaces.astype(float), config.settings.depth_range)
    assert 0.1 < float(metres.min()) < 30.0
    assert float(metres.max()) <= config.settings.depth_range


def test_the_id_passes_decode_to_indices_the_manifest_can_name(preview_graph, config):
    """Anti-aliasing would blend indices at silhouettes and invent regions
    that were never rendered; the data passes are point-sampled to stop it."""
    numpy = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    from render import passes as passes_mod
    from render.manifest import Manifest

    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")

    manifest = Manifest.load(config.manifest_path)
    index_map = passes_mod.IndexMap.from_dict(manifest.stats.get("pass_index"))
    assert index_map, "the ID passes are unreadable without their index map"

    for pass_name, lookup in (("material_id", index_map.materials),
                              ("object_id", index_map.objects)):
        path = passes_mod.pass_filename(
            os.path.join(config.preview_dir, "room_a", "viewpoint_01.png"), pass_name)
        raw = numpy.asarray(Image.open(path).convert("RGB")).astype(int)
        indices = numpy.unique(passes_mod.decode_index(raw[..., 0], raw[..., 1]))
        present = [int(i) for i in indices if i > 0]
        assert present, f"{pass_name} rendered nothing"
        unresolvable = [i for i in present if i not in lookup]
        assert not unresolvable, f"{pass_name} holds indices nobody can name: {unresolvable}"


def test_the_albedo_pass_is_unlit(preview_graph, config):
    """Emission shading means the scene's lights cannot reach the albedo —
    which is the whole reason it can tell a dark room from dark paint.

    Demonstrated by brightness rather than by variance: the probe scene is lit
    by one weak sun, so its surfaces render dim, while their albedo is the
    material's own colour at full strength. The depth pass supplies the mask,
    because it is point-sampled and therefore free of the anti-aliased edge
    pixels that would otherwise dominate either statistic.
    """
    numpy = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    from render import passes as passes_mod

    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")
    stem = os.path.join(config.preview_dir, "room_a", "viewpoint_01.png")

    def load(path):
        return numpy.asarray(Image.open(path).convert("RGB")).astype(float)

    beauty = load(stem)
    albedo = load(passes_mod.pass_filename(stem, "albedo"))
    depth = load(passes_mod.pass_filename(stem, "depth"))[..., 0]

    surface = depth > 0
    assert surface.any(), "the probe cube should be visible from this camera"
    assert albedo[surface].mean() > beauty[surface].mean() * 2.0


def test_passes_can_be_switched_off_entirely(preview_graph, config):
    from render.renderer import RenderSettings

    config.settings = RenderSettings(width=160, height=90, samples=4, passes=())
    PreviewPipeline(preview_graph, config).render_viewpoint("img_a1")

    files = os.listdir(os.path.join(config.preview_dir, "room_a"))
    assert files == ["viewpoint_01.png"]
