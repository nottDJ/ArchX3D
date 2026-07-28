"""
ArchX3D — Preview pipeline
==========================
The orchestrator: turns a scene graph plus a generated ``.blend`` into one
deterministic evaluation image per stored ViewPoint, skipping everything that
has not changed.

    scene graph ──► hashes ──► cache ──► tasks ──► scheduler ──► Blender
                                 │                                   │
                                 └────────────── manifest ◄──────────┘

Three entry points, one implementation
--------------------------------------
``render_scene``      every viewpoint in the graph
``render_room``       every viewpoint in one room
``render_viewpoint``  exactly one

They differ only in which viewpoints they select. Everything downstream —
naming, hashing, caching, batching, the manifest — is shared, which is what
guarantees that ``render_room("kitchen")`` writes the same file, with the same
name and the same content, that ``render_scene()`` would have.

Naming
------
``preview/<room_id>/viewpoint_NN.png``, where ``NN`` is the viewpoint's
position within its room when the room's viewpoints are sorted by image id.
Sorted rather than graph order because the vision pipeline analyses images
concurrently and the order they land in the graph is not stable — using it
would rename files between runs and defeat the cache.

The output path is folded into the cache key, so if a viewpoint *is*
renumbered (one was inserted before it), that counts as a miss and the image
is written afresh rather than left stale under a name that now means something
else.

Scope of a run, and what it may not disturb
-------------------------------------------
A room-scoped run rewrites the manifest, which also describes rooms it did not
touch. Those records are carried through untouched: the similarity pass reads
the manifest as the complete picture of the build, and a run that quietly
narrowed it would look exactly like a build that had lost half its previews.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import cache as cache_mod
from . import manifest as manifest_mod
from . import passes as passes_mod
from . import scheduler as scheduler_mod
from .renderer import RenderSettings, SubprocessRenderer
from .scheduler import Batch, RenderOutcome, RenderTask

#: Where viewpoints with no room land. They are still rendered — a camera
#: without a room is a fitting failure upstream, not a reason to lose the
#: evaluation image.
UNASSIGNED_ROOM = "unassigned"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PreviewConfig:
    """Paths and policy for a preview pass.

    Every path is absolute by the time the pipeline sees it; ``from_config``
    resolves relative entries against the project root so ``config.json`` can
    stay readable.
    """

    base_dir: str
    blend_path: str
    graph_path: str = ""
    geometry_path: str = ""
    preview_dir: str = ""
    manifest_path: str = ""
    cache_path: str = ""

    settings: RenderSettings = field(default_factory=RenderSettings)

    #: ``sequential`` or ``threaded``. Sequential starts Blender once, which is
    #: the fastest option on a single machine.
    scheduler: str = "sequential"
    workers: int = 1
    group_by_room: bool = False

    #: Consult the cache at all. False re-renders everything, which is what a
    #: regression baseline wants.
    use_cache: bool = True
    #: Re-render even on a cache hit, but keep updating the cache. The escape
    #: hatch for "the .blend changed in a way the graph does not describe".
    force: bool = False
    #: Fold each room's neighbours into its hash — see ``cache``.
    include_neighbours: bool = False

    blender_executable: str = ""
    timeout: int = 600
    verbose: bool = True

    # -- construction -------------------------------------------------------

    @staticmethod
    def from_config(config: Optional[Dict[str, Any]] = None,
                    base_dir: str = "",
                    **overrides: Any) -> "PreviewConfig":
        """Build from ``config.json``'s ``preview`` block, plus overrides.

        Unknown keys in the block are ignored rather than fatal: this file is
        hand-edited, and a typo should not stop an evaluation pass.
        """
        config = config or {}
        base = os.path.abspath(base_dir or _default_base_dir())
        block = dict(config.get("preview") or {})

        def resolve(value: str, default: str) -> str:
            path = str(value or default)
            return path if os.path.isabs(path) else os.path.join(base, path)

        output_dir = resolve(config.get("output_dir", ""), "output")
        preview_dir = resolve(block.get("directory", ""), os.path.join(output_dir, "preview"))

        settings = RenderSettings.from_dict(block.get("render") or block)

        cfg = PreviewConfig(
            base_dir=base,
            blend_path=resolve(block.get("blend", ""), os.path.join(output_dir, "scene.blend")),
            graph_path=resolve(block.get("scene_graph", ""),
                               os.path.join(base, "data", "scene_graph.json")),
            geometry_path=resolve(block.get("geometry", ""),
                                  os.path.join(base, "data", "geometry.json")),
            preview_dir=preview_dir,
            manifest_path=resolve(block.get("manifest", ""),
                                  os.path.join(preview_dir, "manifest.json")),
            cache_path=resolve(block.get("cache", ""),
                               os.path.join(base, ".cache", "render", "hash.json")),
            settings=settings,
            scheduler=str(block.get("scheduler", "sequential")),
            workers=int(block.get("workers", 1) or 1),
            group_by_room=bool(block.get("group_by_room", False)),
            use_cache=bool(block.get("cache_enabled", True)),
            include_neighbours=bool(block.get("include_neighbours", False)),
            blender_executable=str(block.get("blender_executable", "")),
            timeout=int(block.get("timeout", 600) or 600),
            verbose=bool(block.get("verbose", True)),
        )

        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg


def _default_base_dir() -> str:
    """The project root, two directories up from ``modules/render``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class PreviewReport:
    """What one pass did. Returned to callers; summarised into the manifest."""

    scope: str = "scene"
    rendered: int = 0
    cached: int = 0
    failed: int = 0
    duration_ms: int = 0
    manifest_path: str = ""
    records: List[manifest_mod.RenderRecord] = field(default_factory=list)
    #: Human-readable explanations: no viewpoints, no Blender, pruned records.
    notes: List[str] = field(default_factory=list)
    cache_stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.rendered + self.cached + self.failed

    @property
    def ok(self) -> bool:
        """True when nothing failed. An empty pass is a success, not a failure.

        A graph with no viewpoints is a legitimate state — it means no
        reference photographs were supplied — and it should not read as a
        broken build.
        """
        return self.failed == 0

    def summary(self) -> str:
        if not self.total:
            return f"no previews ({self.scope})"
        parts = [f"{self.rendered} rendered", f"{self.cached} cached"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        return f"{', '.join(parts)} in {self.duration_ms} ms"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "rendered": self.rendered,
            "cached": self.cached,
            "failed": self.failed,
            "total": self.total,
            "duration_ms": self.duration_ms,
            "manifest": self.manifest_path,
            "cache": self.cache_stats,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class PreviewPipeline:
    """Plans, caches, schedules and records a preview pass.

    The executor is injected rather than constructed: production passes a
    :class:`renderer.SubprocessRenderer`, ``blender_generator`` passes an
    ``InlineRenderer`` because it is already inside Blender, and tests pass a
    function. Nothing in this class knows Blender exists.
    """

    def __init__(self, graph, config: PreviewConfig,
                 executor: Optional[Callable[[Batch], List[RenderOutcome]]] = None) -> None:
        self.graph = graph
        self.config = config
        self.settings = config.settings
        self._executor = executor
        self.cache = cache_mod.RenderCache(config.cache_path, enabled=config.use_cache)
        self.manifest = manifest_mod.Manifest.load(config.manifest_path)
        self.manifest.root = os.path.dirname(os.path.abspath(config.manifest_path))
        # Stats are rebuilt every run, but the ID index map must survive a run
        # that rendered nothing: it still describes the previews on disk.
        self._previous_stats = dict(self.manifest.stats)
        self._pass_index: Optional[Dict[str, Any]] = None
        self.scheduler = scheduler_mod.make_scheduler(config.scheduler, config.workers)

    # -- selection ----------------------------------------------------------

    def viewpoints(self) -> List[Any]:
        return list(getattr(self.graph, "viewpoints", []) or [])

    def _numbering(self) -> Dict[str, int]:
        """Viewpoint id -> its 1-based index within its room.

        Computed from the whole graph even when rendering one room, so a
        room-scoped run cannot rename files a scene-scoped run produced.
        """
        by_room: Dict[str, List[Any]] = {}
        for viewpoint in self.viewpoints():
            room = viewpoint.room_id or UNASSIGNED_ROOM
            by_room.setdefault(room, []).append(viewpoint)

        numbers: Dict[str, int] = {}
        for room, members in by_room.items():
            for index, viewpoint in enumerate(
                sorted(members, key=lambda v: v.image_id), start=1
            ):
                numbers[viewpoint.image_id] = index
        return numbers

    def image_path(self, viewpoint, number: int) -> str:
        room = _safe_name(viewpoint.room_id or UNASSIGNED_ROOM)
        return os.path.join(self.config.preview_dir, room, f"viewpoint_{number:02d}.png")

    # -- public API ---------------------------------------------------------

    def render_scene(self) -> PreviewReport:
        """Every stored viewpoint."""
        return self._run(self.viewpoints(), scope="scene")

    def render_room(self, room_id: str) -> PreviewReport:
        """Every viewpoint belonging to one room."""
        selected = [v for v in self.viewpoints() if v.room_id == room_id]
        report = self._run(selected, scope=f"room:{room_id}")
        if not selected:
            report.notes.append(f"no viewpoints are assigned to room {room_id!r}")
        return report

    def render_viewpoint(self, viewpoint_id: str) -> PreviewReport:
        """Exactly one viewpoint."""
        selected = [v for v in self.viewpoints() if v.image_id == viewpoint_id]
        report = self._run(selected, scope=f"viewpoint:{viewpoint_id}")
        if not selected:
            report.notes.append(f"no viewpoint with id {viewpoint_id!r}")
        return report

    # -- the pass -----------------------------------------------------------

    def _run(self, selected: Sequence[Any], scope: str) -> PreviewReport:
        started = time.perf_counter()
        report = PreviewReport(scope=scope, manifest_path=self.config.manifest_path)

        hashes = cache_mod.compute(
            self.graph,
            geometry_path=self.config.geometry_path,
            settings_fingerprint=self.settings.fingerprint(),
        )
        if self.config.include_neighbours:
            hashes = cache_mod.with_neighbours(hashes, self.graph)

        # Records for viewpoints the graph no longer has are dropped before
        # anything else, so a deleted camera cannot leave a preview behind that
        # the similarity pass would still score.
        live = {v.image_id for v in self.viewpoints()}
        dropped = self.manifest.prune(live)
        pruned_cache = self.cache.prune(live)
        if dropped or pruned_cache:
            report.notes.append(
                f"dropped {max(len(dropped), pruned_cache)} record(s) for viewpoints "
                "that are no longer in the graph"
            )

        numbering = self._numbering()
        tasks: List[RenderTask] = []

        for viewpoint in selected:
            number = numbering.get(viewpoint.image_id, 1)
            output = self.image_path(viewpoint, number)
            width, height = self.settings.resolution_for(viewpoint.aspect)

            room_hash = hashes.for_room(viewpoint.room_id)
            camera = cache_mod.camera_hash(viewpoint, width, height)
            relative = manifest_mod.relative_image(self.manifest.root, output)
            key = cache_mod.render_key(hashes.scene, room_hash, camera, relative)

            expected_passes = [
                passes_mod.pass_filename(output, name)
                for name in passes_mod.normalise(self.settings.passes)
            ]
            hit = None if self.config.force else self.cache.lookup(
                viewpoint.image_id, key, output, also=expected_passes
            )
            if hit is not None:
                self.manifest.upsert(self._cached_record(viewpoint, hit, relative,
                                                         width, height, hashes, camera))
                report.cached += 1
                continue

            tasks.append(RenderTask(
                viewpoint_id=viewpoint.image_id,
                room_id=viewpoint.room_id or UNASSIGNED_ROOM,
                output=output,
                width=width,
                height=height,
                viewpoint=viewpoint.to_dict(),
                key=key,
                scene_hash=hashes.scene,
                room_hash=room_hash,
                camera_hash=camera,
                source_image=viewpoint.source_image,
            ))

        if tasks:
            self._execute(tasks, report, hashes)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        report.cache_stats = self.cache.stats()
        report.records = [
            r for r in self.manifest.records
            if r.viewpoint_id in {v.image_id for v in selected}
        ]

        self._write(report)
        return report

    def _execute(self, tasks: List[RenderTask], report: PreviewReport,
                 hashes: cache_mod.SceneHashes) -> None:
        """Render the misses and fold the outcomes into cache and manifest."""
        executor = self._executor or self._default_executor()

        checker = getattr(executor, "available", None)
        if checker is not None and not checker():
            report.notes.append(getattr(executor, "unavailable_reason", lambda: "")())

        for task in tasks:
            os.makedirs(os.path.dirname(task.output), exist_ok=True)

        batches = scheduler_mod.partition(
            tasks,
            workers=self.config.workers,
            group_by_room=self.config.group_by_room,
        )
        outcomes = self.scheduler.run(batches, executor)
        self._collect_index_maps(executor)

        by_id = {o.viewpoint_id: o for o in outcomes}
        for task in tasks:
            outcome = by_id.get(task.viewpoint_id)
            if outcome is None or not outcome.ok:
                report.failed += 1
                # A failed render must invalidate whatever the cache thought,
                # or the next run would report a hit on a missing image.
                self.cache.forget(task.viewpoint_id)
                self.manifest.upsert(self._failed_record(task, outcome))
                continue

            report.rendered += 1
            record = self._rendered_record(task, outcome)
            self.manifest.upsert(record)
            self.cache.store(cache_mod.CacheEntry(
                viewpoint_id=task.viewpoint_id,
                key=task.key,
                image=record.image,
                scene_hash=task.scene_hash,
                room_hash=task.room_hash,
                camera_hash=task.camera_hash,
                render_ms=outcome.render_ms,
                timestamp=record.timestamp,
            ))

    def _default_executor(self):
        return SubprocessRenderer(
            blend_path=self.config.blend_path,
            settings=self.settings,
            executable=self.config.blender_executable,
            timeout=self.config.timeout,
            verbose=self.config.verbose,
        )

    # -- record construction ------------------------------------------------

    def _rendered_record(self, task: RenderTask,
                         outcome: RenderOutcome) -> manifest_mod.RenderRecord:
        written = {
            name: manifest_mod.relative_image(self.manifest.root, path)
            for name, path in sorted((outcome.passes or {}).items())
        }
        return manifest_mod.RenderRecord(
            viewpoint_id=task.viewpoint_id,
            room=task.room_id,
            image=manifest_mod.relative_image(self.manifest.root, task.output),
            source_image=task.source_image,
            camera_hash=task.camera_hash,
            scene_hash=task.scene_hash,
            room_hash=task.room_hash,
            width=outcome.width or task.width,
            height=outcome.height or task.height,
            timestamp=cache_mod.timestamp(),
            render_ms=outcome.render_ms,
            status=manifest_mod.STATUS_RENDERED,
            camera_source=outcome.camera_source,
            passes=written,
        )

    def _failed_record(self, task: RenderTask,
                       outcome: Optional[RenderOutcome]) -> manifest_mod.RenderRecord:
        return manifest_mod.RenderRecord(
            viewpoint_id=task.viewpoint_id,
            room=task.room_id,
            image=manifest_mod.relative_image(self.manifest.root, task.output),
            source_image=task.source_image,
            camera_hash=task.camera_hash,
            scene_hash=task.scene_hash,
            room_hash=task.room_hash,
            width=task.width,
            height=task.height,
            timestamp=cache_mod.timestamp(),
            status=manifest_mod.STATUS_FAILED,
            error=(outcome.error if outcome else "no outcome reported"),
        )

    def _cached_record(self, viewpoint, entry: cache_mod.CacheEntry, relative: str,
                       width: int, height: int, hashes: cache_mod.SceneHashes,
                       camera: str) -> manifest_mod.RenderRecord:
        """Carry a hit's existing record forward, or rebuild one from the cache.

        The original ``timestamp`` and ``render_ms`` are preserved: they
        describe when the *image* was made, and overwriting them with this
        run's clock would make a cached preview look freshly rendered.
        """
        existing = self.manifest.record_for(viewpoint.image_id)
        record = existing or manifest_mod.RenderRecord(
            viewpoint_id=viewpoint.image_id,
            room=viewpoint.room_id or UNASSIGNED_ROOM,
            image=relative,
            source_image=viewpoint.source_image,
            width=width,
            height=height,
            timestamp=entry.timestamp,
            render_ms=entry.render_ms,
        )
        record.status = manifest_mod.STATUS_CACHED
        record.error = ""
        record.scene_hash = hashes.scene
        record.room_hash = hashes.for_room(viewpoint.room_id)
        record.camera_hash = camera
        record.image = relative
        return record

    # -- persistence --------------------------------------------------------

    def _collect_index_maps(self, executor) -> None:
        """Carry the ID-pass index maps from the executor into the manifest.

        They describe the build rather than a viewpoint — which Blender object
        index means ``sofa_1`` — so they live once in the manifest's stats. A
        run that rendered nothing leaves the previous maps in place, because
        they are still true.
        """
        maps = getattr(executor, "index_maps", None)
        if not maps:
            return
        merged = dict(self.manifest.stats.get("pass_index") or {})
        for kind, mapping in maps.items():
            merged.setdefault(kind, {}).update(mapping)
        self._pass_index = merged

    def _write(self, report: PreviewReport) -> None:
        self.manifest.stats = {
            "settings": self.settings.to_dict(),
            "settings_fingerprint": self.settings.fingerprint(),
            "blend": os.path.basename(self.config.blend_path),
            "scheduler": self.scheduler.describe(),
            "last_run": report.to_dict(),
        }
        index = getattr(self, "_pass_index", None) or (
            self._previous_stats.get("pass_index") if self._previous_stats else None
        )
        if index:
            self.manifest.stats["pass_index"] = index
        self.manifest.save(self.config.manifest_path)
        self.cache.save()


def _safe_name(value: str) -> str:
    """A filesystem-safe directory name for a room id."""
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(value))
    return cleaned.strip("_") or UNASSIGNED_ROOM


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


