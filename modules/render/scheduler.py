"""
ArchX3D — Render scheduling
===========================
Decides *how* a set of preview renders is executed, without knowing anything
about Blender.

The abstraction, and why it earns its keep
------------------------------------------
Rendering ten viewpoints can be done ten ways: one Blender process each, one
process for all ten, four processes of two or three, or ten machines on a farm.
Which is fastest depends on facts the caller knows and the renderer does not —
how many cores are free, whether a GPU is shared, whether the ``.blend`` is on
a network share.

The expensive constant is process startup. Blender takes ~3 s to start, load a
furnished scene and compile its shaders, against ~250 ms to render a 640x360
EEVEE frame once it has. So the default is not "one task per worker" but **one
process per batch, many tasks per batch** — the scheduler's real job is
deciding how tasks are grouped, and only then how the groups run.

That is why ``run`` takes batches and an executor callable rather than
individual tasks: a future process pool or render farm changes how batches are
dispatched without changing what a batch is, and a test can substitute a fake
executor and assert on scheduling behaviour with no Blender anywhere.

Failure isolation
-----------------
An executor that raises fails its whole batch and nothing else — the remaining
batches still run, and every task in the failed batch gets a ``RenderOutcome``
carrying the error. A single bad camera must not cost you the other nine
previews, and a silent gap in the manifest would be worse than a recorded
failure.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


@dataclass
class RenderTask:
    """One preview to produce: a camera, an output path, and its cache key.

    Deliberately a plain data record with no behaviour — it is serialised into
    the job file handed to Blender, and will one day be serialised onto a wire
    for a farm.
    """

    viewpoint_id: str
    room_id: str
    #: Absolute path of the PNG to write.
    output: str
    width: int
    height: int
    #: The stored ViewPoint, as ``ViewPoint.to_dict()``. Carried so the render
    #: process can rebuild the exact camera without loading the scene graph,
    #: and so a farm node needs only this record.
    viewpoint: Dict[str, Any] = field(default_factory=dict)

    #: Cache bookkeeping, passed through to the manifest.
    key: str = ""
    scene_hash: str = ""
    room_hash: str = ""
    camera_hash: str = ""
    source_image: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewpoint_id": self.viewpoint_id,
            "room_id": self.room_id,
            "output": self.output,
            "width": self.width,
            "height": self.height,
            "viewpoint": self.viewpoint,
        }


@dataclass
class RenderOutcome:
    """What actually happened to one task."""

    viewpoint_id: str
    ok: bool = False
    render_ms: int = 0
    error: str = ""
    #: ``blend`` or ``graph`` — see ``manifest.RenderRecord.camera_source``.
    camera_source: str = ""
    width: int = 0
    height: int = 0
    #: Auxiliary passes actually written, ``{pass_name: absolute path}``. A
    #: pass that failed is simply absent; the evaluation engine treats that as
    #: an unmeasured axis rather than as a zero.
    passes: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RenderOutcome":
        return RenderOutcome(
            viewpoint_id=str(d.get("viewpoint_id", "")),
            ok=bool(d.get("ok", False)),
            render_ms=int(d.get("render_ms", 0) or 0),
            error=str(d.get("error", "")),
            camera_source=str(d.get("camera_source", "")),
            width=int(d.get("width", 0) or 0),
            height=int(d.get("height", 0) or 0),
            passes={str(k): str(v) for k, v in (d.get("passes") or {}).items()},
        )


#: A batch is the unit of execution: one Blender process renders all of it.
Batch = List[RenderTask]

#: Anything that can turn a batch into outcomes. ``renderer.SubprocessRenderer``
#: is the production one; tests pass a lambda.
Executor = Callable[[Batch], List[RenderOutcome]]


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def partition(
    tasks: Sequence[RenderTask],
    workers: int = 1,
    group_by_room: bool = False,
    max_per_batch: Optional[int] = None,
) -> List[Batch]:
    """Group tasks into batches, one Blender process each.

    ``workers``
        How many batches to aim for. With the default of 1 every task shares a
        single process, which is the fastest option on one machine because
        loading the scene and compiling its shaders dominates — and each extra
        worker pays that cost again.
    ``group_by_room``
        Keep each room's viewpoints together. Costs nothing on a sequential
        run and makes a failure legible ("the kitchen batch died"); a farm
        would also want it, since a room is the natural unit of work.
    ``max_per_batch``
        Ceiling on batch size, for a farm with a per-job time limit.

    Tasks keep their input order within a batch so a run is reproducible.
    """
    if not tasks:
        return []

    workers = max(1, int(workers))

    if group_by_room:
        by_room: Dict[str, Batch] = {}
        for task in tasks:
            by_room.setdefault(task.room_id, []).append(task)
        groups = [by_room[room] for room in sorted(by_room)]
    else:
        groups = [list(tasks)]

    # Split the groups round-robin until there are enough for every worker.
    # Round-robin rather than contiguous chunks because render cost varies far
    # more between rooms than within one, so interleaving balances better.
    batches: List[Batch] = []
    for group in groups:
        share = max(1, min(workers, len(group)))
        if share == 1:
            batches.append(list(group))
        else:
            buckets: List[Batch] = [[] for _ in range(share)]
            for index, task in enumerate(group):
                buckets[index % share].append(task)
            batches.extend(b for b in buckets if b)

    if max_per_batch and max_per_batch > 0:
        capped: List[Batch] = []
        for batch in batches:
            for start in range(0, len(batch), max_per_batch):
                capped.append(batch[start:start + max_per_batch])
        batches = capped

    return batches


# ---------------------------------------------------------------------------
# Schedulers
# ---------------------------------------------------------------------------


class Scheduler(ABC):
    """Runs batches through an executor. The seam a render farm slots into."""

    #: Short name, recorded in the manifest so a run can be explained later.
    name = "scheduler"

    @abstractmethod
    def run(self, batches: Sequence[Batch], execute: Executor) -> List[RenderOutcome]:
        """Execute every batch, returning outcomes in task order.

        Implementations must not raise on a failing batch; see
        :func:`_isolated`.
        """

    def describe(self) -> Dict[str, Any]:
        return {"scheduler": self.name}


def _isolated(batch: Batch, execute: Executor) -> List[RenderOutcome]:
    """Run one batch, converting any exception into per-task failures.

    Also fills in outcomes for tasks the executor forgot to report, so the
    number of outcomes always matches the number of tasks and the manifest
    cannot silently lose a viewpoint.
    """
    try:
        produced = list(execute(batch) or [])
    except Exception as exc:  # noqa: BLE001 - a batch failure is data, not a crash
        return [
            RenderOutcome(viewpoint_id=t.viewpoint_id, ok=False, error=str(exc))
            for t in batch
        ]

    by_id = {o.viewpoint_id: o for o in produced}
    return [
        by_id.get(
            task.viewpoint_id,
            RenderOutcome(
                viewpoint_id=task.viewpoint_id,
                ok=False,
                error="renderer returned no outcome for this viewpoint",
            ),
        )
        for task in batch
    ]


class SequentialScheduler(Scheduler):
    """One batch after another. The default, and the reproducible one.

    With the default batching (everything in one batch) this is also the
    fastest single-machine strategy, because it starts Blender once.
    """

    name = "sequential"

    def run(self, batches: Sequence[Batch], execute: Executor) -> List[RenderOutcome]:
        outcomes: List[RenderOutcome] = []
        for batch in batches:
            outcomes.extend(_isolated(batch, execute))
        return outcomes


class ThreadedScheduler(Scheduler):
    """Batches concurrently, one thread each.

    Threads are the right primitive despite the GIL: each batch is an external
    Blender process, so the thread spends its life blocked in ``wait()``. It
    behaves like a process pool without paying to pickle anything, and the same
    ``run`` signature will accept a real pool or a farm dispatcher later.

    Worth knowing before turning it up: Blender processes are memory-hungry and
    contend for the same cores, so past two or three workers the wall-clock
    gain flattens and can reverse.
    """

    name = "threaded"

    def __init__(self, workers: int = 2) -> None:
        self.workers = max(1, int(workers))

    def run(self, batches: Sequence[Batch], execute: Executor) -> List[RenderOutcome]:
        if len(batches) <= 1:
            return SequentialScheduler().run(batches, execute)

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as pool:
            # Submitted in order and collected in order, so the outcome list is
            # deterministic regardless of which batch finishes first.
            futures = [pool.submit(_isolated, batch, execute) for batch in batches]
            outcomes: List[RenderOutcome] = []
            for future in futures:
                outcomes.extend(future.result())
        return outcomes

    def describe(self) -> Dict[str, Any]:
        return {"scheduler": self.name, "workers": self.workers}


def make_scheduler(name: str = "sequential", workers: int = 1) -> Scheduler:
    """Build a scheduler from configuration.

    Unknown names fall back to sequential rather than raising: a typo in
    ``config.json`` should cost speed, not the run.
    """
    if str(name).lower() in ("threaded", "parallel", "pool") and workers > 1:
        return ThreadedScheduler(workers=workers)
    return SequentialScheduler()


def default_workers(requested: int = 0) -> int:
    """How many batches to run at once when the caller did not say.

    Zero means "decide for me": half the cores, capped at four, because each
    worker is a whole Blender process and the box still has to be usable.
    """
    if requested and requested > 0:
        return int(requested)
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))
