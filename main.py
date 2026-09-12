"""
ArchX3D — Pipeline Orchestrator
=================================
Runs the full 2D DXF → 3D model pipeline:

  Step 1: DXF Reconstruction (modules/recon) — deterministic, CPU-only
  Step 2: Scene Analysis    (scene_analyzer.py)   — with --images
          or AI Styling     (style_generator.py)  — legacy, colours only
  Step 3: Blender 3D Gen    (blender_generator.py)
  Step 4: Video Stitching   (video_stitcher.py)
  Step 5: Evaluation        (evaluation/engine.py)     — with --evaluate
  Step 6: Refinement        (optimizer/pipeline.py)    — with --refine

Usage:
  python main.py <input.dxf> --images ref1.jpg ref2.jpg
  python main.py <input.dxf> --images reference_images/
  python main.py <input.dxf> --skip-vision          # unfurnished shell

Step 1 needs no API key, no network and no model: it reads the CAD geometry,
resolves the drawing's units from its own evidence, reconstructs wall systems,
rooms and openings, and refuses to continue if the result does not validate.
Everything after it is optional enrichment. "No API key" never means
"no building".

Supplying reference photographs of the interior runs the vision pipeline,
which produces data/scene_graph.json — furniture, lighting, finishes and
spatial relationships — and the Blender step rebuilds the room from it.
Without images the pipeline still produces the architectural shell.
"""

import subprocess
import sys
import os
import json
import argparse
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.join(BASE_DIR, 'modules'))
from child_process import child_command  # noqa: E402
from app_paths import code_path, data_path, ensure_data_dirs  # noqa: E402

# Code and data are the same directory in a source checkout and different ones
# inside a frozen bundle; see modules/app_paths.py.
MODULES_DIR = code_path('modules')
CONFIG_PATH = code_path('config.json')
DATA_DIR = data_path('data')
OUTPUT_DIR = data_path('output')

# --- Configuration ---
BLENDER_EXECUTABLE_PATH = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
# ---------------------

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("ArchX3D")


