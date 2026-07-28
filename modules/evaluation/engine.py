"""
ArchX3D — Evaluation engine
===========================
The orchestrator: resolves each viewpoint's reference photograph and render,
runs the five axes over them, aggregates upward, and writes the four
documents.

    manifest ──┐
    graph ─────┼──► ViewContext ──► axes ──► ViewpointEvaluation
    references ┘                                    │
                                                    ▼
                                        RoomEvaluation ──► BuildingSummary
                                                    │
                                                    ▼
                        evaluation.json  per_viewpoint.json
                        per_room.json    building_summary.json

It measures and nothing else
----------------------------
The scene graph is loaded read-only and never written back. Every number here
is derived: run it twice on the same inputs and the four documents are
identical apart from their timestamps. That is what makes an evaluation
usable as a regression baseline, and it is why no part of this package can
"helpfully" correct what it finds.

Resolving a reference photograph
--------------------------------
The manifest records a ``source_image`` filename, not a path — the vision pass
stored what it was given. Finding the file again means looking in the places a
project keeps them: the configured images directory, the project root, beside
the graph. When it cannot be found, that viewpoint's pixel axes are unmeasured
and the note says which filename was sought, because "colour: not measured" on
its own sends people looking in the wrong place.
"""

from __future__ import annotations

import json
import os
import sys
import time

if __name__ == "__main__" and __package__ in (None, ""):
    # Run as a loose script (``python modules/evaluation/engine.py``), there is
    # no package for the relative imports below to resolve against. Putting
    # ``modules/`` on the path and adopting the package name makes the
    # documented command work without a launcher script or a PYTHONPATH.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Aliased so the package name does not shadow the ``evaluation`` locals
    # used throughout this module.
    import evaluation as _package  # noqa: F401  (registers the package)

    __package__ = "evaluation"
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import imaging, scoring
from .axes import colour as colour_axis
from .axes import layout as layout_axis
from .axes import lighting as lighting_axis
from .axes import material as material_axis
from .axes import objects as objects_axis
from .context import ViewContext
from .projection import Camera
from .schema import (
    DEFAULT_WEIGHTS,
    OBJECTS,
    BuildingSummary,
    EvaluationResult,
    Finding,
    RoomEvaluation,
    ViewpointEvaluation,
)

EVALUATION_VERSION = "1.0"

#: Where reference photographs are looked for, relative to the project root.
DEFAULT_IMAGE_DIRS = ("reference_images", "data/reference_images", "images")

