"""
Tests for render scheduling.

No Blender anywhere: the scheduler's whole purpose is to be the seam between
"what to render" and "where it runs", so a fake executor is not a compromise
here — it is the interface being tested.

Two behaviours carry the design: batching (one process renders many
viewpoints, because process startup dominates) and failure isolation (one bad
camera must not cost the other nine previews).
"""

from __future__ import annotations

import threading

from render import scheduler as scheduler_mod
from render.scheduler import (
    RenderOutcome,
    RenderTask,
    SequentialScheduler,
    ThreadedScheduler,
    partition,
)


def task(viewpoint_id, room="room_a"):
    return RenderTask(
        viewpoint_id=viewpoint_id,
        room_id=room,
        output=f"/tmp/{room}/{viewpoint_id}.png",
        width=640,
        height=360,
    )


def succeed(batch):
    return [RenderOutcome(viewpoint_id=t.viewpoint_id, ok=True, render_ms=10) for t in batch]


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def test_everything_lands_in_one_batch_by_default():
    """Starting Blender costs ~3 s and rendering a frame ~250 ms, so the
    default must be one process for the lot."""
    tasks = [task(f"img_{i}") for i in range(5)]
    batches = partition(tasks)
    assert len(batches) == 1
    assert [t.viewpoint_id for t in batches[0]] == [t.viewpoint_id for t in tasks]


def test_no_tasks_means_no_batches():
    assert partition([]) == []


def test_workers_split_the_work():
    tasks = [task(f"img_{i}") for i in range(6)]
    batches = partition(tasks, workers=3)
    assert len(batches) == 3
    assert sum(len(b) for b in batches) == 6


def test_a_worker_count_above_the_task_count_does_not_make_empty_batches():
    batches = partition([task("img_1"), task("img_2")], workers=8)
    assert len(batches) == 2
    assert all(batches)


def test_grouping_by_room_keeps_a_room_together():
    tasks = [task("img_a1", "room_a"), task("img_b1", "room_b"), task("img_a2", "room_a")]
    batches = partition(tasks, group_by_room=True)

    rooms = [{t.room_id for t in batch} for batch in batches]
    assert rooms == [{"room_a"}, {"room_b"}]


def test_max_per_batch_caps_batch_size():
    """A farm with a per-job time limit needs an upper bound."""
    tasks = [task(f"img_{i}") for i in range(5)]
    batches = partition(tasks, max_per_batch=2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_batching_preserves_every_task():
    tasks = [task(f"img_{i}", room=f"room_{i % 2}") for i in range(7)]
    batches = partition(tasks, workers=2, group_by_room=True)
    scheduled = {t.viewpoint_id for batch in batches for t in batch}
    assert scheduled == {t.viewpoint_id for t in tasks}


# ---------------------------------------------------------------------------
# Sequential execution
# ---------------------------------------------------------------------------


def test_sequential_runs_every_batch_in_order():
    seen = []

    def execute(batch):
        seen.append([t.viewpoint_id for t in batch])
        return succeed(batch)

    batches = partition([task("img_1"), task("img_2"), task("img_3")], workers=3)
    outcomes = SequentialScheduler().run(batches, execute)

    assert len(seen) == 3
    assert [o.viewpoint_id for o in outcomes] == [b[0].viewpoint_id for b in batches]
    assert all(o.ok for o in outcomes)


def test_an_executor_that_raises_fails_only_its_own_batch():
    def execute(batch):
        if batch[0].viewpoint_id == "img_2":
            raise RuntimeError("blender exploded")
        return succeed(batch)

    batches = partition([task("img_1"), task("img_2"), task("img_3")], workers=3)
    outcomes = {o.viewpoint_id: o for o in SequentialScheduler().run(batches, execute)}

    assert outcomes["img_1"].ok and outcomes["img_3"].ok
    assert not outcomes["img_2"].ok
    assert "blender exploded" in outcomes["img_2"].error


def test_a_task_the_executor_forgot_is_reported_as_failed():
    """A silent gap in the manifest would be worse than a recorded failure."""
    def execute(batch):
        return [RenderOutcome(viewpoint_id=batch[0].viewpoint_id, ok=True)]

    outcomes = SequentialScheduler().run(
        partition([task("img_1"), task("img_2")]), execute
    )
    by_id = {o.viewpoint_id: o for o in outcomes}
    assert by_id["img_1"].ok
    assert not by_id["img_2"].ok
    assert "no outcome" in by_id["img_2"].error


def test_an_executor_returning_nothing_fails_the_whole_batch():
    outcomes = SequentialScheduler().run(partition([task("img_1")]), lambda batch: None)
    assert not outcomes[0].ok


# ---------------------------------------------------------------------------
# Threaded execution
# ---------------------------------------------------------------------------


def test_threaded_runs_batches_concurrently():
    """Each batch is an external process, so the threads really do overlap."""
    started = threading.Barrier(3, timeout=5)

    def execute(batch):
        started.wait()          # deadlocks unless three batches run at once
        return succeed(batch)

    batches = partition([task(f"img_{i}") for i in range(3)], workers=3)
    outcomes = ThreadedScheduler(workers=3).run(batches, execute)
    assert all(o.ok for o in outcomes)


def test_threaded_returns_outcomes_in_batch_order():
    """Deterministic output regardless of which batch finishes first."""
    import time

    def execute(batch):
        # The first batch is slowest, so completion order is not submit order.
        time.sleep(0.05 if batch[0].viewpoint_id == "img_0" else 0.0)
        return succeed(batch)

    batches = partition([task(f"img_{i}") for i in range(4)], workers=4)
    outcomes = ThreadedScheduler(workers=4).run(batches, execute)
    assert [o.viewpoint_id for o in outcomes] == ["img_0", "img_1", "img_2", "img_3"]


def test_threaded_isolates_a_failing_batch():
    def execute(batch):
        if batch[0].viewpoint_id == "img_1":
            raise RuntimeError("gpu lost")
        return succeed(batch)

    batches = partition([task(f"img_{i}") for i in range(3)], workers=3)
    outcomes = {o.viewpoint_id: o for o in ThreadedScheduler(workers=3).run(batches, execute)}
    assert not outcomes["img_1"].ok
    assert outcomes["img_0"].ok and outcomes["img_2"].ok


def test_threaded_degrades_to_sequential_for_a_single_batch():
    """Spinning up a pool to run one thing is pure overhead."""
    outcomes = ThreadedScheduler(workers=4).run(partition([task("img_1")]), succeed)
    assert len(outcomes) == 1 and outcomes[0].ok


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_make_scheduler_defaults_to_sequential():
    assert isinstance(scheduler_mod.make_scheduler(), SequentialScheduler)
    assert isinstance(scheduler_mod.make_scheduler("threaded", workers=1), SequentialScheduler)


def test_make_scheduler_builds_a_threaded_one_when_asked():
    built = scheduler_mod.make_scheduler("threaded", workers=3)
    assert isinstance(built, ThreadedScheduler)
    assert built.describe() == {"scheduler": "threaded", "workers": 3}


def test_an_unknown_scheduler_name_costs_speed_not_the_run():
    assert isinstance(scheduler_mod.make_scheduler("render-farm-9000", 4), SequentialScheduler)


def test_default_workers_is_bounded():
    assert scheduler_mod.default_workers(7) == 7
    assert 1 <= scheduler_mod.default_workers(0) <= 4
