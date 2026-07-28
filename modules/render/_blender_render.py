"""
ArchX3D — In-Blender preview renderer
=====================================
The only module in ``render`` that imports ``bpy``. Everything else — hashing,
caching, scheduling, the manifest — runs outside Blender and is testable
without it.

Two entry points, one code path
-------------------------------
``main()``
    Run as ``blender --background scene.blend --python _blender_render.py --
    --job job.json``. Reads a batch of tasks, renders them into the *already
    loaded* file, writes a result file. No geometry is generated here: the
    ``.blend`` is whatever ``blender_generator`` last produced.

``render_tasks()``
    Called in-process by ``renderer.InlineRenderer`` when
    ``blender_generator`` has just built the scene. Starting a second Blender
    to reload a file still warm in memory would double the cost of a preview
    pass for nothing.

Cameras are never estimated
---------------------------
A preview is only comparable with the photograph it is scored against if it
reproduces that photograph's fitted pose exactly. Two sources are acceptable,
in order:

1. the ``Ref_<image_id>`` camera the generator already built into the .blend;
2. a camera rebuilt from the stored ``ViewPoint`` by ``blender.camera``, the
   same function the generator used.

If neither is available the task fails and says so. Guessing a camera would
produce an image that looks fine and scores meaningless.
"""

from __future__ import annotations

import json
import os
import sys
import time

import bpy

# Blender's bundled Python does not see the project; add ``modules/`` so the
# schema and the camera builder can be imported. Same trick as
# ``blender_generator``, which is the only other file that needs it.
_MODULES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MODULES_DIR not in sys.path:
    sys.path.insert(0, _MODULES_DIR)


def _log(message: str) -> None:
    """Prefixed so the host process can pick our lines out of Blender's noise."""
    print(f"[PREVIEW] {message}")


# ---------------------------------------------------------------------------
# Deterministic render settings
# ---------------------------------------------------------------------------


def _set(owner, attribute: str, value) -> bool:
    """Assign an optional Blender property, reporting whether it existed.

    EEVEE's property set changes between releases — ``use_gtao`` vanished when
    EEVEE Next folded ambient occlusion into ray tracing, ``use_raytracing``
    arrived with it. Pinning a setting that does not exist on this build is
    not an error; silently pinning nothing would be.
    """
    if not hasattr(owner, attribute):
        return False
    try:
        setattr(owner, attribute, value)
        return True
    except (AttributeError, TypeError, ValueError) as exc:
        _log(f"could not set {attribute}={value!r}: {exc}")
        return False


def apply_settings(scene, settings: dict) -> None:
    """Pin every stochastic and tone-mapping knob. See ``renderer``'s table."""
    render = scene.render
    engine = str(settings.get("engine", "EEVEE")).upper()

    # -- engine ------------------------------------------------------------
    if engine == "CYCLES":
        _apply_cycles(scene, settings)
    else:
        _apply_eevee(scene, settings)

    # -- framing -----------------------------------------------------------
    render.resolution_percentage = 100
    render.use_border = False
    render.use_crop_to_border = False
    render.filter_size = float(settings.get("filter_size", 1.5))
    render.use_motion_blur = False
    render.film_transparent = bool(settings.get("transparent", False))
    render.use_persistent_data = bool(settings.get("persistent_data", True))

    # A stray compositor or sequencer strip in the generated file would post-
    # process the evaluation image without anything in the manifest saying so.
    render.use_compositing = False
    render.use_sequencer = False

    # -- output ------------------------------------------------------------
    image = render.image_settings
    image.file_format = str(settings.get("file_format", "PNG"))
    image.color_mode = "RGBA" if settings.get("transparent") else "RGB"
    _set(image, "color_depth", str(settings.get("color_depth", "8")))
    _set(image, "compression", int(settings.get("compression", 15)))
    # Blender dithers 8-bit output by default, which puts +/-1 LSB noise on
    # exactly the flat surfaces the colour axis measures.
    render.dither_intensity = float(settings.get("dither", 0.0))
    render.use_file_extension = True
    _disable_stamps(render)

    # -- colour management -------------------------------------------------
    view = scene.view_settings
    for transform in (str(settings.get("view_transform", "Standard")), "Standard", "Raw"):
        if _set(view, "view_transform", transform):
            break
    _set(view, "look", str(settings.get("look", "None")))
    view.exposure = float(settings.get("exposure", 0.0))
    view.gamma = float(settings.get("gamma", 1.0))
    _set(view, "use_curve_mapping", False)
    _set(view, "use_white_balance", False)          # Blender 4.5+
    _set(scene.display_settings, "display_device", "sRGB")

    # -- time --------------------------------------------------------------
    # The walkthrough orbit is keyframed, so "which frame" changes the scene.
    # Always stand on the same one.
    scene.frame_set(int(settings.get("frame", 1)))