#: File names the engine writes.
DOCUMENTS = ("evaluation.json", "per_viewpoint.json", "per_room.json",
             "building_summary.json")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EvaluationConfig:
    """Where to read from, where to write to, and the thresholds in between."""

    base_dir: str = ""
    #: The preview manifest produced by Phase 2.
    manifest_path: str = ""
    graph_path: str = ""
    #: Directories searched for reference photographs, in order.
    image_dirs: Tuple[str, ...] = DEFAULT_IMAGE_DIRS
    #: Where the four documents and the HTML report are written.
    output_dir: str = ""

    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    #: Metres per full-scale depth byte; must match what the render used.
    depth_range: float = 20.0
    #: Mirrors ``ConfidencePolicy.REVIEW``, quoted in omission remedies.
    review_threshold: float = 0.40

    #: Write difference overlays beside the report. Costs one small PNG per
    #: viewpoint and is the fastest way for a human to see what a number means.
    write_overlays: bool = True
    #: Emit the HTML report as well as the JSON.
    write_html: bool = True
    verbose: bool = True

    @staticmethod
    def from_config(config: Optional[Dict[str, Any]] = None, base_dir: str = "",
                    **overrides: Any) -> "EvaluationConfig":
        config = config or {}
        base = os.path.abspath(base_dir or _default_base_dir())
        block = dict(config.get("evaluation") or {})
        preview = dict(config.get("preview") or {})
        vision = dict(config.get("vision") or {})

        def resolve(value: str, default: str) -> str:
            path = str(value or default)
            return path if os.path.isabs(path) else os.path.join(base, path)

        output_root = resolve(config.get("output_dir", ""), "output")
        preview_dir = resolve(preview.get("directory", ""),
                              os.path.join(output_root, "preview"))

        image_dirs: List[str] = []
        configured = block.get("image_dirs") or vision.get("images_dir")
        if isinstance(configured, str):
            image_dirs.append(configured)
        elif configured:
            image_dirs.extend(str(d) for d in configured)
        image_dirs.extend(DEFAULT_IMAGE_DIRS)

        weights = dict(DEFAULT_WEIGHTS)
        for axis, value in (block.get("weights") or {}).items():
            if axis in weights:
                try:
                    weights[axis] = max(0.0, float(value))
                except (TypeError, ValueError):
                    pass

        built = EvaluationConfig(
            base_dir=base,
            manifest_path=resolve(block.get("manifest", ""),
                                  os.path.join(preview_dir, "manifest.json")),
            graph_path=resolve(block.get("scene_graph", ""),
                               os.path.join(base, "data", "scene_graph.json")),
            image_dirs=tuple(dict.fromkeys(image_dirs)),
            output_dir=resolve(block.get("directory", ""),
                               os.path.join(output_root, "evaluation")),
            weights=weights,
            depth_range=float(preview.get("depth_range", 20.0) or 20.0),
            write_overlays=bool(block.get("overlays", True)),
            write_html=bool(block.get("html", True)),
            verbose=bool(block.get("verbose", True)),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(built, key):
                setattr(built, key, value)
        return built

    def resolve_image(self, filename: str) -> str:
        """Find a reference photograph by the name the graph recorded.

        Tries the name as given (it may already be a path), then each
        configured directory, then a case-insensitive stem match — the vision
        pass may have recorded ``img0`` for ``img0.jpg``, and a run should not
        be lost to a file extension.
        """
        if not filename:
            return ""
        if os.path.isabs(filename) and os.path.exists(filename):
            return filename

        candidates = [os.path.join(self.base_dir, filename)]
        for directory in self.image_dirs:
            root = directory if os.path.isabs(directory) else os.path.join(
                self.base_dir, directory
            )
            candidates.append(os.path.join(root, filename))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate

        stem = os.path.splitext(os.path.basename(filename))[0].lower()
        for directory in self.image_dirs:
            root = directory if os.path.isabs(directory) else os.path.join(
                self.base_dir, directory
            )
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                if os.path.splitext(entry)[0].lower() == stem:
                    return os.path.join(root, entry)
        return ""


def _default_base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class Evaluator:
    """Runs the axes over a build and assembles the result.

    Holds no state between runs beyond its configuration, so the same
    evaluator can be re-used and will not carry a previous build's numbers
    into the next one.
    """

    def __init__(self, graph, manifest, config: EvaluationConfig) -> None:
        self.graph = graph
        self.manifest = manifest
        self.config = config
        self.notes: List[str] = []

    # -- per viewpoint ------------------------------------------------------

    def evaluate_viewpoint(self, record, viewpoint) -> ViewpointEvaluation:
        """Score one reference/render pair across all five axes."""
        evaluation = ViewpointEvaluation(
            viewpoint_id=record.viewpoint_id,
            room=record.room,
            render=self.manifest.resolve(record),
            scene_hash=record.scene_hash,
            camera_hash=record.camera_hash,
        )

        reference = self.config.resolve_image(
            record.source_image or getattr(viewpoint, "source_image", "")
        )
        evaluation.reference = reference
        if not reference and record.source_image:
            evaluation.notes.append(
                f"reference image {record.source_image!r} not found in "
                f"{', '.join(self.config.image_dirs)}"
            )

        context = self._context(record, viewpoint, reference)
        evaluation.passes_used = sorted(
            name for name, value in context.pair.passes.items() if value is not None
        )
        evaluation.notes.extend(context.pair.notes)

        findings: List[Finding] = []
        for axis_module in (colour_axis, material_axis, lighting_axis, layout_axis):
            score, produced = axis_module.evaluate(context)
            evaluation.axes[score.axis] = score
            findings.extend(produced)

        score, produced = objects_axis.evaluate(context, scope="viewpoint")
        evaluation.axes[score.axis] = score
        findings.extend(produced)

        evaluation.findings = findings
        evaluation.totals = scoring.combine(evaluation.axes, self.config.weights)
        return evaluation

    def _context(self, record, viewpoint, reference_path: str) -> ViewContext:
        pair = imaging.load_pair(reference_path, self.manifest.resolve(record))
        if pair.render is not None:
            self._load_passes(record, pair)

        room = self.graph.room_by_id(record.room) if self.graph else None
        camera = Camera.from_viewpoint(viewpoint) if viewpoint is not None else None

        return ViewContext(
            viewpoint_id=record.viewpoint_id,
            room_id=record.room,
            viewpoint=viewpoint,
            graph=self.graph,
            room=room,
            pair=pair,
            index_map=self._index_map(),
            depth_range=self.config.depth_range,
            camera=camera,
            config=self.config,
        )

    def _load_passes(self, record, pair) -> None:
        """Decode whichever auxiliary passes this record advertises.

        ID passes are loaded without interpolation; everything else is
        resampled to the render's working size so the arrays line up.
        """
        from render import passes as passes_mod

        size = pair.shape
        for name, relative in (record.passes or {}).items():
            path = os.path.normpath(os.path.join(self.manifest.root, relative))
            if name in passes_mod.INDEX_PASSES or name == passes_mod.DEPTH:
                # Depth joins the ID passes in refusing interpolation: an
                # averaged depth is a surface that is not there.
                data = imaging.load_raw(path, size=size)
            elif name == passes_mod.NORMAL:
                data = imaging.load_raw(path, size=size)
            else:
                data = imaging.load_rgb(path, size=size)
            if data is None:
                pair.notes.append(f"{name} pass listed but not readable")
                continue
            pair.passes[name] = data

    def _index_map(self):
        from render import passes as passes_mod

        return passes_mod.IndexMap.from_dict(self.manifest.stats.get("pass_index"))

    # -- per room -----------------------------------------------------------

    def evaluate_room(self, room_id: str,
                      viewpoints: Sequence[ViewpointEvaluation]) -> RoomEvaluation:
        """Aggregate a room's viewpoints, plus its room-scope object check."""
        room = self.graph.room_by_id(room_id) if self.graph else None
        evaluation = RoomEvaluation(
            room_id=room_id,
            room_type=getattr(room, "room_type", "") or "",
            style=getattr(room, "style", "") or "",
            viewpoint_ids=[v.viewpoint_id for v in viewpoints],
        )

        evaluation.axes = scoring.merge_axes([v.axes for v in viewpoints])

        # The object axis is re-run at room scope. A per-viewpoint check can
        # only see what one photograph framed; an object nobody photographed
        # but the room should contain surfaces only here.
        context = ViewContext(room_id=room_id, graph=self.graph, room=room,
                              config=self.config)
        room_objects, object_findings = objects_axis.evaluate(context, scope="room")
        if room_objects.measured:
            evaluation.axes[OBJECTS] = room_objects

        evaluation.findings = object_findings
        evaluation.totals = scoring.combine(evaluation.axes, self.config.weights)
        if not viewpoints:
            evaluation.notes.append(
                "no viewpoints reference this room; only the object axis applies"
            )
        return evaluation

    # -- the whole build ----------------------------------------------------

    def run(self) -> EvaluationResult:
        started = time.perf_counter()
        result = EvaluationResult(generated_at=_now())

        viewpoints_by_id = {
            v.image_id: v for v in (getattr(self.graph, "viewpoints", []) or [])
        }
        records = [r for r in self.manifest.records if r.ok]

        for record in sorted(records, key=lambda r: (r.room, r.viewpoint_id)):
            evaluation = self.evaluate_viewpoint(
                record, viewpoints_by_id.get(record.viewpoint_id)
            )
            if record.viewpoint_id not in viewpoints_by_id:
                evaluation.notes.append(
                    "no ViewPoint in the graph for this render; layout "
                    "displacement could not be measured"
                )
            result.viewpoints.append(evaluation)

        room_ids = _room_ids(self.graph, result.viewpoints)
        for room_id in room_ids:
            members = [v for v in result.viewpoints if v.room == room_id]
            result.rooms.append(self.evaluate_room(room_id, members))

        result.building = self._summarise(result)
        result.notes = self._notes(result)
        result.metadata = {
            "evaluation_version": EVALUATION_VERSION,
            "weights": dict(self.config.weights),
            "manifest": self.config.manifest_path,
            "scene_graph": self.config.graph_path,
            "image_dirs": list(self.config.image_dirs),
            "pixel_backend": "numpy+Pillow" if imaging.available()
                             else imaging.unavailable_reason(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        return result

    def _summarise(self, result: EvaluationResult) -> BuildingSummary:
        summary = BuildingSummary()
        if not result.rooms:
            return summary

        summary.axes = scoring.merge_axes([r.axes for r in result.rooms])

        # A room where nothing could be measured — no viewpoint, no objects —
        # is excluded rather than scored zero. Averaging it in as 0.0 would
        # say the reconstruction is wrong there, which is a claim about a
        # room nobody looked at. This is the same rule the axes follow,
        # applied one level up.
        assessed = [r for r in result.rooms if r.totals.weight_used > 0.0]
        summary.room_scores = {r.room_id: r.totals.score for r in assessed}

        totals = scoring.combine(summary.axes, self.config.weights)
        # Area-weight the rooms rather than take the axis means directly: the
        # merged axes treat every room alike, and a cupboard should not sway
        # the building's verdict as much as a living room.
        if assessed:
            totals.score = scoring.weighted_mean(
                (room.totals.score,
                 scoring.room_weight(
                     self.graph.room_by_id(room.room_id) if self.graph else None))
                for room in assessed
            )
        summary.totals = totals

        collected: List[Finding] = []
        for viewpoint in result.viewpoints:
            collected.extend(viewpoint.findings)
        for room in result.rooms:
            collected.extend(room.findings)

        summary.findings = scoring.top_findings(collected)
        summary.subsystem_pressure = scoring.subsystem_pressure(summary.findings)

        passes_seen = sorted({p for v in result.viewpoints for p in v.passes_used})
        summary.coverage = scoring.coverage(
            total_viewpoints=len(getattr(self.graph, "viewpoints", []) or [])
                             or len(result.viewpoints),
            evaluated=len(result.viewpoints),
            with_reference=sum(1 for v in result.viewpoints if v.reference),
            passes_seen=passes_seen,
        )
        summary.coverage["rooms_total"] = len(result.rooms)
        summary.coverage["rooms_assessed"] = len(assessed)
        return summary

    def _notes(self, result: EvaluationResult) -> List[str]:
        notes = list(self.notes)
        if not imaging.available():
            notes.append(imaging.unavailable_reason())
        without = [v.viewpoint_id for v in result.viewpoints if not v.reference]
        if without:
            notes.append(
                f"{len(without)} viewpoint(s) had no reference photograph, so "
                f"only the object axis applied to them: {', '.join(without[:6])}"
            )
        if not result.viewpoints:
            notes.append(
                "no successful renders in the manifest; run the preview "
                "pipeline first"
            )
        unassessed = [r.room_id for r in result.rooms if r.totals.weight_used <= 0.0]
        if unassessed:
            notes.append(
                f"{len(unassessed)} room(s) had nothing measurable and are "
                f"excluded from the building score: {', '.join(unassessed[:6])}"
            )
        return notes


def _room_ids(graph, viewpoints: Sequence[ViewpointEvaluation]) -> List[str]:
    """Every room worth reporting: those with renders, plus the graph's own.

    Rooms with no viewpoint still appear, carrying their object comparison and
    an explicit note. Dropping them would make an unphotographed room look
    like a room that does not exist.
    """
    ids = [v.room for v in viewpoints if v.room]
    for room in getattr(graph, "rooms", []) or []:
        ids.append(room.id)
    seen: List[str] = []
    for room_id in ids:
        if room_id and room_id not in seen:
            seen.append(room_id)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(graph=None, manifest=None, config: Optional[EvaluationConfig] = None,
             write: bool = True, **overrides) -> EvaluationResult:
    """Evaluate a reconstruction against the photographs it was built from.

    Loads the scene graph and preview manifest when not supplied, runs every
    axis over every rendered viewpoint, and — unless ``write=False`` — writes
    the four JSON documents and the HTML report.

    The scene graph is never modified.
    """
    config = config or EvaluationConfig.from_config(
        _load_json(_config_path()), **overrides
    )
    graph = graph if graph is not None else _load_graph(config.graph_path)
    manifest = manifest if manifest is not None else _load_manifest(config.manifest_path)

    result = Evaluator(graph, manifest, config).run()
    if write:
        write_documents(result, config)
    return result


def write_documents(result: EvaluationResult, config: EvaluationConfig) -> List[str]:
    """Write the four JSON documents, and the HTML report when enabled.

    Four files rather than one because they have different readers: a
    refinement pass wants ``per_viewpoint``, a dashboard wants
    ``building_summary``, and a human debugging one room wants ``per_room``.
    Making each of them parse the full document to find its slice would be a
    needless coupling.
    """
    os.makedirs(config.output_dir, exist_ok=True)
    written: List[str] = []

    documents = {
        "evaluation.json": result.to_dict(),
        "per_viewpoint.json": result.viewpoint_document(),
        "per_room.json": result.room_document(),
        "building_summary.json": result.building_document(),
    }
    for name, payload in documents.items():
        path = os.path.join(config.output_dir, name)
        _write_json(path, payload)
        written.append(path)

    if config.write_html:
        from . import report as report_mod

        written.extend(report_mod.write(result, config))
    return written


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _config_path() -> str:
    return os.path.join(_default_base_dir(), "config.json")


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _ensure_modules_on_path() -> None:
    modules = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if modules not in sys.path:
        sys.path.insert(0, modules)


def _load_graph(path: str):
    _ensure_modules_on_path()
    from vision.schema import SceneGraph

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"scene graph not found at {path}; run the vision pipeline first"
        )
    return SceneGraph.load(path)


def _load_manifest(path: str):
    _ensure_modules_on_path()
    from render.manifest import Manifest

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"preview manifest not found at {path}; run the preview pipeline first"
        )
    return Manifest.load(path)


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python modules/evaluation/engine.py``.

    Standalone because evaluation is the step you re-run most: after every
    material tweak, every lighting change, every re-fit. It reads what the
    preview pipeline already wrote and touches nothing else.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="ArchX3D reconstruction evaluation"
    )
    parser.add_argument("--manifest", help="preview manifest to evaluate")
    parser.add_argument("--scene-graph", help="scene graph to evaluate against")
    parser.add_argument("--images", help="directory holding the reference photographs")
    parser.add_argument("--output", help="where to write the documents and report")
    parser.add_argument("--no-html", action="store_true",
                        help="write only the JSON documents")
    parser.add_argument("--top", type=int, default=10,
                        help="how many findings to print (default 10)")
    args = parser.parse_args(argv)

    config = EvaluationConfig.from_config(_load_json(_config_path()))
    if args.manifest:
        config.manifest_path = os.path.abspath(args.manifest)
    if args.scene_graph:
        config.graph_path = os.path.abspath(args.scene_graph)
    if args.images:
        config.image_dirs = (os.path.abspath(args.images),) + config.image_dirs
    if args.output:
        config.output_dir = os.path.abspath(args.output)
    config.write_html = not args.no_html

    try:
        result = evaluate(config=config)
    except FileNotFoundError as exc:
        print(f"[EVAL] {exc}")
        return 1

    for note in result.notes:
        print(f"[EVAL] {note}")
    print(f"[EVAL] {result.summary()}")

    for finding in result.findings[:args.top]:
        unit = finding.unit or ""
        print(f"[EVAL]   {finding.severity:.2f} [{finding.axis}] {finding.summary} "
              f"({finding.difference:.2f}{unit}) -> {finding.subsystem}")

    pressure = result.building.subsystem_pressure
    if pressure:
        leader = max(pressure.items(), key=lambda kv: kv[1])
        print(f"[EVAL] evidence points hardest at {leader[0]} ({leader[1]:.2f})")
    print(f"[EVAL] documents: {config.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