def load_config():
    """Load pipeline config or use defaults."""
    defaults = {
        "layer_names": ["WALLS"],
        "scale_factor": 1.0,
        "arc_segments": 16,
        "auto_detect_layer": True,
        "deduplicate": True,
        "skip_styling": False,
        "wall_height": 3.0,
        "vision": {
            "enabled": True,
            "model": "gemini-flash-latest",
            "fallback_model": "gemini-flash-lite-latest",
            "cache": True,
            "cache_dir": ".cache/vision",
            "max_images": 6,
            "images_dir": "reference_images",
            "include_uncertain": False,
        },
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                user = json.load(f)
            return {**defaults, **user}
        except Exception as e:
            log.warning(f"Failed to load config: {e}. Using defaults.")
    return defaults


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def resolve_reference_images(cli_images, vision_cfg):
    """Expand --images entries (files or directories) into a list of images.

    Falls back to the configured ``images_dir`` when nothing was passed, so a
    project can simply keep its references in ``reference_images/``.
    """
    entries = cli_images
    if not entries:
        default_dir = os.path.join(BASE_DIR, vision_cfg.get("images_dir", "reference_images"))
        if os.path.isdir(default_dir):
            entries = [default_dir]
        else:
            return []

    resolved = []
    for entry in entries:
        path = entry if os.path.isabs(entry) else os.path.join(BASE_DIR, entry)
        if os.path.isfile(path):
            resolved.append(os.path.abspath(path))
        elif os.path.isdir(path):
            resolved.extend(
                os.path.abspath(os.path.join(path, name))
                for name in sorted(os.listdir(path))
                if name.lower().endswith(IMAGE_EXTENSIONS)
            )
        else:
            log.warning(f"Reference image path not found: {entry}")

    max_images = vision_cfg.get("max_images", 6)
    if len(resolved) > max_images:
        log.info(f"Using the first {max_images} of {len(resolved)} reference images")
        resolved = resolved[:max_images]

    return resolved


def _log_scene_graph_summary(path):
    """Print a short account of what the vision step reconstructed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            graph = json.load(f)
    except Exception as e:
        log.warning(f"Could not read scene graph summary: {e}")
        return

    objects = graph.get("objects", [])
    built = [o for o in objects if not o.get("uncertain")]
    confidence = graph.get("diagnostics", {}).get("confidence", {})

    log.info(f"  Scene: {graph.get('room', {}).get('room_type', '?')} "
             f"({graph.get('room', {}).get('style', '?')})")
    log.info(f"  Objects: {len(built)} buildable / {len(objects)} detected"
             + (f", mean confidence {confidence.get('mean')}" if confidence.get("mean") else ""))
    log.info(f"  Lights: {len(graph.get('lights', []))}, "
             f"openings: {len(graph.get('openings', []))}")

    validation = graph.get("diagnostics", {}).get("validation", {})
    if validation.get("total_issues"):
        log.info(f"  Validation: {validation.get('corrected', 0)} auto-corrected, "
                 f"{validation.get('uncorrected', 0)} unresolved")


def run_step(command, description, critical=True):
    """Execute a pipeline step as a subprocess."""
    log.info(f"{'='*50}")
    log.info(f"STEP: {description}")
    log.info(f"CMD:  {' '.join(command) if isinstance(command, list) else command}")
    log.info(f"{'='*50}")

    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            # Decode child output as UTF-8 regardless of the console codepage;
            # without this a non-ASCII log line from a step raises on Windows.
            encoding="utf-8",
            errors="replace",
            timeout=900  # Blender renders and vision calls can both be slow
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                log.info(f"  {line}")
        log.info(f"[OK] {description} - SUCCESS")
        return True

    except subprocess.CalledProcessError as e:
        log.error(f"[FAIL] {description} - FAILED (exit code {e.returncode})")
        if e.stderr:
            for line in e.stderr.strip().split('\n')[-10:]:  # Last 10 lines
                log.error(f"  STDERR: {line}")
        if e.stdout:
            for line in e.stdout.strip().split('\n')[-5:]:
                log.error(f"  STDOUT: {line}")
        if critical:
            sys.exit(1)
        return False

    except subprocess.TimeoutExpired:
        log.error(f"[TIMEOUT] {description} - TIMED OUT (>600s)")
        if critical:
            sys.exit(1)
        return False

    except OSError as e:
        log.error(f"[OS_ERROR] {description} - OS ERROR: {e}")
        if critical:
            sys.exit(1)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="ArchX3D: Convert 2D DXF floor plans to 3D Blender models"
    )
    parser.add_argument(
        "input_dxf",
        nargs='?',
        default=os.path.join(BASE_DIR, "test_floorplan.dxf"),
        help="Path to input DXF file (default: test_floorplan.dxf)"
    )
    parser.add_argument(
        "--images",
        nargs="+",
        default=None,
        help="Reference interior photographs (files or a directory) to rebuild from"
    )
    parser.add_argument(
        "--skip-vision",
        action="store_true",
        help="Skip scene analysis; generate the unfurnished architectural shell"
    )
    parser.add_argument(
        "--use-scene-graph",
        action="store_true",
        help="Build from the existing data/scene_graph.json without re-analysing "
             "(used after the wizard's review step)"
    )
    parser.add_argument(
        "--vision-model",
        default=None,
        help="Override the vision model (default: config.json vision.model)"
    )
    parser.add_argument(
        "--no-vision-cache",
        action="store_true",
        help="Bypass the cached vision responses and re-query the model"
    )
    parser.add_argument(
        "--include-uncertain",
        action="store_true",
        help="Build low-confidence detections instead of withholding them"
    )
    parser.add_argument(
        "--skip-styling",
        action="store_true",
        help="Skip the legacy Gemini text styling step"
    )
    parser.add_argument(
        "--skip-render",
        action="store_true",
        help="Skip frame rendering and video stitching (export GLB/blend only)"
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Score the reconstruction against the reference photographs and "
             "write output/evaluation/ (needs numpy and Pillow)"
    )
    parser.add_argument(
        "--refine",
        action="store_true",
        help="Plan improvements from the evaluation and run the optimisation "
             "loop, keeping only changes that measurably help. Implies "
             "--evaluate; each iteration is a Blender rebuild, so budget "
             "minutes"
    )
    parser.add_argument(
        "--refine-iterations",
        type=int,
        default=8,
        help="Optimisation budget in iterations (default: 8)"
    )
    parser.add_argument(
        "--layers",
        type=str,
        default=None,
        help="Comma-separated layer names to extract (overrides config.json)"
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=None,
        help="Metres per DXF unit. Overrides the reconstruction's own unit "
             "resolution — use only when the drawing's evidence is wrong"
    )
    parser.add_argument(
        "--diagnostics",
        default=None,
        help="Directory for the reconstruction diagnostics bundle "
             "(default: output/diagnostics). Pass '' to disable"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Deterministic CPU-only build: no vision, no styling, no network. "
             "Equivalent to --skip-vision --skip-styling, and stated as such "
             "in the log so a build is never silently AI-assisted"
    )

    args = parser.parse_args()
    config = load_config()

    if args.offline:
        # Explicit offline mode. The geometry engine never needed a key; this
        # switch is about being able to say so, and about not having a build
        # quietly reach for the network because a key happened to be present.
        args.skip_vision = True
        args.skip_styling = True

    input_dxf = os.path.abspath(args.input_dxf)
    if not os.path.exists(input_dxf):
        log.error(f"Input DXF file not found: {input_dxf}")
        sys.exit(1)

    # Ensure the writable directories exist (see modules/app_paths.py)
    ensure_data_dirs()

    geometry_json = os.path.join(DATA_DIR, "geometry.json")
    building_json = os.path.join(DATA_DIR, "building.json")
    # None means "the default place"; an empty string means "nowhere".
    if args.diagnostics is None:
        diagnostics_dir = os.path.join(OUTPUT_DIR, "diagnostics")
    else:
        diagnostics_dir = args.diagnostics or None
    styling_json = os.path.join(DATA_DIR, "styling.json")
    scene_graph_json = os.path.join(DATA_DIR, "scene_graph.json")

    vision_cfg = config.get("vision", {})
    reference_images = resolve_reference_images(args.images, vision_cfg)
    run_vision = bool(reference_images) and not args.skip_vision and vision_cfg.get("enabled", True)
    # Decided up front (not inside Step 2) so the stale-scene-graph cleanup
    # below sees the same "will vision actually run" answer that Step 2 acts
    # on. Otherwise a missing key left run_vision True, the cleanup branch
    # never fired, and a leftover scene_graph.json from an earlier run with a
    # key silently furnished this "unfurnished" build.
    vision_key_missing = run_vision and not os.environ.get("GEMINI_API_KEY")
    if vision_key_missing:
        run_vision = False

    # --layers is no longer how walls are found: the reconstruction classifies
    # every layer itself, by AIA/ISO convention and by vernacular, and an
    # explicit list is now a restriction rather than a requirement. It is kept
    # because scripts pass it, and reported so nobody assumes it still steers
    # the extraction.
    if args.layers:
        log.warning("--layers is ignored by the reconstruction engine, which "
                    "classifies layers from their names and the geometry. "
                    "Restricting to %r is no longer necessary.", args.layers)

    log.info("=" * 60)
    log.info("  ArchX3D Pipeline")
    log.info("=" * 60)
    log.info(f"  Input:    {input_dxf}")
    log.info(f"  Engine:   deterministic CPU reconstruction (no API key needed)")
    if args.offline:
        log.info(f"  Mode:     OFFLINE — CPU only, no network, no AI")
    if args.scale:
        log.info(f"  Scale:    {args.scale} m/unit (overridden)")
    else:
        log.info(f"  Scale:    resolved from the drawing's own evidence")
    if run_vision:
        log.info(f"  Vision:   {len(reference_images)} reference image(s), "
                 f"model {args.vision_model or vision_cfg.get('model')}")
    elif vision_key_missing:
        log.info("  Vision:   SKIP (GEMINI_API_KEY not set)")
    elif not reference_images:
        log.info("  Vision:   SKIP (no reference images)")
    elif args.skip_vision:
        log.info("  Vision:   SKIP (--skip-vision)")
    else:
        log.info("  Vision:   SKIP (disabled in config.json)")
    log.info(f"  Render:   {'SKIP' if args.skip_render else 'Enabled'}")
    log.info(f"  Evaluate: {'Enabled' if (args.evaluate or args.refine) else 'SKIP'}")
    log.info(f"  Refine:   {f'{args.refine_iterations} iterations' if args.refine else 'SKIP'}")
    log.info("=" * 60)

    # =========================================================================
    # STEP 1: DXF Reconstruction (deterministic, CPU-only)
    # =========================================================================
    #
    # Runs in-process rather than as a child: it is pure Python, it is the
    # step whose failure must stop the build, and its diagnostics are worth
    # more than a subprocess exit code.
    log.info("")
    log.info("-" * 60)
    log.info("Step 1: DXF Reconstruction (CPU / offline)")
    log.info("-" * 60)
    from recon import compat as recon_compat          # noqa: E402
    from recon.ir import ReconstructionError          # noqa: E402
    from recon.pipeline import reconstruct            # noqa: E402

    try:
        building = reconstruct(
            input_dxf,
            wall_height=config.get("wall_height", 2.7),
            user_scale=args.scale,
            diagnostics_dir=diagnostics_dir,
        )
    except ReconstructionError as exc:
        log.error("Reconstruction failed at stage '%s': %s", exc.stage, exc)
        for failure in exc.failures:
            log.error("  - %s", failure)
        log.error("No 3D model was generated. Generating one from a "
                  "reconstruction this broken would produce the distorted "
                  "geometry this check exists to prevent.")
        if diagnostics_dir:
            log.error("  Diagnostics written to %s", diagnostics_dir)
        sys.exit(1)

    building.to_json(building_json)
    recon_compat.write_geometry_json(building, geometry_json)

    units = building.units
    log.info("  Units:     %s (%s, confidence %.2f)",
             units.unit_name, units.method, units.confidence)
    if units.conflict:
        log.warning("  %s", units.conflict)
    log.info("  Size:      %.2f x %.2f m", building.width, building.depth)
    log.info("  Walls:     %d (%.1f m total)", len(building.walls),
             building.total_wall_length)
    log.info("  Rooms:     %d (%.1f m2 floor, %.1f m2 footprint)",
             len(building.rooms), building.floor_area, building.footprint_area)
    summary = building.summary()
    log.info("  Openings:  %d doors, %d windows",
             summary.get("doors", 0), summary.get("windows", 0))
    for warning in building.validation.get("warnings", [])[:6]:
        log.warning("  ! %s", warning)
    named = [r.label for r in building.rooms if r.label]
    if named:
        log.info("  Named:     %s", ", ".join(named[:12]) +
                 (" ..." if len(named) > 12 else ""))
    log.info("  Wrote      %s", os.path.basename(building_json))

    # =========================================================================
    # STEP 2: Scene Analysis (vision) — or legacy text styling
    # =========================================================================
    #
    # A stale scene_graph.json from an earlier run would silently furnish this
    # model with the wrong room, so it is cleared unless this run rebuilds it.
    # --use-scene-graph is the deliberate exception: the wizard has already
    # produced and had the user review a graph, and re-running vision would
    # discard their edits.
    if args.use_scene_graph:
        if not os.path.exists(scene_graph_json):
            log.error(f"--use-scene-graph given but {scene_graph_json} does not exist")
            sys.exit(1)
        log.info("Step 2: Using the existing reviewed scene graph")
        _log_scene_graph_summary(scene_graph_json)
        run_vision = False
    elif not run_vision and os.path.exists(scene_graph_json):
        os.remove(scene_graph_json)
        log.info("Removed a stale scene_graph.json from a previous run")

    if args.use_scene_graph:
        pass  # Step 2 already satisfied above.
    elif run_vision:
        cmd_vision = child_command(
            os.path.join(MODULES_DIR, "scene_analyzer.py"),
            [
                geometry_json,
                scene_graph_json,
                "--images", *reference_images,
                "--wall-height", str(config.get("wall_height", 3.0)),
                "--model", args.vision_model or vision_cfg.get("model", "gemini-flash-latest"),
                "--fallback-model", vision_cfg.get("fallback_model", "gemini-flash-lite-latest"),
                "--cache-dir", vision_cfg.get("cache_dir", ".cache/vision"),
                "--max-images", str(vision_cfg.get("max_images", 6)),
            ],
        )
        if args.no_vision_cache or not vision_cfg.get("cache", True):
            cmd_vision.append("--no-cache")
        if args.include_uncertain or vision_cfg.get("include_uncertain", False):
            cmd_vision.append("--include-uncertain")

        # Non-critical: a vision failure still yields a correct shell.
        run_step(cmd_vision, "Step 2: Scene Analysis (vision)", critical=False)

        if os.path.exists(scene_graph_json):
            _log_scene_graph_summary(scene_graph_json)
    elif vision_key_missing:
        log.warning("GEMINI_API_KEY not set — skipping scene analysis.")
        log.warning("The model will be generated unfurnished.")
    else:
        skip_styling = args.skip_styling or config.get("skip_styling", False)
        if skip_styling or not os.environ.get("GEMINI_API_KEY"):
            log.info("Step 2: SKIPPED — no reference images and styling disabled")
        else:
            log.info("Step 2: No reference images; falling back to legacy text styling")
            cmd_style = child_command(
                os.path.join(MODULES_DIR, "style_generator.py"),
                [geometry_json, styling_json],
            )
            run_step(cmd_style, "Step 2: AI Style Generation (legacy)", critical=False)

    # =========================================================================
    # STEP 3: Blender 3D Generation & Export
    # =========================================================================
    if not os.path.exists(BLENDER_EXECUTABLE_PATH):
        log.error(f"Blender not found: {BLENDER_EXECUTABLE_PATH}")
        log.error("Please install Blender or update BLENDER_EXECUTABLE_PATH in main.py")
        sys.exit(1)

    cmd_blender = [
        BLENDER_EXECUTABLE_PATH,
        "--background",
        "--python",
        os.path.join(MODULES_DIR, "blender_generator.py")
    ]
    # --skip-render previously only skipped video stitching, so Blender still
    # rendered every frame. Pass the intent through so it is honoured.
    if args.skip_render:
        os.environ["ARCHX3D_SKIP_RENDER"] = "1"
    run_step(cmd_blender, "Step 3: Blender 3D Generation & Export")

    # =========================================================================
    # STEP 4: Video Stitching (optional)
    # =========================================================================
    if not args.skip_render:
        frames_dir = os.path.join(OUTPUT_DIR, 'frames')
        if os.path.exists(frames_dir) and os.listdir(frames_dir):
            cmd_stitcher = child_command(
                os.path.join(MODULES_DIR, "video_stitcher.py")
            )
            run_step(cmd_stitcher, "Step 4: Video Stitching", critical=False)
        else:
            log.warning("No rendered frames found. Skipping video stitching.")
    else:
        log.info("Step 4: Video Stitching — SKIPPED (--skip-render)")

    # =========================================================================
    # STEP 5: Reconstruction Evaluation (optional)
    # =========================================================================
    #
    # Reads what Step 3's preview pass already wrote and measures it against
    # the reference photographs. Non-critical and never destructive: it only
    # measures, and a build is still a build if nobody scored it.
    if args.evaluate or args.refine:
        cmd_evaluate = child_command(
            os.path.join(MODULES_DIR, "evaluation", "engine.py")
        )
        if reference_images:
            cmd_evaluate += ["--images", os.path.dirname(reference_images[0])]
        run_step(cmd_evaluate, "Step 5: Reconstruction Evaluation", critical=False)

    # =========================================================================
    # STEP 6: Planning & Optimisation (optional)
    # =========================================================================
    #
    # Plans changes from the evaluation's findings and executes them, keeping
    # only what measurably improves the score. Non-critical: a build that was
    # not improved is still the build.
    if args.refine:
        cmd_refine = child_command(
            os.path.join(MODULES_DIR, "optimizer", "pipeline.py"),
            ["--max-iterations", str(args.refine_iterations)],
        )
        run_step(cmd_refine, "Step 6: Planning & Optimisation", critical=False)

    # =========================================================================
    # Summary
    # =========================================================================
    log.info("")
    log.info("=" * 60)
    log.info("  ArchX3D Pipeline - COMPLETE")
    log.info("=" * 60)

    outputs = {
        "geometry.json": os.path.join(DATA_DIR, "geometry.json"),
        "scene_graph.json": scene_graph_json,
        "model.glb": os.path.join(OUTPUT_DIR, "model.glb"),
        "scene.blend": os.path.join(OUTPUT_DIR, "scene.blend"),
        "walkthrough.mp4": os.path.join(OUTPUT_DIR, "walkthrough.mp4"),
        "preview/manifest.json": os.path.join(OUTPUT_DIR, "preview", "manifest.json"),
    }
    if args.evaluate or args.refine:
        outputs["evaluation/report.html"] = os.path.join(
            OUTPUT_DIR, "evaluation", "report.html")
    if args.refine:
        for name in ("planner_report.json", "optimization_history.json",
                     "metrics.json"):
            outputs[f"refinement/{name}"] = os.path.join(
                OUTPUT_DIR, "refinement", name)
    for name, path in outputs.items():
        if os.path.exists(path):
            size = os.path.getsize(path)
            log.info(f"  [OK] {name:20s} ({size:>10,} bytes)")
        else:
            log.info(f"  [--] {name:20s} (not generated)")


if __name__ == "__main__":
    main()
