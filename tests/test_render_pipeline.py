"""
Integration tests for the preview pipeline.

Blender is replaced by a fake executor that writes a byte or two to each output
path and records what it was asked for. That is not a shortcut: everything
worth asserting here — what got scheduled, what was skipped, what the manifest
says afterwards — is decided before Blender is ever reached, and pinning it
without a two-second process launch per case is what makes these tests
runnable in a loop.

The regression cases at the bottom are the pipeline's reason for existing:
edit one material, one light or one camera, and exactly the right previews
re-render.
"""

from __future__ import annotations

import copy
import os

import pytest

from render import manifest as manifest_mod
from render import passes as passes_mod
from render.preview import PreviewConfig, PreviewPipeline
from render.renderer import RenderSettings
from render.scheduler import RenderOutcome


class FakeBlender:
    """Stands in for a Blender subprocess: writes the files, records the call.

    Writes the auxiliary passes too, because the pipeline treats a preview
    whose passes are missing as unusable — a fake that only wrote the beauty
    image would make every second run a cache miss and hide that behaviour
    rather than test it.

    Also satisfies the ``available()`` protocol the pipeline uses to warn about
    a missing renderer, so the "no Blender installed" path stays exercised by
    its own test rather than by every test accidentally.
    """

    def __init__(self, fail: "set[str]" = frozenset(),
                 passes=passes_mod.DEFAULT_PASSES) -> None:
        self.rendered: list = []
        self.batches: list = []
        self.fail = set(fail)
        self.passes = tuple(passes)
        self.index_maps = {"objects": {"1": "sofa_1"}, "materials": {"1": "FloorMaterial"}}

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    @staticmethod
    def _write(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")

    def __call__(self, batch):
        self.batches.append([t.viewpoint_id for t in batch])
        outcomes = []
        for task in batch:
            if task.viewpoint_id in self.fail:
                outcomes.append(RenderOutcome(viewpoint_id=task.viewpoint_id, ok=False,
                                              error="fake failure"))
                continue
            self._write(task.output)
            written = {}
            for name in self.passes:
                path = passes_mod.pass_filename(task.output, name)
                self._write(path)
                written[name] = path
            self.rendered.append(task.viewpoint_id)
            outcomes.append(RenderOutcome(
                viewpoint_id=task.viewpoint_id, ok=True, render_ms=12,
                camera_source="blend", width=task.width, height=task.height,
                passes=written,
            ))
        return outcomes


@pytest.fixture
def config(tmp_path):
    """A pipeline rooted entirely inside the temp directory."""
    return PreviewConfig(
        base_dir=str(tmp_path),
        blend_path=str(tmp_path / "output" / "scene.blend"),
        graph_path=str(tmp_path / "data" / "scene_graph.json"),
        geometry_path=str(tmp_path / "data" / "geometry.json"),
        preview_dir=str(tmp_path / "output" / "preview"),
        manifest_path=str(tmp_path / "output" / "preview" / "manifest.json"),
        cache_path=str(tmp_path / ".cache" / "render" / "hash.json"),
        settings=RenderSettings(width=64, height=36, samples=1),
        verbose=False,
    )


def build(graph, config, executor):
    return PreviewPipeline(graph, config, executor=executor)


# ---------------------------------------------------------------------------
# Rendering a scene
# ---------------------------------------------------------------------------


def test_every_viewpoint_is_rendered_once(preview_graph, config):
    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_scene()

    assert report.rendered == 3
    assert report.cached == 0
    assert report.failed == 0
    assert sorted(blender.rendered) == ["img_a1", "img_a2", "img_b1"]


def test_images_land_in_a_directory_per_room(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    preview = config.preview_dir
    assert os.path.exists(os.path.join(preview, "room_a", "viewpoint_01.png"))
    assert os.path.exists(os.path.join(preview, "room_a", "viewpoint_02.png"))
    assert os.path.exists(os.path.join(preview, "room_b", "viewpoint_01.png"))


def test_the_manifest_describes_every_render(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    manifest = manifest_mod.Manifest.load(config.manifest_path)
    record = manifest.record_for("img_a2")
    assert record.room == "room_a"
    assert record.image == "room_a/viewpoint_02.png"
    assert record.source_image == "img_a2.jpg"
    assert record.width == 64 and record.height == 36
    assert record.camera_hash and record.scene_hash and record.room_hash
    assert record.status == manifest_mod.STATUS_RENDERED
    assert os.path.exists(manifest.resolve(record))


def test_one_blender_process_renders_the_whole_scene(preview_graph, config):
    """Process startup dominates, so the default must not launch one per view."""
    blender = FakeBlender()
    build(preview_graph, config, blender).render_scene()
    assert len(blender.batches) == 1


def test_a_graph_without_viewpoints_is_a_success_not_a_failure(preview_graph, config):
    """No reference photographs is a legitimate state of the pipeline."""
    preview_graph.viewpoints = []
    report = build(preview_graph, config, FakeBlender()).render_scene()

    assert report.total == 0
    assert report.ok
    assert os.path.exists(config.manifest_path)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_an_unchanged_scene_renders_nothing_the_second_time(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    second = FakeBlender()
    report = build(preview_graph, config, second).render_scene()

    assert report.cached == 3
    assert report.rendered == 0
    assert second.rendered == []
    assert second.batches == []          # Blender was never launched at all


def test_a_cache_hit_keeps_the_original_timestamp(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()
    first = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")

    build(preview_graph, config, FakeBlender()).render_scene()
    second = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")

    assert second.timestamp == first.timestamp
    assert second.render_ms == first.render_ms
    assert second.status == manifest_mod.STATUS_CACHED


def test_deleting_a_preview_re_renders_only_that_one(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()
    os.unlink(os.path.join(config.preview_dir, "room_b", "viewpoint_01.png"))

    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_scene()

    assert blender.rendered == ["img_b1"]
    assert report.cached == 2


def test_force_re_renders_everything(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    config.force = True
    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_scene()

    assert report.rendered == 3
    assert sorted(blender.rendered) == ["img_a1", "img_a2", "img_b1"]


def test_a_disabled_cache_never_hits(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    config.use_cache = False
    report = build(preview_graph, config, FakeBlender()).render_scene()
    assert report.rendered == 3


def test_changed_render_settings_invalidate_every_preview(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    config.settings = RenderSettings(width=64, height=36, samples=32)
    blender = FakeBlender()
    build(preview_graph, config, blender).render_scene()
    assert len(blender.rendered) == 3


# ---------------------------------------------------------------------------
# Incremental scopes
# ---------------------------------------------------------------------------


def test_rendering_one_room_touches_only_that_room(preview_graph, config):
    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_room("room_a")

    assert sorted(blender.rendered) == ["img_a1", "img_a2"]
    assert report.rendered == 2


def test_rendering_one_viewpoint_touches_only_that_viewpoint(preview_graph, config):
    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_viewpoint("img_b1")

    assert blender.rendered == ["img_b1"]
    assert report.rendered == 1


def test_a_room_scoped_run_does_not_narrow_the_manifest(preview_graph, config):
    """The similarity pass reads the manifest as the whole build.

    A partial run that dropped the rooms it skipped would be indistinguishable
    from a build that had lost half its previews.
    """
    build(preview_graph, config, FakeBlender()).render_scene()

    config.force = True
    build(preview_graph, config, FakeBlender()).render_room("room_a")

    manifest = manifest_mod.Manifest.load(config.manifest_path)
    assert len(manifest.records) == 3
    assert manifest.record_for("img_b1").status == manifest_mod.STATUS_RENDERED


def test_room_and_scene_scopes_agree_on_filenames(preview_graph, config, tmp_path):
    """``render_room`` must write the file ``render_scene`` would have."""
    build(preview_graph, config, FakeBlender()).render_room("room_a")
    from_room = sorted(os.listdir(os.path.join(config.preview_dir, "room_a")))

    for name in from_room:
        os.unlink(os.path.join(config.preview_dir, "room_a", name))
    os.unlink(config.cache_path)

    build(preview_graph, config, FakeBlender()).render_scene()
    assert sorted(os.listdir(os.path.join(config.preview_dir, "room_a"))) == from_room


def test_an_unknown_room_is_reported_not_raised(preview_graph, config):
    report = build(preview_graph, config, FakeBlender()).render_room("no_such_room")
    assert report.total == 0
    assert any("no_such_room" in note for note in report.notes)


def test_an_unknown_viewpoint_is_reported_not_raised(preview_graph, config):
    report = build(preview_graph, config, FakeBlender()).render_viewpoint("img_zz")
    assert report.total == 0
    assert any("img_zz" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Regression: a change re-renders exactly what it should
# ---------------------------------------------------------------------------


def rerender(graph, config):
    """Run a second pass over an already-rendered project, reporting the work."""
    blender = FakeBlender()
    report = build(graph, config, blender).render_scene()
    return sorted(blender.rendered), report


def test_changing_a_material_re_renders_only_its_room(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    changed = copy.deepcopy(preview_graph)
    changed.objects[0].material = "leather"      # the sofa, in room_a

    rendered, report = rerender(changed, config)
    assert rendered == ["img_a1", "img_a2"]
    assert report.cached == 1


def test_changing_lighting_re_renders_only_its_room(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    changed = copy.deepcopy(preview_graph)
    changed.rooms[1].lighting.time_of_day = "night"

    rendered, report = rerender(changed, config)
    assert rendered == ["img_b1"]
    assert report.cached == 2


def test_moving_a_camera_re_renders_only_its_viewpoint(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    changed = copy.deepcopy(preview_graph)
    changed.viewpoints[0].yaw = 42.0

    rendered, report = rerender(changed, config)
    assert rendered == ["img_a1"]
    assert report.cached == 2


def test_a_building_wide_change_re_renders_everything(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    changed = copy.deepcopy(preview_graph)
    changed.floor.color_hex = "#101010"

    rendered, _ = rerender(changed, config)
    assert rendered == ["img_a1", "img_a2", "img_b1"]


def test_deleting_a_viewpoint_drops_its_record(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    changed = copy.deepcopy(preview_graph)
    changed.viewpoints = [v for v in changed.viewpoints if v.image_id != "img_b1"]
    report = build(changed, config, FakeBlender()).render_scene()

    manifest = manifest_mod.Manifest.load(config.manifest_path)
    assert manifest.record_for("img_b1") is None
    assert any("no longer in the graph" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failed_render_is_recorded_and_not_cached(preview_graph, config):
    blender = FakeBlender(fail={"img_a1"})
    report = build(preview_graph, config, blender).render_scene()

    assert report.failed == 1
    assert report.rendered == 2
    assert not report.ok

    record = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")
    assert record.status == manifest_mod.STATUS_FAILED
    assert record.error == "fake failure"


def test_a_failure_is_retried_on_the_next_run(preview_graph, config):
    """A failed render must never register as a cache hit."""
    build(preview_graph, config, FakeBlender(fail={"img_a1"})).render_scene()

    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_scene()

    assert blender.rendered == ["img_a1"]
    assert report.cached == 2


def test_a_missing_blender_is_a_note_and_a_failure_not_a_crash(preview_graph, config):
    class NoBlender:
        def available(self):
            return False

        def unavailable_reason(self):
            return "no Blender executable found"

        def __call__(self, batch):
            return [RenderOutcome(viewpoint_id=t.viewpoint_id, ok=False,
                                  error="no Blender executable found") for t in batch]

    report = build(preview_graph, config, NoBlender()).render_scene()
    assert report.failed == 3
    assert any("no Blender" in note for note in report.notes)


# ---------------------------------------------------------------------------
# Auxiliary passes
# ---------------------------------------------------------------------------


def test_the_manifest_records_every_pass_written(preview_graph, config):
    build(preview_graph, config, FakeBlender()).render_scene()

    record = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")
    assert set(record.passes) == set(passes_mod.DEFAULT_PASSES)
    assert record.passes["albedo"] == "room_a/viewpoint_01_albedo.png"


def test_a_deleted_pass_re_renders_that_preview(preview_graph, config):
    """A preview missing its depth pass is not a usable evaluation input,
    however current the beauty render is."""
    build(preview_graph, config, FakeBlender()).render_scene()
    os.unlink(os.path.join(config.preview_dir, "room_a", "viewpoint_01_depth.png"))

    blender = FakeBlender()
    report = build(preview_graph, config, blender).render_scene()

    assert blender.rendered == ["img_a1"]
    assert report.cached == 2


def test_the_index_maps_reach_the_manifest(preview_graph, config):
    """Without them an object-ID mask is an anonymous blob."""
    build(preview_graph, config, FakeBlender()).render_scene()

    stats = manifest_mod.Manifest.load(config.manifest_path).stats
    assert stats["pass_index"]["objects"]["1"] == "sofa_1"


def test_the_index_maps_survive_a_fully_cached_run(preview_graph, config):
    """They still describe the previews on disk, so a no-op run must keep them."""
    build(preview_graph, config, FakeBlender()).render_scene()
    build(preview_graph, config, FakeBlender()).render_scene()

    stats = manifest_mod.Manifest.load(config.manifest_path).stats
    assert stats["pass_index"]["materials"]["1"] == "FloorMaterial"


def test_passes_can_be_switched_off(preview_graph, config):
    config.settings = RenderSettings(width=64, height=36, samples=1, passes=())
    blender = FakeBlender(passes=())
    build(preview_graph, config, blender).render_scene()

    record = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")
    assert record.passes == {}
    assert not os.path.exists(
        os.path.join(config.preview_dir, "room_a", "viewpoint_01_albedo.png")
    )


def test_changing_the_pass_list_invalidates_every_preview(preview_graph, config):
    """The pass list is part of what a preview *is*, so it is in the key."""
    build(preview_graph, config, FakeBlender()).render_scene()

    config.settings = RenderSettings(width=64, height=36, samples=1, passes=("albedo",))
    blender = FakeBlender(passes=("albedo",))
    report = build(preview_graph, config, blender).render_scene()
    assert report.rendered == 3


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_the_render_matches_the_viewpoint_aspect_by_default(preview_graph, config):
    """A 4:3 photograph's viewpoint rendered 16:9 would show scene the
    photograph never contained, and similarity would score that as a miss."""
    preview_graph.viewpoints[0].aspect = 4 / 3
    config.settings = RenderSettings(width=640, match_aspect=True)

    build(preview_graph, config, FakeBlender()).render_scene()
    record = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")
    assert (record.width, record.height) == (640, 480)


def test_a_fixed_resolution_can_be_forced(preview_graph, config):
    preview_graph.viewpoints[0].aspect = 4 / 3
    config.settings = RenderSettings(width=640, height=360, match_aspect=False)

    build(preview_graph, config, FakeBlender()).render_scene()
    record = manifest_mod.Manifest.load(config.manifest_path).record_for("img_a1")
    assert (record.width, record.height) == (640, 360)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_resolves_paths_against_the_project_root(tmp_path):
    built = PreviewConfig.from_config({"preview": {"width": 800}}, base_dir=str(tmp_path))

    assert built.settings.width == 800
    assert built.preview_dir == os.path.join(str(tmp_path), "output", "preview")
    assert built.manifest_path.endswith(os.path.join("preview", "manifest.json"))
    assert os.path.isabs(built.cache_path)


def test_unknown_config_keys_are_ignored(tmp_path):
    """config.json is hand-edited; a typo should not stop an evaluation pass."""
    built = PreviewConfig.from_config(
        {"preview": {"widht": 800, "samples": 4}}, base_dir=str(tmp_path)
    )
    assert built.settings.width == 640
    assert built.settings.samples == 4


def test_settings_survive_a_round_trip():
    settings = RenderSettings(samples=32, transparent=True, view_transform="Filmic")
    assert RenderSettings.from_dict(settings.to_dict()) == settings
