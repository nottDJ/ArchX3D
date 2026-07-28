"""
ArchX3D — Scene Analyzer (pipeline step 2)
==========================================
CLI wrapper around the vision pipeline.

    python modules/scene_analyzer.py <geometry.json> <scene_graph.json> \
        --images ref1.jpg ref2.jpg [--model gemini-2.5-pro] [--no-cache]

Replaces the old ``style_generator.py`` step, which sent DXF *coordinates* as
text to Gemini and produced furniture names that nothing consumed. This step
sends the actual reference photographs and produces a scene graph the Blender
generator builds from.

Exit codes: 0 on success (including a partial scene), 2 when no usable
observations were produced. `main.py` treats step 2 as non-critical, so a
vision failure still yields an unfurnished but correct architectural model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow both `python modules/scene_analyzer.py` and `python -m modules.scene_analyzer`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# On Windows a piped stdout defaults to cp1252, which cannot encode the box
# characters and arrows used in progress output. Force UTF-8 so a log line can
# never abort the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-standard stream
        pass

from vision import pipeline  # noqa: E402
from vision.pipeline import PipelineConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyse reference interior images into an ArchX3D scene graph."
    )
    parser.add_argument("geometry_json", help="DXF-extracted geometry.json")
    parser.add_argument("output_json", help="Where to write scene_graph.json")
    parser.add_argument(
        "--images",
        nargs="+",
        required=True,
        help="Reference image files, or a directory of them",
    )
    parser.add_argument("--model", default=None, help="Vision model id")
    parser.add_argument("--fallback-model", default=None, help="Model used if the primary fails")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the response cache")
    parser.add_argument("--cache-dir", default=None, help="Cache location")
    parser.add_argument("--wall-height", type=float, default=None, help="Ceiling height, metres")
    parser.add_argument("--max-images", type=int, default=None, help="Cap on images analysed")
    parser.add_argument(
        "--include-uncertain",
        action="store_true",
        help="Build low-confidence detections instead of withholding them",
    )
    parser.add_argument(
        "--single-room",
        action="store_true",
        help="Skip room segmentation and treat the plan as one open space",
    )
    parser.add_argument(
        "--gap-closing",
        type=float,
        default=None,
        help="Doorway width used to separate rooms, metres (default 1.10)",
    )
    parser.add_argument(
        "--review-out",
        default=None,
        help="Also write the review payload the validation UI consumes",
    )
    parser.add_argument(
        "--apply-edits",
        default=None,
        help="Apply a review-edits JSON file to an existing scene graph and exit",
    )
    args = parser.parse_args()

    # Edit application is a separate, cheap operation: it rewrites an existing
    # graph after the user's review and never re-queries the model.
    if args.apply_edits:
        return _apply_edits(args)

    if not os.path.exists(args.geometry_json):
        print(f"[ERROR] geometry file not found: {args.geometry_json}")
        return 2

    with open(args.geometry_json, "r", encoding="utf-8") as fh:
        geometry = json.load(fh)

    # Expand any directories into their image files.
    images = []
    for entry in args.images:
        found = pipeline.discover_images(entry)
        if not found:
            print(f"[WARN] no images found at: {entry}")
        images.extend(found)

    if not images:
        print("[ERROR] no reference images resolved from --images")
        return 2

    defaults = PipelineConfig()
    config = PipelineConfig(
        model=args.model or defaults.model,
        fallback_model=args.fallback_model or defaults.fallback_model,
        cache_dir=args.cache_dir or defaults.cache_dir,
        use_cache=not args.no_cache,
        max_images=args.max_images or defaults.max_images,
        wall_height=args.wall_height or defaults.wall_height,
        include_uncertain=args.include_uncertain,
        single_room=args.single_room,
        gap_closing_m=args.gap_closing or defaults.gap_closing_m,
    )

    print(f"[VISION] Analysing {len(images)} reference image(s) with {config.model}")
    result = pipeline.analyse(images, geometry, config)

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
    result.graph.save(args.output_json)
    print(f"[VISION] Scene graph written to {args.output_json}")

    if args.review_out:
        _write_review(result.graph, args.review_out)

    _summarise(result)
    return 0 if result.ok else 2


def _write_review(graph, path: str) -> None:
    """Emit the payload the wizard's validation step renders."""
    from vision import review

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(review.build_review(graph), fh, indent=2)
    print(f"[VISION] Review payload written to {path}")


def _apply_edits(args) -> int:
    """Rewrite an existing scene graph with the user's review decisions."""
    from vision import review
    from vision.schema import SceneGraph

    if not os.path.exists(args.output_json):
        print(f"[ERROR] scene graph not found: {args.output_json}")
        return 2
    if not os.path.exists(args.apply_edits):
        print(f"[ERROR] edits file not found: {args.apply_edits}")
        return 2

    graph = SceneGraph.load(args.output_json)
    with open(args.apply_edits, "r", encoding="utf-8") as fh:
        edits = json.load(fh)

    updated, report = review.apply_edits(graph, edits)
    updated.save(args.output_json)

    summary = report.to_dict()
    print(f"[REVIEW] Applied {summary['total_changes']} change(s) to {args.output_json}")
    for key in ("removed_objects", "kept_uncertain_objects", "recategorised",
                "moved_between_rooms", "room_types_changed", "lights_removed"):
        if summary[key]:
            print(f"  {key}: {len(summary[key])} -> {', '.join(summary[key][:6])}"
                  + (" ..." if len(summary[key]) > 6 else ""))
    for rejected in summary["rejected_edits"]:
        print(f"  [!] {rejected}")

    if args.review_out:
        _write_review(updated, args.review_out)

    print(f"[REVIEW] {len(updated.buildable_objects())} objects will now be built")
    return 0


def _summarise(result) -> None:
    """Print a short human-readable account of what was reconstructed."""
    graph = result.graph
    buildable = graph.buildable_objects()

    print("-" * 60)
    print(f"  Room       : {graph.room.room_type} ({graph.room.style}), "
          f"{graph.room.width:.1f} x {graph.room.depth:.1f} m")
    print(f"  Objects    : {len(buildable)} buildable / {len(graph.objects)} detected")
    print(f"  Lights     : {len(graph.buildable_lights())}")
    print(f"  Openings   : {len(graph.openings)}")
    print(f"  Finishes   : wall={graph.floor.material and graph.walls[0].finish.material if graph.walls else 'n/a'}, "
          f"floor={graph.floor.material}, ceiling={graph.ceiling.material} ({graph.ceiling_type})")

    confidence = graph.diagnostics.get("confidence", {})
    if confidence.get("objects"):
        print(f"  Confidence : mean {confidence['mean']:.2f}, "
              f"{confidence['accepted']} accepted, {confidence['uncertain']} uncertain")

    for obj in sorted(buildable, key=lambda o: -o.confidence)[:12]:
        print(f"    - {obj.category:16s} conf={obj.confidence:.2f} "
              f"pos=({obj.position.x:6.2f},{obj.position.y:6.2f}) "
              f"rot={obj.rotation_z:5.0f}° "
              f"{obj.dimensions.width:.2f}x{obj.dimensions.depth:.2f}x{obj.dimensions.height:.2f}m "
              f"[{obj.asset}]")

    uncertain = [o for o in graph.objects if o.uncertain]
    if uncertain:
        print(f"  Flagged uncertain (not built): "
              f"{', '.join(sorted({o.category for o in uncertain}))}")

    if result.errors:
        print(f"  Errors     : {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    ! {error}")
    print("-" * 60)


if __name__ == "__main__":
    sys.exit(main())
