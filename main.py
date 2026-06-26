"""
ArchX3D — Pipeline Orchestrator
=================================
Runs the full 2D DXF → 3D model pipeline:

  Step 1: DXF Extraction  (dxf_extractor.py)
  Step 2: AI Styling       (style_generator.py)  [optional]
  Step 3: Blender 3D Gen   (blender_generator.py)
  Step 4: Video Stitching  (video_stitcher.py)

Usage: python main.py <input.dxf> [--skip-styling] [--skip-render]
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
        "skip_styling": False
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                user = json.load(f)
            return {**defaults, **user}
        except Exception as e:
            log.warning(f"Failed to load config: {e}. Using defaults.")
    return defaults


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
            timeout=600  # 10 minute timeout for Blender renders
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
        "--skip-styling",
        action="store_true",
        help="Skip the Gemini AI styling step"
    )
    parser.add_argument(
        "--skip-render",
        action="store_true",
        help="Skip frame rendering and video stitching (export GLB/blend only)"
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
    log.info(f"  Styling:  {'SKIP' if args.skip_styling or config.get('skip_styling') else 'Gemini AI'}")
    log.info(f"  Render:   {'SKIP' if args.skip_render else 'Enabled'}")
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
    # STEP 2: AI Style Generation (optional)
    # =========================================================================
    skip_styling = args.skip_styling or config.get("skip_styling", False)

    if not skip_styling:
        if not os.environ.get("GEMINI_API_KEY"):
            log.warning("GEMINI_API_KEY not set. Skipping styling step.")
            log.warning("The Blender generator will use default materials.")
        else:
            cmd_style = [
                sys.executable,
                os.path.join(MODULES_DIR, "style_generator.py"),
                geometry_json,
                styling_json
            ]
            run_step(cmd_style, "Step 2: AI Style Generation", critical=False)
    else:
        log.info("Step 2: AI Styling — SKIPPED (--skip-styling)")

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
    # Summary
    # =========================================================================
    log.info("")
    log.info("=" * 60)
    log.info("  ArchX3D Pipeline - COMPLETE")
    log.info("=" * 60)

    outputs = {
        "geometry.json": os.path.join(DATA_DIR, "geometry.json"),
        "model.glb": os.path.join(OUTPUT_DIR, "model.glb"),
        "scene.blend": os.path.join(OUTPUT_DIR, "scene.blend"),
        "walkthrough.mp4": os.path.join(OUTPUT_DIR, "walkthrough.mp4"),
    }
    for name, path in outputs.items():
        if os.path.exists(path):
            size = os.path.getsize(path)
            log.info(f"  [OK] {name:20s} ({size:>10,} bytes)")
        else:
            log.info(f"  [--] {name:20s} (not generated)")


if __name__ == "__main__":
    main()