def _pipeline(graph=None, config: Optional[PreviewConfig] = None,
              executor=None, **overrides) -> PreviewPipeline:
    config = config or PreviewConfig.from_config(_load_json(_config_path()), **overrides)
    if graph is None:
        graph = _load_graph(config.graph_path)
    return PreviewPipeline(graph, config, executor=executor)


def render_scene(graph=None, config: Optional[PreviewConfig] = None,
                 executor=None, **overrides) -> PreviewReport:
    """Render every stored viewpoint. The whole-building entry point."""
    return _pipeline(graph, config, executor, **overrides).render_scene()


def render_room(room_id: str, graph=None, config: Optional[PreviewConfig] = None,
                executor=None, **overrides) -> PreviewReport:
    """Render one room's viewpoints, leaving every other record intact."""
    return _pipeline(graph, config, executor, **overrides).render_room(room_id)


def render_viewpoint(viewpoint_id: str, graph=None, config: Optional[PreviewConfig] = None,
                     executor=None, **overrides) -> PreviewReport:
    """Render a single viewpoint. The tightest refinement loop there is."""
    return _pipeline(graph, config, executor, **overrides).render_viewpoint(viewpoint_id)


def render_after_generation(graph, config: Optional[Dict[str, Any]] = None,
                            base_dir: str = "") -> PreviewReport:
    """Render previews from inside the Blender that just built the scene.

    Called by ``blender_generator`` once generation is complete. Uses the
    in-memory scene rather than reloading the ``.blend`` it just wrote — the
    file and the memory are the same scene, and a second Blender launch would
    roughly double the cost of the pass.

    Cache and manifest behave exactly as they do for an out-of-process run, so
    a subsequent ``render_scene()`` sees the previews as valid and skips them.
    """
    from .renderer import InlineRenderer

    preview_config = PreviewConfig.from_config(config or {}, base_dir=base_dir)
    executor = InlineRenderer(preview_config.settings)
    return PreviewPipeline(graph, preview_config, executor=executor).render_scene()


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _config_path() -> str:
    return os.path.join(_default_base_dir(), "config.json")


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_graph(path: str):
    """Load the scene graph, with a message a user can act on if it is absent."""
    modules_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)
    from vision.schema import SceneGraph

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"scene graph not found at {path}; run the vision pipeline first"
        )
    return SceneGraph.load(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python modules/render/preview.py [--room X | --viewpoint Y]``.

    Standalone so a preview pass can be re-run against an existing ``.blend``
    without re-running generation — which is the normal case while tuning
    materials or lighting.
    """
    parser = argparse.ArgumentParser(description="ArchX3D preview render pass")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--room", help="render only this room's viewpoints")
    scope.add_argument("--viewpoint", help="render only this viewpoint")
    parser.add_argument("--force", action="store_true",
                        help="re-render even when the cache is valid")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore and do not write the cache")
    parser.add_argument("--width", type=int, help="preview width in pixels")
    parser.add_argument("--samples", type=int, help="EEVEE sample count")
    parser.add_argument("--workers", type=int, help="parallel Blender processes")
    parser.add_argument("--blend", help="path to the generated .blend")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    config = PreviewConfig.from_config(_load_json(_config_path()))
    if args.width:
        config.settings.width = args.width
    if args.samples:
        config.settings.samples = args.samples
    if args.workers:
        config.workers = args.workers
        config.scheduler = "threaded" if args.workers > 1 else "sequential"
    if args.blend:
        config.blend_path = os.path.abspath(args.blend)
    config.force = args.force
    config.use_cache = not args.no_cache
    config.verbose = not args.quiet

    try:
        graph = _load_graph(config.graph_path)
    except FileNotFoundError as exc:
        print(f"[PREVIEW] {exc}")
        return 1

    pipeline = PreviewPipeline(graph, config)
    if args.room:
        report = pipeline.render_room(args.room)
    elif args.viewpoint:
        report = pipeline.render_viewpoint(args.viewpoint)
    else:
        report = pipeline.render_scene()

    for note in report.notes:
        print(f"[PREVIEW] {note}")
    print(f"[PREVIEW] {report.summary()}")
    print(f"[PREVIEW] manifest: {report.manifest_path}")
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
