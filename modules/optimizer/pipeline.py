"""
ArchX3D — Wiring the loop to the real pipeline
==============================================
The executor the optimiser calls, and the end-to-end entry point that puts
planner and optimiser together.

What one cycle costs
--------------------
The loop's abstract step — "measure this graph" — is concretely:

1. write the mutated scene graph to a working copy;
2. run ``blender_generator`` to rebuild the ``.blend`` (~40 s);
3. render the previews and their passes (~1.5 s per viewpoint, cached);
4. evaluate them against the reference photographs (~50 ms).

Step 2 is the whole cost, and it is why :class:`planner.action_graph.ActionType`
distinguishes camera-only actions: a viewpoint correction changes nothing that
is built, so the rebuild is skipped and the same cycle takes about three
seconds instead of forty.

Working copies
--------------
Generation reads ``data/scene_graph.json`` and writes ``output/scene.blend``.
An optimisation run must not overwrite either — a rejected action would leave
the project holding a build nobody asked for. So the executor works in its own
directory and the project's files are untouched until the caller decides to
keep the result.

The preview cache earns its keep here
-------------------------------------
Phase 2's hashing is per-room and per-viewpoint, so an action that changes one
room re-renders that room's viewpoints and reuses the rest. On a multi-room
building that is most of the render cost avoided on every iteration, without
the optimiser having to know anything about it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

if __name__ == "__main__" and __package__ in (None, ""):
    # Run as a loose script, there is no package for the relative imports to
    # resolve against. Same bootstrap as ``evaluation.engine``, for the same
    # reason: one documented command that needs no launcher or PYTHONPATH.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import optimizer as _package  # noqa: F401  (registers the package)

    __package__ = "optimizer"
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .optimizer import ExecutionResult

#: Environment variable the generator reads to skip its frame-rendering loop.
#: Every optimisation cycle sets it: the walkthrough animation is a deliverable
#: and has nothing to do with scoring.
SKIP_FRAMES = "ARCHX3D_SKIP_RENDER"


@dataclass
class PipelineExecutor:
    """Turns a scene graph into an evaluation of it, the real way.

    Callable, so it satisfies the optimiser's ``Executor`` contract directly.
    Holds the working paths and the counters that let ``metrics.json`` say
    where the time went.
    """

    #: Working directory. Everything the run writes lives under here.
    work_dir: str
    #: The project the run is optimising, read for its DXF geometry and
    #: reference photographs. Never written to.
    base_dir: str
    blender: str = ""
    #: Seconds before a generation is abandoned.
    timeout: int = 900
    verbose: bool = True

    rebuilds: int = 0
    renders: int = 0
    evaluations: int = 0
    render_ms: int = 0
    evaluate_ms: int = 0
    generate_ms: int = 0
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Absolute from the outset. Generation runs with the working tree as
        # its CWD, so a relative path handed in on the command line would be
        # resolved a second time against itself — which fails in a way that
        # reads as "geometry.json not found" rather than as a path bug.
        self.work_dir = os.path.abspath(self.work_dir)
        self.base_dir = os.path.abspath(self.base_dir)

    # -- paths --------------------------------------------------------------

    @property
    def graph_path(self) -> str:
        return os.path.join(self.work_dir, "data", "scene_graph.json")

    @property
    def geometry_path(self) -> str:
        return os.path.join(self.base_dir, "data", "geometry.json")

    @property
    def blend_path(self) -> str:
        return os.path.join(self.work_dir, "output", "scene.blend")

    @property
    def preview_dir(self) -> str:
        return os.path.join(self.work_dir, "output", "preview")

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.preview_dir, "manifest.json")

    def prepare(self) -> None:
        """Create the working tree and copy in what generation needs."""
        for relative in ("data", "output"):
            os.makedirs(os.path.join(self.work_dir, relative), exist_ok=True)
        source = os.path.join(self.base_dir, "data", "geometry.json")
        target = os.path.join(self.work_dir, "data", "geometry.json")
        if os.path.exists(source) and not os.path.exists(target):
            shutil.copy2(source, target)

    # -- the cycle ----------------------------------------------------------

    def __call__(self, graph, rebuild: bool = True) -> ExecutionResult:
        """One measurement of the graph as it currently stands."""
        self.prepare()
        graph.save(self.graph_path)

        # A camera-only action needs no rebuild — but it does need something to
        # render *from*, and on the first cycle the working tree has no .blend
        # at all. Skipping generation then produces a run whose previews all
        # fail while the evaluation quietly scores whatever axes survive, which
        # is a far worse outcome than paying for one rebuild.
        if rebuild or not os.path.exists(self.blend_path):
            ok, error = self._generate()
            if not ok:
                return ExecutionResult(ok=False, error=error)

        started = time.perf_counter()
        report = self._render(graph)
        self.render_ms += int((time.perf_counter() - started) * 1000)
        if report is None:
            return ExecutionResult(ok=False, error="the preview pass failed")
        # A manifest full of *failed* records is not an empty manifest, and the
        # difference matters: the evaluation would happily score the axes that
        # need no image and report an improvement drawn from nothing.
        if report.failed and not (report.rendered or report.cached):
            return ExecutionResult(
                ok=False,
                error=f"every preview failed: {'; '.join(report.notes[:2])}",
            )

        started = time.perf_counter()
        evaluation = self._evaluate(graph)
        elapsed = int((time.perf_counter() - started) * 1000)
        self.evaluate_ms += elapsed
        if evaluation is None:
            return ExecutionResult(ok=False, error="the evaluation failed")

        self.evaluations += 1
        return ExecutionResult(evaluation=evaluation, ok=True,
                               render_ms=self.render_ms, evaluate_ms=elapsed)

    # -- steps --------------------------------------------------------------

    def _generate(self) -> "tuple[bool, str]":
        """Rebuild the .blend from the working scene graph.

        Runs ``blender_generator`` in a background Blender against the working
        directory, so the project's own ``output/`` is never touched. The
        generator's inline preview pass is left enabled: it renders straight
        into the working preview directory, which is exactly what the next
        step wants.
        """
        from render.renderer import blender_executable

        executable = self.blender or blender_executable()
        if not executable:
            return False, "no Blender executable found"

        script = os.path.join(_modules_dir(), "blender_generator.py")
        environment = dict(os.environ)
        environment[SKIP_FRAMES] = "1"
        # The generator resolves its paths from its own location, so it is
        # pointed at the working tree by running it with that as the CWD.
        environment["ARCHX3D_BASE_DIR"] = self.work_dir

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [executable, "--background", "--factory-startup",
                 "--python", script],
                cwd=self.work_dir, env=environment, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"generation timed out after {self.timeout}s"
        except OSError as exc:
            return False, f"could not start Blender: {exc}"

        self.generate_ms += int((time.perf_counter() - started) * 1000)
        self.rebuilds += 1

        if not os.path.exists(self.blend_path):
            tail = "; ".join((completed.stdout or "").strip().splitlines()[-3:])
            return False, f"generation produced no .blend ({tail})"
        return True, ""

    def _render(self, graph):
        """Render the previews the evaluation will score."""
        from render.preview import PreviewConfig, PreviewPipeline

        config = PreviewConfig(
            base_dir=self.work_dir,
            blend_path=self.blend_path,
            graph_path=self.graph_path,
            geometry_path=self.geometry_path,
            preview_dir=self.preview_dir,
            manifest_path=self.manifest_path,
            cache_path=os.path.join(self.work_dir, ".cache", "render", "hash.json"),
            blender_executable=self.blender,
            verbose=False,
        )
        try:
            report = PreviewPipeline(graph, config).render_scene()
        except Exception as exc:  # noqa: BLE001 - a failed render is data
            self.notes.append(f"preview pass raised {type(exc).__name__}: {exc}")
            return None
        self.renders += 1
        return report

    def _evaluate(self, graph):
        """Score the previews against the reference photographs."""
        from evaluation import EvaluationConfig, evaluate
        from render.manifest import Manifest

        config = EvaluationConfig(
            base_dir=self.base_dir,
            manifest_path=self.manifest_path,
            graph_path=self.graph_path,
            output_dir=os.path.join(self.work_dir, "output", "evaluation"),
            # Reports are for the run's final state, not for every iteration:
            # writing an HTML report and its overlays twelve times would cost
            # more than some of the renders.
            write_html=False,
            write_overlays=False,
            verbose=False,
        )
        try:
            return evaluate(graph=graph, manifest=Manifest.load(self.manifest_path),
                            config=config, write=False)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"evaluation raised {type(exc).__name__}: {exc}")
            return None

    def timings(self) -> Dict[str, int]:
        return {
            "render_ms": self.render_ms,
            "evaluate_ms": self.evaluate_ms,
            "generate_ms": self.generate_ms,
            "rebuilds": self.rebuilds,
        }


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@dataclass
class RefinementResult:
    """What a full plan-and-optimise pass produced."""

    plan: Any = None
    history: Any = None
    metrics: Any = None
    graph: Any = None
    documents: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return self.metrics.total_gain if self.metrics else 0.0

    def summary(self) -> str:
        parts = []
        if self.plan is not None:
            parts.append(self.plan.summary())
        if self.metrics is not None:
            parts.append(self.metrics.summary())
        return " | ".join(parts) or "nothing ran"


def refine(graph, evaluation, executor, output_dir: str,
           planner_config=None, optimizer_config=None) -> RefinementResult:
    """Plan from an evaluation, then optimise against it.

    The one call that puts the two halves together. The planner is passed
    through as a *factory* so the optimiser can replan from a fresh evaluation
    once its first plan is exhausted, without ever importing the planner's
    construction logic itself.
    """
    from planner import Planner, write_report
    from .optimizer import Optimizer, OptimizerConfig

    planner = Planner(graph, planner_config)
    plan = planner.plan(evaluation)

    result = RefinementResult(plan=plan, graph=graph)
    os.makedirs(output_dir, exist_ok=True)
    result.documents.append(
        write_report(plan, os.path.join(output_dir, "planner_report.json"))
    )

    if not plan.ordered:
        result.notes.append(
            "the planner proposed no actions; nothing to optimise"
        )
        return result

    config = optimizer_config or OptimizerConfig()
    config.output_dir = config.output_dir or output_dir
    optimizer = Optimizer(graph, executor, config,
                          planner_factory=planner.plan)
    result.history = optimizer.run(plan, baseline=evaluation)
    result.metrics = optimizer.metrics()
    result.documents.extend(optimizer.write_documents(output_dir))
    result.notes.extend(result.metrics.notes)
    return result


def _modules_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    """``python modules/optimizer/pipeline.py``.

    Evaluates the current build, plans from it, and runs the loop. Each
    iteration is a Blender rebuild, so a default run is minutes rather than
    seconds — ``--dry-run`` produces the plan and its report without executing
    anything, which is the right way to see what a run *would* do.
    """
    import argparse
    import json

    from evaluation import EvaluationConfig, evaluate
    from planner import PlannerConfig, Planner, write_report
    from render.manifest import Manifest

    from .optimizer import OptimizerConfig
    from .stopping import StoppingPolicy

    parser = argparse.ArgumentParser(
        description="ArchX3D planning and optimisation"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and write the report, but execute nothing")
    # Defaults come from config.json's ``refinement`` block, so a project can
    # set its own budget once rather than on every invocation.
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--target", type=float, default=None,
                        help="stop once the building score reaches this")
    parser.add_argument("--epsilon", type=float, default=None,
                        help="smallest gain that counts as an improvement")
    parser.add_argument("--max-actions", type=int, default=None,
                        help="ceiling on plan length")
    parser.add_argument("--only", nargs="+", metavar="TYPE",
                        help="restrict to these action types")
    parser.add_argument("--output", help="where to write the three documents")
    parser.add_argument("--work-dir",
                        help="scratch directory for the rebuilds (default: "
                             ".cache/optimize)")
    parser.add_argument("--write-graph", action="store_true",
                        help="save the improved scene graph over data/scene_graph.json")
    args = parser.parse_args(argv)

    base = _project_dir()
    config_path = os.path.join(base, "config.json")
    try:
        with open(config_path, encoding="utf-8") as handle:
            project_config = json.load(handle)
    except (OSError, json.JSONDecodeError):
        project_config = {}

    settings = dict(project_config.get("refinement") or {})

    def setting(name, fallback):
        value = getattr(args, name.replace("-", "_"), None)
        return value if value is not None else settings.get(name, fallback)

    evaluation_config = EvaluationConfig.from_config(project_config, base_dir=base)
    output_dir = args.output or _resolve(base, settings.get("directory"),
                                         "output/refinement")
    work_dir = args.work_dir or _resolve(base, settings.get("work_dir"),
                                         ".cache/optimize")

    try:
        graph = _load_graph(evaluation_config.graph_path)
        manifest = Manifest.load(evaluation_config.manifest_path)
    except FileNotFoundError as exc:
        print(f"[REFINE] {exc}")
        return 1
    if not manifest.records:
        print("[REFINE] no previews to evaluate; run the preview pipeline first")
        return 1

    print("[REFINE] evaluating the current build")
    baseline = evaluate(graph=graph, manifest=manifest,
                        config=evaluation_config, write=False)
    print(f"[REFINE] baseline {baseline.summary()}")

    planner_config = PlannerConfig(max_actions=int(setting("max_actions", 12)))
    if args.only:
        planner_config.allowed_types = tuple(args.only)

    planner = Planner(graph, planner_config)
    plan = planner.plan(baseline)
    os.makedirs(output_dir, exist_ok=True)
    write_report(plan, os.path.join(output_dir, "planner_report.json"))

    print(f"[REFINE] {plan.summary()}")
    for action in plan.ordered:
        print(f"[REFINE]   {action.priority:.3f} [{action.type}] {action.summary}")
    for note in plan.notes:
        print(f"[REFINE] {note}")

    if args.dry_run:
        print(f"[REFINE] dry run; plan written to {output_dir}")
        return 0
    if not plan.ordered:
        return 0

    executor = PipelineExecutor(work_dir=work_dir, base_dir=base)
    optimizer_config = OptimizerConfig(
        stopping=StoppingPolicy(
            target_score=float(setting("target_score", 1.0)
                               if args.target is None else args.target),
            max_iterations=int(setting("max_iterations", 8)),
            epsilon=float(setting("epsilon", 0.002)),
            max_consecutive_rejections=int(
                settings.get("max_consecutive_rejections", 3)),
        ),
        output_dir=output_dir,
        write_graph=args.write_graph or bool(settings.get("write_graph", False)),
        graph_path=evaluation_config.graph_path,
    )
    result = refine(graph, baseline, executor, output_dir,
                    planner_config=planner_config,
                    optimizer_config=optimizer_config)

    print(f"[REFINE] {result.summary()}")
    for note in result.notes:
        print(f"[REFINE] {note}")
    print(f"[REFINE] documents: {output_dir}")
    return 0


def _project_dir() -> str:
    return os.path.dirname(_modules_dir())


def _resolve(base: str, configured, default: str) -> str:
    path = str(configured or default)
    return path if os.path.isabs(path) else os.path.join(base, path)


def _load_graph(path: str):
    from vision.schema import SceneGraph

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"scene graph not found at {path}; run the vision pipeline first"
        )
    return SceneGraph.load(path)


if __name__ == "__main__":
    sys.exit(main())
