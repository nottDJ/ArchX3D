# ArchX3D — Performance guide (v1.0)

Where the time goes, which bottlenecks are worth removing, in what order, for
what expected gain — and which parts of the system must stay slow.

```
  prototype ─► research ─► commercial ─► enterprise ─► cloud ─► large-scale
      │            │            │             │           │            │
   minutes      minutes      seconds       seconds     seconds    milliseconds
   per run     per room     per edit     per project   per user    per request
```

**Status.** The measurements in §2 are from the v1 codebase and are reproducible
with `archx3d bench`. The projections in §4–§7 are estimates with their reasoning
stated; each becomes a measurement when the work lands.

**The rule that governs this document.** Nothing here is optimised on
speculation. Every entry names what was measured, on what input, and what the
expected gain is. An optimisation without a before-and-after number is a
refactor with a marketing description.

---

## Contents

1. [The performance model](#1-the-performance-model)
2. [Where the time actually goes](#2-where-the-time-actually-goes)
3. [The ranked bottleneck list](#3-the-ranked-bottleneck-list)
4. [Tier 1 — the pipeline (highest ROI)](#4-tier-1--the-pipeline-highest-roi)
5. [Tier 2 — the scene graph](#5-tier-2--the-scene-graph)
6. [Tier 3 — rendering](#6-tier-3--rendering)
7. [Tier 4 — the frontend and the API](#7-tier-4--the-frontend-and-the-api)
8. [What should never be optimised](#8-what-should-never-be-optimised)
9. [The stage roadmap](#9-the-stage-roadmap)
10. [Budgets](#10-budgets)
11. [How to measure](#11-how-to-measure)
12. [Optimisation process](#12-optimisation-process)

---

## 1. The performance model

### 1.1 The three time scales

ArchX3D has three fundamentally different latency regimes, and conflating them
produces bad prioritisation.

| Scale | Operations | Target | Governed by |
| --- | --- | --- | --- |
| **Interactive** | select, drag, edit, undo, query, viewport frame | < 16 ms | data structures |
| **Responsive** | commit, validate, preview render, evaluate | < 2 s | algorithms + caching |
| **Batch** | analyse, generate, refine, import, export | minutes | parallelism + external tools |

A 30% improvement in a batch operation is worth less than a 30% improvement in an
interactive one, because the batch operation was always going to be started and
walked away from. **Optimise up the scale, not down it**: making a 4-minute
refinement into a 3-minute refinement changes nothing about how the product
feels; making a 400 ms edit into a 40 ms edit changes everything.

### 1.2 What ArchX3D actually spends time on

Measured over a representative `analyse → generate → evaluate → refine` run on the
`apartment` fixture (5 spaces, 12 photographs, 187 entities):

```
 Model calls (network)     ████████████████████████████████████  38%
 Blender (process + build) ██████████████████████████            27%
 Rendering                 ██████████████                        15%
 Subprocess + JSON I/O     ████████                               9%
 Evaluation (image maths)  ██████                                 6%
 Everything else           ████                                   5%
```

Two facts dominate every decision below:

1. **The two largest costs are external processes we do not control.** Optimising
   ArchX3D's Python does not touch 65% of the runtime. The leverage is in
   *avoiding* calls, *batching* them, and *parallelising* them — not in making
   the surrounding code faster.
2. **Both are perfectly cacheable.** A model response is a pure function of its
   inputs. A render is a pure function of the scene, the camera and the settings.
   The existing content-addressed caches are therefore worth more than any
   algorithmic work in the codebase, and protecting their correctness is a
   performance activity.

### 1.3 Amdahl, stated plainly

A refinement iteration is:

```
mutate graph   ~5 ms      0.02%
write .blend   ~2,000 ms  8%
Blender build  ~18,000 ms 72%
render 8 views ~2,000 ms  8%
evaluate       ~3,000 ms  12%
```

Making the mutation ten times faster saves 4.5 ms out of 25 seconds. Making the
Blender build unnecessary — by caching, by not rebuilding what did not change —
saves 18 seconds. **The entire performance story of this system is "do not do the
expensive thing", not "do the expensive thing faster."**

---

## 2. Where the time actually goes

Reproduce with `archx3d bench --suite all`. Hardware: 8-core CPU, RTX-class GPU,
NVMe, 32 GB. Fixtures defined in [`ARCHITECTURE.md` §20](ARCHITECTURE.md#20-testing-philosophy).

### 2.1 Pipeline stages — `apartment` fixture

| Stage | Cold | Warm cache | Dominated by |
| --- | --- | --- | --- |
| DXF extraction | 1.2 s | 1.2 s | `ezdxf` parse |
| Vision analysis | 94 s | 0.8 s | 12 model calls, sequential |
| Fusion + grounding | 3.1 s | 3.1 s | O(n²) pair comparisons |
| Blender generation | 47 s | 47 s | process start + scene build + export |
| Preview render, 8 views | 24 s | 0.02 s | Blender start (3 s) + 8 × 0.25 s |
| Evaluation | 8.4 s | 8.4 s | image loading and per-pixel maths |
| Planning | 0.3 s | 0.3 s | — |
| **Refinement, 8 iterations** | **206 s** | **151 s** | 8 × (rebuild + render + evaluate) |
| **Full run** | **~384 s** | **~212 s** | |

The warm-cache row for preview render — 24 s to 0.02 s — is the single most
important number in this document. It is what the three-digest cache already
buys, and every design decision that protects it is worth more than any
micro-optimisation.

### 2.2 Scene graph operations — `tower` fixture (110,000 entities)

| Operation | v1 measured | v2 budget | Ratio |
| --- | --- | --- | --- |
| Load document | 44 s | 50 ms (lazy) | **880×** |
| `object_by_id` | 4.8 ms | 8 µs | **600×** |
| Objects in a space | 51 ms | 0.9 ms | **57×** |
| Frustum query | — | 4 ms | new capability |
| `validate_graph` | ~38 min (O(n²)) | 1.8 s | **1,250×** |
| Apply one mutation | 4.9 ms | 0.6 ms | 8× |
| Snapshot | 1,510 ms | 420 ms | 3.6× |
| Undo | 1,480 ms | 3 ms | **490×** |
| Save | 2,100 ms | 12 ms (commit) | **175×** |
| Peak memory | 1.63 GB | 48 MB (lazy) | **34×** |

`validate_graph` is not merely slow — at this scale it does not finish. The
offending line:

```python
if obj.support_id and obj.support_id not in seen_ids | {o.id for o in graph.objects}:
```

builds a full set of every object id **inside a loop over every object**. At
110,000 entities that is 1.2 × 10¹⁰ set insertions.

### 2.3 The frontend

| Operation | Current (187 entities) | At 100k, naive | v2 budget |
| --- | --- | --- | --- |
| Initial load | 340 ms | ~180 s | < 2 s |
| Viewport frame | 4 ms | ~800 ms | < 16 ms |
| Select | 2 ms | ~120 ms | < 5 ms |
| Drag frame | 6 ms | ~200 ms | < 16 ms |
| Undo | 3 ms | ~90 ms | < 5 ms |
| Apply edits round trip | 420 ms | ~40 s | < 100 ms |
| Peak heap | 42 MB | ~3.2 GB | < 400 MB |

The "naive at 100k" column is extrapolated, not measured, and is included to show
which operations degrade linearly (acceptable) versus superlinearly (fatal).

---

## 3. The ranked bottleneck list

Ranked by **ROI = (time saved × frequency) / effort**, not by size of the number.

| # | Bottleneck | Where | Effort | Expected gain | Tier |
| --- | --- | --- | --- | --- | --- |
| 1 | Sequential model calls | `vision/pipeline` | S | **8–10× on analysis** | 1 |
| 2 | Blender rebuild every refinement iteration | `optimizer/pipeline` | M | **4–6× on refinement** | 1 |
| 3 | Subprocess + JSON per pipeline stage | `main.py` | M | 1.4× overall; unlocks everything else | 1 |
| 4 | Blender process start per operation | generation, render | S | 3–6× on multi-render | 1 |
| 5 | O(n) id lookup → O(n²) loops | `vision/schema` | S | **600× at scale** | 2 |
| 6 | Whole-document load / save / snapshot | `vision/schema`, `rollback` | L | 175–880× at scale | 2 |
| 7 | `validate_graph` quadratic membership test | `vision/schema` | XS | **1,250× at scale** | 2 |
| 8 | O(n²) fusion pair comparison | `vision/fusion` | M | 20× at 1,000 detections | 2 |
| 9 | Per-pixel Python in evaluation | `evaluation/imaging` | S | 5–15× on evaluation | 3 |
| 10 | No render batching across viewpoints | `render/scheduler` | S | 2–3× on multi-view | 3 |
| 11 | Single-threaded preview scheduler | `render/scheduler` | M | 3–5× on multi-core | 3 |
| 12 | GLB not compressed or LOD'd | `blender_generator` | M | 5–10× on web load | 4 |
| 13 | No frontend virtualisation or instancing | `web/` | L | 20–50× at scale | 4 |
| 14 | Whole-document undo stack | `web/lib/editor` | M | 30× on undo | 4 |
| 15 | Full review payload rebuild per edit | `project_api` | S | 5× on edit round trip | 4 |

Effort: XS < 1 day · S 1–3 days · M 1–2 weeks · L > 2 weeks.

**Note the shape of this list.** The three highest-ROI items are all "stop doing
the expensive thing" and none of them is an algorithm. #7 is a one-line fix with
a 1,250× gain at scale. That is not typical, and it is worth doing first purely
because it is nearly free.

---

## 4. Tier 1 — the pipeline (highest ROI)

### 4.1 Parallelise model calls · **8–10×**

**Now.** `vision/pipeline._observe_images` processes images one at a time. Twelve
images × ~7.5 s each = 94 s, of which essentially all is network wait.

**Change.** Bounded-concurrency async fan-out; the images are independent by
construction, and fusion is a separate deterministic stage that consumes all
results.

```
12 images × 7.5 s sequential  = 94 s
12 images, concurrency 6      = ~16 s
```

**Why the gain is real.** These are I/O-bound calls to a hosted service. The GIL
is irrelevant, the provider supports concurrent requests, and the only ceiling is
the rate limit — which is a configuration value, not a constant.

**Why it is safe.** Determinism is preserved because fusion sorts its inputs by
image id before reconciling. Completion order does not reach any output. This is
verified by a determinism test that shuffles completion order and asserts
identical output.

**Risks.** Rate limits (handled by the semaphore and `Retry-After`), and cost
concentration (a burst of twelve calls hits a per-minute budget faster) — which is
why the budget check is per-job, not per-call.

### 4.2 Avoid the full Blender rebuild per refinement iteration · **4–6×**

**Now.** Every accepted or rejected action triggers a full regeneration: reload
the graph, rebuild all geometry, rebuild all materials, rebuild the lighting rig,
re-export. 18 seconds, most of it recomputing geometry that did not change.

**Change.** Three levels, in increasing order of effort:

| Level | Mechanism | Gain |
| --- | --- | --- |
| **a. Skip unaffected work** | classify each action by what it invalidates; a `LIGHTING_ADJUSTMENT` needs no geometry rebuild | 2–3× |
| **b. Incremental BuildPlan** | diff the previous plan against the new one; send only the delta to a persistent backend process | 4–6× |
| **c. Persistent backend session** | keep one Blender process alive across the whole run, applying deltas | 5–8× |

The infrastructure already exists in outline: `ActionType.CAMERA_ONLY` already
identifies actions that need no rebuild at all, with the reasoning that *"the
preview renderer reconstructs cameras from the graph — which makes them an order
of magnitude cheaper than everything else."* Level (a) generalises that
classification from one action type to all eleven.

Level (b) requires the BuildPlan ([`ARCHITECTURE.md` §17](ARCHITECTURE.md#17-rendering-how-the-backend-becomes-irrelevant)),
which is being built for backend independence anyway. The performance gain is a
consequence, not the justification.

**Risk on (c).** A long-lived Blender process accumulates state and leaks. Bound
it: recycle every N operations or on memory threshold, and verify correctness by
periodically rendering the same frame in a fresh process and comparing. If they
diverge, the session is the bug and it recycles.

### 4.3 One process for the pipeline · **1.4× directly, and it unlocks the rest**

**Now.** `main.py` runs six `subprocess.run` calls. Each pays interpreter startup
(~200 ms), re-reads `geometry.json` and `scene_graph.json` from disk, and writes
its output back. The scene graph is parsed and serialised **five times** in one
run.

**Change.** Stage functions in `archx3d.pipelines`, called in-process, exchanging
objects. Blender remains a subprocess — it must be — but nothing else is.

```
6 × interpreter start        = 1.2 s
5 × parse+serialise graph    = 4.6 s  (apartment) / 210 s (tower)
─────────────────────────────────────
                        saved  5.8 s  (apartment)
```

1.4× on the apartment fixture is unremarkable. **The reason this is Tier 1 is
that it is a prerequisite for almost everything else**: incremental rebuild, a
persistent backend session, streaming progress, distributed execution and
in-memory caching are all impossible while every stage is a fresh process reading
JSON from a fixed path.

It also removes a correctness problem: `project_api._run_generation` copies files
into the repository root because `main.py` reads fixed paths, so two concurrent
generations corrupt each other.

### 4.4 Batch renders per Blender process · **3–6× on multi-render**

Blender takes ~3 s to start and load a furnished scene, against ~250 ms to render
a 640×360 Eevee frame. `render/scheduler`'s docstring already states the
conclusion — *"the default is not 'one task per worker' but one process per
batch, many tasks per batch"* — and the batching abstraction exists. What is
missing is that `config.json` ships `"scheduler": "sequential", "workers": 1`.

```
8 viewpoints, one process each : 8 × (3.0 + 0.25) = 26.0 s
8 viewpoints, one batch        : 3.0 + 8 × 0.25   =  5.0 s
8 viewpoints, 4 batches × 4 proc: 3.0 + 2 × 0.25  =  3.5 s
```

The work is turning on and validating what is already designed, plus a process
pool executor behind the existing `BatchExecutor` seam.

---

## 5. Tier 2 — the scene graph

These are the changes that convert "works at 40 objects" into "works at 100,000".
They are lower ROI *today* — nobody has a 100,000-entity scene yet — and they are
non-negotiable before anyone does.

### 5.1 Fix `validate_graph`'s quadratic membership test · **1,250× · one line**

```python
# Now — rebuilds the id set on every iteration
if obj.support_id and obj.support_id not in seen_ids | {o.id for o in graph.objects}:

# Fixed — build once
all_ids = {o.id for o in graph.objects}
...
if obj.support_id and obj.support_id not in all_ids:
```

Do this immediately, regardless of the v2 timeline. It is the cheapest fix in the
codebase and it is currently the difference between validation completing and not.

### 5.2 Index-backed lookups · **600×**

Replace every linear scan (`object_by_id`, `wall_by_id`, `room_by_id`,
`objects_in_room`, `viewpoints_for`) with hash and B-tree indexes
([`SCENE_GRAPH_SPEC.md` §11](SCENE_GRAPH_SPEC.md#11-indexes)).

The gain is not really 600× on one call — it is the removal of an entire class of
accidental O(n²), because these functions are called inside loops over the same
collections in `mutations`, `validate`, `recheck`, `review` and the generator.

### 5.3 Entity–component storage and partial loading · **34× memory, 880× load**

The structural change ([`SCENE_GRAPH_SPEC.md` §2](SCENE_GRAPH_SPEC.md#2-the-model-entities-and-components)).
Three separate wins:

- **Lazy loading**: open a scene without materialising it.
- **Column reads**: the viewport needs `transform` + `bounds`, ~80 bytes per
  entity, not the whole entity.
- **Slotted, frozen dataclasses**: no per-instance `__dict__`.

### 5.4 Journal-based undo · **490×**

Replace `rollback.take`'s `copy.deepcopy(graph.to_dict())` — 1.5 s and ~200 MB of
churn per action — with materialised operation inverses. Undo becomes proportional
to what changed, not to the scene.

Snapshots remain, as periodic checkpoints, which is what they are actually good
for. The correctness argument in `rollback`'s docstring is preserved by
materialising inverses at apply time rather than deriving them
([`SCENE_GRAPH_SPEC.md` §6.4](SCENE_GRAPH_SPEC.md#64-inverses-are-materialised-never-recomputed)).

### 5.5 Spatial hashing in fusion · **20× at 1,000 detections**

`vision/fusion` compares every detection against every other to decide whether
two observations describe one object. At 40 detections that is 780 comparisons
and invisible; at 1,000 it is 500,000.

Fix: hash detections into a spatial grid and compare only within neighbouring
cells. Two detections more than a few metres apart are never the same object, and
the comparison that proves it is wasted.

---

## 6. Tier 3 — rendering

### 6.1 Vectorise the evaluation's image maths · **5–15×**

`evaluation/imaging` does per-pixel work in Python for colour statistics, masked
means and difference overlays. On a 640×360 preview that is 230,400 iterations
per operation, several operations per axis, five axes, per viewpoint.

`numpy` is already a dependency of the evaluation path. The change is mechanical
and the risk is low, with one caveat: **the numbers must not change**. Vectorised
accumulation has a different summation order, which can shift a score in the
fourth decimal place. That is enough to break a golden file and, more
importantly, to make a regression baseline drift.

Mitigation: use `float64` accumulators, round at the boundary as
`evaluation.schema` already does, and gate the change on the golden-file suite
reproducing identical rounded output.

### 6.2 Parallel render batches · **3–5× on multi-core**

The `BatchExecutor` seam exists. Add a process-pool implementation, set
`workers` from CPU count with a memory-aware cap (each Blender process holding a
furnished scene is 1–3 GB, which is the real limit, not cores).

Must not change results: the determinism test shuffles batch completion order and
asserts identical manifests.

### 6.3 Resolution and sample discipline · **already right, protect it**

Evaluation renders at 640×360 with 16 Eevee samples. That is correct: the
evaluation compares perceptual statistics over regions, not pixel detail, and
rendering at delivery resolution would cost 20× for no measurable change in any
axis score.

This is a decision that will be questioned by someone who sees a low-resolution
preview and assumes it is a limitation. It is not. Document the reasoning next to
the setting.

### 6.4 GPU and denoising

| Change | Gain | Caveat |
| --- | --- | --- |
| Cycles on GPU (OptiX/HIP) | 5–20× on Cycles | none for final renders |
| OptiX denoising | 4–8× (fewer samples for equal quality) | **not for evaluation renders** — a denoiser is a learned prior that alters colour and texture statistics, which is exactly what the material and colour axes measure |
| Eevee for evaluation | already the default | correct |
| Persistent data between renders | 1.5–2× | memory |

The denoiser caveat is important and easy to get wrong: a denoised evaluation
render would score *better* while being *less* faithful, which corrupts the
metric in the most damaging possible direction.

---

## 7. Tier 4 — the frontend and the API

### 7.1 Instanced rendering and LOD · **20–50× at scale**

100,000 entities as individual Three.js meshes is ~100,000 draw calls. Instancing
by asset key collapses identical objects into one call; screen-space LOD replaces
distant objects with impostors.

```
100k individual meshes    ~800 ms/frame
instanced by asset key    ~40 ms/frame
+ LOD + frustum cull      ~12 ms/frame
```

Interiors cull exceptionally well — a closed door removes an entire room — so the
room graph is worth using as an occlusion structure.

### 7.2 Journal-based undo in the client · **30×**

`web/lib/editor.ts` keeps up to 200 whole `EditorDoc` values. That is correct for
its current size and does not survive 100,000 entities. Operations and their
inverses are smaller, correct under collaboration, and identical to the server's
model.

### 7.3 Incremental review payload · **5×**

`project_api.apply_review_edits` rebuilds the entire review payload and re-runs
validation after every edit. At 187 entities that is ~420 ms; the edit itself is
~5 ms.

Fix: return the delta. The client already has the rest.

### 7.4 GLB delivery · **5–10×**

The `model.glb` in this repository is 14 MB. For web delivery:

| Technique | Reduction |
| --- | --- |
| Draco mesh compression | 4–8× |
| KTX2/Basis textures | 3–6× |
| Instancing (`EXT_mesh_gpu_instancing`) | scene-dependent, often large |
| Meshopt | 2–3×, faster decode than Draco |
| Per-level splitting | load one storey |

Draco plus KTX2 typically takes 14 MB to under 2 MB with no visible difference at
web viewing distance.

### 7.5 API

| Change | Gain |
| --- | --- |
| Direct-to-storage uploads (presigned) | removes API servers from the byte path entirely |
| Content-addressed artefacts + immutable caching | CDN hit rate near 1.0 |
| Cursor pagination | constant-time page N |
| Dataloader batching in GraphQL | removes N+1 |
| Streaming responses for large queries | bounded memory |

---

## 8. What should never be optimised

**The most important section in this document.** Each of these is deliberately
not the fastest option, and speeding it up is a regression that a benchmark will
report as an improvement.

### 8.1 The evaluation engine's determinism

The engine is *derived*: run it twice on the same inputs and you get the same
numbers. That property is what makes an evaluation usable as a regression
baseline, a research result and a cache key.

**Forbidden:** sampling instead of measuring; parallelism that changes float
accumulation order; approximate colour difference; caching a score against
anything other than an exact input digest; skipping an axis because it is usually
fine.

**If evaluation is too slow, run fewer viewpoints — not sloppier maths.** Fewer
viewpoints is a coverage reduction, reported honestly by `coverage` and
`weight_used`. Sloppier maths is a silent accuracy reduction.

### 8.2 Rollback and undo correctness

Restore must be exact. Not "close enough that a subsequent render looks the same"
— exact. `rollback`'s reasoning holds: a drifting undo produces a graph that is
*nearly* the previous one, and the discrepancy only surfaces after a rejected
action, which is precisely when nobody is examining the state.

**Forbidden:** partial restore; diff-based restore that skips "unchanged" fields
without proving they are unchanged; lazy restore that defers work; skipping the
snapshot for an action believed to be safe.

### 8.3 The confidence policy's conservatism

`ACCEPT = 0.65`, `REVIEW = 0.40`, and the default that the uncertain band keeps
the record without building it. Admitting more detections would make scenes look
fuller and would be faster than a second review pass.

**Forbidden:** lowering a threshold for throughput; skipping validation on
high-confidence detections; batching that raises confidence by aggregation
without the corroboration actually being independent.

`evaluation.schema.merge` gets this right and is the model: corroboration raises
confidence *"but never to certainty: several views of one room share a light rig,
so they are not independent."*

### 8.4 The render cache's conservative invalidation

Anything unattributable to a room folds into `scene_hash` and invalidates the
whole building. That is deliberately more invalidation than strictly necessary,
and the reasoning is explicit: *"a re-render costs a few hundred milliseconds
while a stale evaluation image costs a wrong similarity score."*

**Forbidden:** narrowing attribution to raise the hit rate; time-based
invalidation; trusting a `.blend` timestamp; a "probably unchanged" fast path.

The cache may be made *faster* — a better hash, a faster store, a distributed
tier. Its *correctness* is not a tuning parameter.

### 8.5 Provenance and audit

Every value carries where it came from. Every change is journalled with its
inverse. This costs storage and a little write latency.

**Forbidden:** dropping provenance to reduce document size; sampling the audit
log; compacting the journal above the retention horizon; omitting inverses for
"small" operations.

The journal *is* the audit log, the undo mechanism and the sync mechanism. Its
completeness is load-bearing three times over.

### 8.6 Input validation

Every operation is validated before it applies. Every model response is validated
before it is trusted. Every uploaded file is checked.

**Forbidden:** trusting a client because the UI validates too; skipping
validation on a "internal" path; sampling validation.

Validation is microseconds against operations that cost seconds. There is no
scenario in which skipping it is the right trade.

### 8.7 Error context

Structured errors with fields and a remedy cost more than a string.

**Forbidden:** stripping context in production; sampling error reports; dropping
stack traces.

A production failure you cannot diagnose costs hours of engineering time. The
allocation costs nanoseconds.

### 8.8 Readability of the domain layer

The vision, evaluation, planning and optimisation code is the part of ArchX3D
that expresses judgement, and it is the part contributors most need to
understand. It is not in any hot path — the hot paths are Blender, the network
and the scene store.

**Forbidden:** inlining for speed; removing an intermediate variable that names
a concept; replacing a clear loop with an unreadable comprehension; hand-rolling
something the standard library does — unless a profile shows that function in the
top ten and the comment says so.

### 8.9 The single-writer rule

Serialising scene mutation through one writer per document is not the fastest
possible design. It is what gives a total journal order, which is what makes
undo, history, collaboration and sync one mechanism instead of four.

**Forbidden:** fine-grained locking for throughput; lock-free component writes;
bypassing the transaction for a "simple" change.

Write throughput scales by *scene*, and scenes shard perfectly. There is no
throughput problem here to solve.

---

## 9. The stage roadmap

What "fast enough" means changes as the product does.

### Stage 1 — Prototype *(where v1 is)*

**Definition of fast enough:** a run completes.

| Metric | Target |
| --- | --- |
| Full pipeline | < 10 min |
| Scene size | < 100 entities |
| Users | 1 |

Optimise nothing. Every hour spent on performance here is an hour not spent
discovering whether the reconstruction is any good.

### Stage 2 — Research system

**Definition:** an experiment sweep finishes overnight.

| Metric | Target |
| --- | --- |
| Full pipeline | < 4 min |
| Scenes per night | > 200 |
| Reproducibility | byte-identical |

Priorities: #1 (parallel model calls), #3 (one process), #4 (render batching),
#7 (the quadratic fix). Determinism becomes a hard requirement here, not a
preference — a sweep whose results drift is a sweep that proves nothing.

### Stage 3 — Commercial product

**Definition:** an editing session feels immediate.

| Metric | Target |
| --- | --- |
| Edit round trip | < 100 ms |
| Preview after edit | < 2 s |
| Refinement iteration | < 8 s |
| Scene size | 5,000 entities |
| Concurrent users | 100 |

Priorities: #2 (incremental rebuild), #5 (indexes), #10/#11 (render
parallelism), #15 (incremental review), #12 (GLB delivery).

### Stage 4 — Enterprise platform

**Definition:** a real building loads and edits.

| Metric | Target |
| --- | --- |
| Open a 100k-entity scene | < 2 s |
| Query | < 10 ms |
| Viewport | 60 fps |
| Concurrent editors per scene | 10 |
| IFC import, 1 GB | < 5 min |

Priorities: #6 (entity–component + partial load), #13 (instancing/LOD),
#14 (journal undo), collaboration, streaming.

### Stage 5 — Cloud service

**Definition:** thousands of tenants, predictable cost.

| Metric | Target |
| --- | --- |
| API p99 | < 200 ms |
| Job queue wait p95 | < 30 s |
| Render cache hit rate | > 80% |
| Cost per reconstruction | < $0.50 |
| Availability | 99.9% |

Priorities: distributed cache, autoscaling, spot instances, model tiering,
per-tenant budgets. Note that the top cost levers are all *avoidance* — cache hit
rate, deduplication, right-sized workers — not raw speed.

### Stage 6 — Large-scale deployment

**Definition:** portfolio scale.

| Metric | Target |
| --- | --- |
| Scenes | 10⁷ |
| Entities under management | 10¹⁰ |
| Concurrent jobs | 10⁴ |
| API p99 | < 100 ms |
| Cost per reconstruction | < $0.10 |

Priorities: sharding, read replicas, regional deployment, tiered storage, journal
compaction at scale.

---

## 10. Budgets

CI-enforced. A change that exceeds a budget fails the build; raising a budget is a
reviewed decision with a justification.

### Interactive — must never regress

| Operation | Budget |
| --- | --- |
| Entity lookup | 10 µs |
| Component read | 20 µs |
| Query, 1k results | 5 ms |
| Frustum query, 100k | 5 ms |
| Apply operation | 100 µs |
| Validate transaction | 1 ms |
| Undo | 5 ms |
| Viewport frame | 16 ms |
| Selection change | 5 ms |

### Responsive

| Operation | Budget |
| --- | --- |
| Commit, 100 ops | 50 ms |
| Open scene, lazy | 200 ms |
| Open one level | 500 ms |
| Preview render, cached | 50 ms |
| Preview render, cold | 2 s |
| Evaluate one viewpoint | 1 s |
| API request p99 | 200 ms |

### Batch

| Operation | Budget |
| --- | --- |
| DXF import, 10k entities | 30 s |
| IFC import, 1 GB | 5 min |
| Vision analysis, 12 images | 30 s |
| Generation, 5k entities | 60 s |
| Refinement iteration | 8 s |
| glTF export, 100k entities | 2 min |

### Memory

| Context | Budget |
| --- | --- |
| Core + scene imported | 30 MB |
| Scene open, lazy, 100k | 50 MB |
| Scene open, full, 100k | 400 MB |
| Worker process, idle | 150 MB |
| Browser, 100k entities | 400 MB |

---

## 11. How to measure

### 11.1 Profile before you change anything

```bash
archx3d bench --suite scene --fixture tower --profile
py-spy record -o profile.svg -- archx3d refine --max-iterations 3
python -X importtime -c "import archx3d" 2>&1 | sort -k2 -rn | head -20
memray run --output mem.bin -m archx3d generate
```

An optimisation that was not preceded by a profile is a guess, and in this
codebase guesses point at Python code that accounts for 5% of the runtime.

### 11.2 Continuous benchmarking

Every benchmark runs nightly on pinned hardware. Results are stored, plotted and
alerted on:

- Regression > 10% on any budgeted operation → the build fails.
- Regression > 5% → a warning on the PR.
- Improvement > 20% → flagged for the release notes, and checked for correctness,
  because a large unexplained speedup usually means something stopped happening.

### 11.3 Production measurement

The metrics in [`ARCHITECTURE.md` §13](ARCHITECTURE.md#13-logging-telemetry-and-observability)
are the production view. The three that matter most for performance:

```
archx3d_render_cache_ratio{scope}      — the single most important number
archx3d_task_duration_seconds{class}   — where the fleet's time goes
archx3d_query_seconds{plan}            — with a query.scan event on a full scan
```

A `query.scan` event names the missing index. That is what converts a slow
production query into a work item rather than a mystery.

---

## 12. Optimisation process

1. **Measure.** Profile the real workload on a real fixture.
2. **Find the top three.** Not the thing you suspected.
3. **Check §8.** Is this something that must stay slow? Stop if so.
4. **Estimate ROI.** Time saved × frequency ÷ effort. Write the number down.
5. **Write the benchmark first.** It is the regression test.
6. **Change one thing.**
7. **Measure again.** Same fixture, same hardware, same command.
8. **Verify correctness.** Full suite, determinism suite, golden files.
9. **Document.** The commit message states before, after and the mechanism.
10. **Add the budget.** So it cannot silently regress.

### Rejecting an optimisation

Reject if any of these is true:

- The gain is under 10% and the code became harder to read.
- It changes a result, however slightly, without an explicit decision.
- It introduces non-determinism.
- It weakens validation, provenance, or the audit trail.
- It optimises something in §8.
- It is in a code path that is not in the profile's top ten.
- It cannot be measured.

The last one is the most common and the most important. A change nobody can
measure is a change nobody can defend, and it will be re-done differently in a
year by someone who also cannot measure it.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — caching, distributed execution, telemetry, testing tiers.
- [`SCENE_GRAPH_SPEC.md`](SCENE_GRAPH_SPEC.md) — §23 performance budgets and the structures that meet them.
- [`ENGINEERING_PRINCIPLES.md`](ENGINEERING_PRINCIPLES.md) — §3 determinism, §10 cost as a design parameter.
- [`DESIGN_GUIDELINES.md`](DESIGN_GUIDELINES.md) — §18 anti-patterns, several of which are performance bugs.
- [`ROADMAP.md`](ROADMAP.md) — which release delivers which tier.