#: Blender writes these into PNG text chunks even when stamp burn-in is off.
#: ``date`` and ``render_time`` are wall-clock, so leaving them on makes two
#: renders of an identical scene differ in bytes while agreeing in every pixel
#: — which is enough to defeat a byte-level regression check. The rest are
#: switched off with them because the manifest already records all of it, and
#: more reliably.
_STAMP_FLAGS = (
    "use_stamp_date", "use_stamp_render_time", "use_stamp_time",
    "use_stamp_frame", "use_stamp_frame_range", "use_stamp_camera",
    "use_stamp_lens", "use_stamp_scene", "use_stamp_filename",
    "use_stamp_marker", "use_stamp_sequencer_strip", "use_stamp_note",
    "use_stamp_hostname", "use_stamp_memory",
)


def _disable_stamps(render) -> None:
    """Strip wall-clock metadata so identical scenes produce identical files."""
    _set(render, "use_stamp", False)          # burn-in overlay
    for flag in _STAMP_FLAGS:
        _set(render, flag, False)


def _apply_eevee(scene, settings: dict) -> None:
    """EEVEE, across the Next rename and the 4.x/5.x property churn."""
    for identifier in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = identifier
            break
        except (TypeError, ValueError):
            continue

    eevee = getattr(scene, "eevee", None)
    if eevee is None:
        return

    samples = int(settings.get("samples", 16))
    _set(eevee, "taa_render_samples", samples)
    # Reprojection reuses the previous frame's samples, making a render depend
    # on what was rendered before it — the definition of non-deterministic.
    _set(eevee, "use_taa_reprojection", False)
    _set(eevee, "use_shadow_jitter", False)
    _set(eevee, "use_shadow_jitter_viewport", False)
    _set(eevee, "use_bokeh_jittered", False)
    # Screen-space ray tracing is temporally accumulated and denoised; both
    # carry history. Off by default, and it is most of the frame time.
    _set(eevee, "use_raytracing", bool(settings.get("raytracing", False)))
    # EEVEE legacy only — Next folds ambient occlusion into ray tracing.
    if _set(eevee, "use_gtao", bool(settings.get("ambient_occlusion", True))):
        _set(eevee, "gtao_distance", 1.0)


def _apply_cycles(scene, settings: dict) -> None:
    """Cycles, for the occasional reference render. Seeded and un-denoised."""
    try:
        scene.render.engine = "CYCLES"
    except (TypeError, ValueError):
        _log("Cycles unavailable in this Blender session; falling back to EEVEE")
        _apply_eevee(scene, settings)
        return

    cycles = getattr(scene, "cycles", None)
    if cycles is None:
        return
    _set(cycles, "samples", int(settings.get("samples", 16)))
    _set(cycles, "use_adaptive_sampling", False)   # sample count would vary
    _set(cycles, "seed", 0)
    _set(cycles, "use_animated_seed", False)
    _set(cycles, "use_denoising", False)           # denoisers are not reproducible
    _set(cycles, "time_limit", 0.0)                # a time limit is wall-clock dependent


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


