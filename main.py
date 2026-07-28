"""
ArchX3D — Pipeline Orchestrator
=================================
Runs the full 2D DXF → 3D model pipeline:

  Step 1: DXF Extraction   (dxf_extractor.py)
  Step 2: Scene Analysis    (scene_analyzer.py)   — with --images
          or AI Styling     (style_generator.py)  — legacy, colours only
  Step 3: Blender 3D Gen    (blender_generator.py)
  Step 4: Video Stitching   (video_stitcher.py)
  Step 5: Evaluation        (evaluation/engine.py)     — with --evaluate

Usage:
  python main.py <input.dxf> --images ref1.jpg ref2.jpg
  python main.py <input.dxf> --images reference_images/
  python main.py <input.dxf> --skip-vision          # unfurnished shell

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

# --- Configuration ---
BLENDER_EXECUTABLE_PATH = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
# ---------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MODULES_DIR = os.path.join(BASE_DIR, 'modules')
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

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
            "model": "gemini-2.5-pro",
            "fallback_model": "gemini-2.5-flash",
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
        "--layers",
        type=str,
        default=None,
        help="Comma-separated layer names to extract (overrides config.json)"
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=None,
        help="Scale factor for DXF units to meters (overrides config.json)"
    )

    args = parser.parse_args()
    config = load_config()

    input_dxf = os.path.abspath(args.input_dxf)
    if not os.path.exists(input_dxf):
        log.error(f"Input DXF file not found: {input_dxf}")
        sys.exit(1)

    # Ensure data and output dirs exist
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    geometry_json = os.path.join(DATA_DIR, "geometry.json")
    styling_json = os.path.join(DATA_DIR, "styling.json")
    scene_graph_json = os.path.join(DATA_DIR, "scene_graph.json")

    vision_cfg = config.get("vision", {})
    reference_images = resolve_reference_images(args.images, vision_cfg)
    run_vision = bool(reference_images) and not args.skip_vision and vision_cfg.get("enabled", True)

    # Resolve layer names and scale
    layers = args.layers if args.layers else ','.join(config.get("layer_names", ["WALLS"]))
    scale = str(args.scale if args.scale else config.get("scale_factor", 1.0))
    arc_segs = str(config.get("arc_segments", 16))

    log.info("=" * 60)
    log.info("  ArchX3D Pipeline")
    log.info("=" * 60)
    log.info(f"  Input:    {input_dxf}")
    log.info(f"  Layers:   {layers}")
    log.info(f"  Scale:    {scale}")
    if run_vision:
        log.info(f"  Vision:   {len(reference_images)} reference image(s), "
                 f"model {args.vision_model or vision_cfg.get('model')}")
    else:
        log.info("  Vision:   SKIP (no reference images)"
                 if not reference_images else "  Vision:   SKIP (--skip-vision)")
    log.info(f"  Render:   {'SKIP' if args.skip_render else 'Enabled'}")
    log.info(f"  Evaluate: {'Enabled' if args.evaluate else 'SKIP'}")
    log.info("=" * 60)

    # =========================================================================
    # STEP 1: DXF Extraction
    # =========================================================================
    cmd_extract = [
        sys.executable,
        os.path.join(MODULES_DIR, "dxf_extractor.py"),
        input_dxf,
        geometry_json,
        layers,
        scale,
        arc_segs
    ]
    run_step(cmd_extract, "Step 1: DXF Geometry Extraction")

    # Verify extraction produced output
    if not os.path.exists(geometry_json):
        log.error("geometry.json was not created!")
        sys.exit(1)

    with open(geometry_json, 'r') as f:
        geo = json.load(f)
    seg_count = geo.get("metadata", {}).get("segment_count", 0)
    log.info(f"  Extracted {seg_count} wall segments")

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
        if not os.environ.get("GEMINI_API_KEY"):
            log.warning("GEMINI_API_KEY not set — skipping scene analysis.")
            log.warning("The model will be generated unfurnished.")
        else:
            cmd_vision = [
                sys.executable,
                os.path.join(MODULES_DIR, "scene_analyzer.py"),
                geometry_json,
                scene_graph_json,
                "--images", *reference_images,
                "--wall-height", str(config.get("wall_height", 3.0)),
                "--model", args.vision_model or vision_cfg.get("model", "gemini-2.5-pro"),
                "--fallback-model", vision_cfg.get("fallback_model", "gemini-2.5-flash"),
                "--cache-dir", vision_cfg.get("cache_dir", ".cache/vision"),
                "--max-images", str(vision_cfg.get("max_images", 6)),
            ]
            if args.no_vision_cache or not vision_cfg.get("cache", True):
                cmd_vision.append("--no-cache")
            if args.include_uncertain or vision_cfg.get("include_uncertain", False):
                cmd_vision.append("--include-uncertain")

            # Non-critical: a vision failure still yields a correct shell.
            run_step(cmd_vision, "Step 2: Scene Analysis (vision)", critical=False)

            if os.path.exists(scene_graph_json):
                _log_scene_graph_summary(scene_graph_json)
    else:
        skip_styling = args.skip_styling or config.get("skip_styling", False)
        if skip_styling or not os.environ.get("GEMINI_API_KEY"):
            log.info("Step 2: SKIPPED — no reference images and styling disabled")
        else:
            log.info("Step 2: No reference images; falling back to legacy text styling")
            cmd_style = [
                sys.executable,
                os.path.join(MODULES_DIR, "style_generator.py"),
                geometry_json,
                styling_json
            ]
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
            cmd_stitcher = [
                sys.executable,
                os.path.join(MODULES_DIR, "video_stitcher.py")
            ]
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
    if args.evaluate:
        cmd_evaluate = [
            sys.executable,
            os.path.join(MODULES_DIR, "evaluation", "engine.py"),
        ]
        if reference_images:
            cmd_evaluate += ["--images", os.path.dirname(reference_images[0])]
        run_step(cmd_evaluate, "Step 5: Reconstruction Evaluation", critical=False)

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
    if args.evaluate:
        outputs["evaluation/report.html"] = os.path.join(
            OUTPUT_DIR, "evaluation", "report.html")
    for name, path in outputs.items():
        if os.path.exists(path):
            size = os.path.getsize(path)
            log.info(f"  [OK] {name:20s} ({size:>10,} bytes)")
        else:
            log.info(f"  [--] {name:20s} (not generated)")


if __name__ == "__main__":
    main()