class CameraResolver:
    """Finds or rebuilds the exact camera for a viewpoint, once per session.

    Cameras built here are kept in a dict rather than rebuilt per task: a batch
    normally renders every viewpoint of a room, and re-adding a camera object
    for each would leave a trail of ``Ref_x.001`` duplicates in the file.
    """

    def __init__(self) -> None:
        self._built = {}
        self._builder = self._load_builder()

    @staticmethod
    def _load_builder():
        try:
            from blender import camera as bl_camera
            from vision.schema import ViewPoint
        except ImportError as exc:
            _log(f"camera rebuild unavailable ({exc}); only .blend cameras can be used")
            return None
        return (bl_camera, ViewPoint)

    def resolve(self, task: dict):
        """Return ``(camera_object, source)`` or ``(None, reason)``."""
        image_id = task.get("viewpoint_id", "")

        existing = bpy.data.objects.get(f"Ref_{image_id}")
        if existing is not None and existing.type == "CAMERA":
            return existing, "blend"

        if image_id in self._built:
            return self._built[image_id], "graph"

        payload = task.get("viewpoint") or {}
        if not payload:
            return None, "no camera in the .blend and no stored ViewPoint to rebuild from"
        if self._builder is None:
            return None, "no camera in the .blend and the camera builder is unavailable"

        bl_camera, ViewPoint = self._builder
        viewpoint = ViewPoint.from_dict(payload)
        camera = bl_camera.build_camera_from_viewpoint(viewpoint)
        if camera is None:
            return None, "camera could not be rebuilt from the stored ViewPoint"
        self._built[image_id] = camera
        return camera, "graph"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_tasks(tasks, settings: dict):
    """Render a batch into the current scene.

    Returns ``(results, index_maps)`` — one result dict per task, plus the
    object and material index maps the ID passes need to be readable. The maps
    describe the build rather than any one viewpoint, so they are accumulated
    across the batch and reported once.

    Never raises for a single bad task: a failed camera is recorded and the
    rest of the batch still renders. A batch that dies wholesale costs the
    caller every preview in it.
    """
    scene = bpy.context.scene
    apply_settings(scene, settings)

    resolver = CameraResolver()
    previous_camera = scene.camera
    results = []
    index_maps = {}

    for task in tasks:
        viewpoint_id = task.get("viewpoint_id", "")
        started = time.perf_counter()
        try:
            camera, source = resolver.resolve(task)
            if camera is None:
                results.append({"viewpoint_id": viewpoint_id, "ok": False, "error": source})
                _log(f"{viewpoint_id}: SKIP — {source}")
                continue

            width = int(task.get("width", 640))
            height = int(task.get("height", 360))
            output = task.get("output", "")

            scene.camera = camera
            scene.render.resolution_x = width
            scene.render.resolution_y = height
            scene.render.filepath = _filepath_for(output)

            os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
            bpy.ops.render.render(write_still=True)

            elapsed = int((time.perf_counter() - started) * 1000)
            if not os.path.exists(output):
                results.append({
                    "viewpoint_id": viewpoint_id, "ok": False, "render_ms": elapsed,
                    "error": f"render reported success but {os.path.basename(output)} "
                             f"was not written",
                })
                continue

            # Auxiliary passes for the evaluation engine. Rendered after the
            # beauty image and from the same camera, so nothing about the view
            # can drift between them.
            pass_paths, pass_indices = _render_passes(output, settings)
            if pass_indices:
                index_maps.update(pass_indices)

            results.append({
                "viewpoint_id": viewpoint_id,
                "ok": True,
                "render_ms": elapsed,
                "camera_source": source,
                "width": width,
                "height": height,
                "passes": pass_paths,
            })
            _log(f"{viewpoint_id}: {width}x{height} in {elapsed} ms "
                 f"(camera from {source})")

        except Exception as exc:  # noqa: BLE001 - one task must not kill the batch
            results.append({
                "viewpoint_id": viewpoint_id,
                "ok": False,
                "render_ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            })
            _log(f"{viewpoint_id}: FAILED — {exc}")

    scene.camera = previous_camera
    return results, index_maps


def _render_passes(output: str, settings: dict):
    """Render the auxiliary passes for one preview, if any were requested.

    Imported lazily so that a build with passes switched off never touches the
    pass machinery, and so a fault in it degrades to "no passes" rather than
    losing the beauty render that has already succeeded.
    """
    wanted = settings.get("passes")
    if not wanted:
        return {}, {}

    # This file runs two ways: imported as ``render._blender_render`` by the
    # in-process path, and handed to ``blender --python`` as a loose script,
    # where there is no package to be relative to. ``modules/`` is on the path
    # in both cases, so the absolute import is the one that always works.
    try:
        from render import _blender_passes
    except ImportError as exc:
        _log(f"pass rendering unavailable ({exc}); beauty render only")
        return {}, {}

    try:
        written, indices = _blender_passes.render_passes(output, wanted, settings)
    except Exception as exc:  # noqa: BLE001 - passes are diagnostics
        _log(f"pass rendering failed: {type(exc).__name__}: {exc}")
        return {}, {}
    return written, indices


def _filepath_for(output: str) -> str:
    """Blender appends the format's extension when ``use_file_extension`` is on.

    Handing it ``foo.png`` would produce ``foo.png.png``, so the extension is
    stripped here and Blender puts it back.
    """
    stem, extension = os.path.splitext(output)
    return stem if extension.lower() == ".png" else output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _job_path(argv) -> str:
    """Read ``-- --job <path>`` off Blender's command line."""
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if "--job" in argv:
        index = argv.index("--job")
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main() -> int:
    path = _job_path(list(sys.argv))
    if not path or not os.path.exists(path):
        _log("no job file given; nothing to render")
        return 1

    with open(path, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    tasks = job.get("tasks") or []
    settings = job.get("settings") or {}
    _log(f"rendering {len(tasks)} viewpoint(s) from {bpy.data.filepath or 'the open scene'}")

    started = time.perf_counter()
    results, index_maps = render_tasks(tasks, settings)
    total = int((time.perf_counter() - started) * 1000)

    result_path = job.get("result_path")
    if result_path:
        # Written last and in one call: the host treats a missing result file
        # as a total batch failure, so a partial write must not be possible.
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"total_ms": total, "results": results,
                       "index_maps": index_maps}, handle, indent=2)

    ok = sum(1 for r in results if r.get("ok"))
    _log(f"batch complete: {ok}/{len(results)} rendered in {total} ms")
    return 0


if __name__ == "__main__":
    main()
