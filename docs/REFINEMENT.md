# ArchX3D — Planning & optimisation (v1.0)

Turns evaluation findings into ranked, explainable actions, executes them, and
keeps only the ones that measurably improve the reconstruction.

```
EvaluationResult ──► ActionPlan ──► apply ──► validate ──► render ──► evaluate
     (findings)      (planner)                 (optimiser)              │
                                                                        ▼
                                                            keep if better, else undo
```

**No model of any kind is called.** Every number is arithmetic over the
evaluation and the scene graph. **No DXF geometry is touched.** Walls, doors,
windows and locked objects are immutable, checked before and after every
action.

---

## 1. Why a planning layer

The optimiser must never consume raw findings. A finding is an *observation* —
"the render is 0.42 darker than the reference" — and an optimiser fed
observations has to re-derive intent at the moment it mutates a scene graph.

Concretely, an evaluation of one badly lit room produces three findings: the
render is darker, its light is warmer, its shadows are flatter. Handed over
individually they become three edits to the same `LightingEnvironment`, each
measured against the last, each partly undoing the previous one's effect on the
others — three iterations of ~40 s each to reach what one could have produced,
and a history that reads as an argument with itself.

The planner does that reasoning once, up front, where it can be inspected:

```
lighting too warm ─┐
room dark ─────────┼──► one LightingAdjustment, three trigger findings
shadows flat ──────┘
```

What comes out is a list of instructions with absolute values, provenance and
an execution order. The optimiser needs nothing else.

### A defect this phase had to fix first

The evaluation engine's own `Finding.key` was `axis|subsystem|scope`, so *any*
two lighting findings about one room merged into one before the planner ever
saw them — the example above was literally impossible. `Finding` now carries a
`code` (`exposure`, `warmth`, `contrast`, `displacement`, `missing`, …) and the
room, and the key is `axis|subsystem|code|room|scope`. Merging across
viewpoints of one room still works; merging away distinct facts no longer does.

---

## 2. Architecture

```
modules/
├── planner/
│   ├── findings.py       the boundary — the only reader of an EvaluationResult
│   ├── grouping.py       root causes, and the actions that answer them
│   ├── ranking.py        expected gain, cost, priority
│   ├── dependencies.py   ordering rules and contradictions
│   ├── action_graph.py   Action, ActionPlan, and the DAG they form
│   └── planner.py        orchestration + planner_report.json
└── optimizer/
    ├── optimizer.py      the loop
    ├── constraints.py    what may never be touched, and what must stay true
    ├── mutations.py      the only code that writes to a scene graph
    ├── rollback.py       whole-graph snapshots, restored in place
    ├── stopping.py       the four conditions a run ends on
    ├── history.py        optimization_history.json
    ├── metrics.py        metrics.json
    └── pipeline.py       the real executor, refine(), and the CLI
```

`mutations.py` and `pipeline.py` are additions to the specified file list.
Mutation is kept out of the loop because eleven kinds of graph surgery would
bury the control flow, and the surgery is where the mistakes live; `pipeline`
holds the Blender wiring so that nothing in the loop imports it.

### Output

```
output/refinement/
├── planner_report.json        what was proposed, dropped, and why
├── optimization_history.json  every attempt, including the rejected ones
└── metrics.json               trajectory, attribution, calibration
```

---

## 3. The planner

### findings.py — the boundary

Adapts `EvaluationResult` into a `FindingSet`: frozen copies, indexed by
subsystem, room, object and material, carrying the axis scores so gain can be
estimated against real headroom. Findings stop here; everything downstream
works in `Action` objects.

`PlannedFinding.key` is stable across rephrasing (built from what a finding is
*about*, not how it is worded), which is what lets yesterday's history be
compared with today's plan.

### grouping.py — root causes and synthesis

Findings share a root cause when they name the same **subsystem** and the same
**scope** (a room, object or material). Deliberately blunt, and blunt in the
safe direction: two wrongly grouped become one action that does slightly too
much and gets rolled back if it does not help; two wrongly separated cost an
extra iteration.

Synthesis reads the scene graph — **read-only** — because an action must say
"set ambient to 0.62", not "raise ambient", or the optimiser would need
judgement at the moment of mutation.

| Subsystem | Action |
|---|---|
| `LightingEnvironment`, `LightSource` | LightingAdjustment |
| `ColourPalette` | PaletteAdjustment |
| `SurfaceFinish`, `MaterialSpecies` | MaterialAdjustment |
| `SceneGraphTransform` | FurnitureTranslation (+ Rotation) |
| `AssetPlacement` | AssetReplacement / VariantSwap / ScaleRefinement |
| `ObjectDetection` | DecorDensity |
| `CameraFit` | CameraCorrection |
| `Geometry`, `RenderSettings` | **nothing** — named in the report as unactionable |

`RenderSettings` is unactionable on principle: an optimiser that tuned its own
scoring instrument would be doing the opposite of its job.

**Bounded, damped moves.** Every derived parameter is clamped, and most move
only 75% of the way toward what a measurement implies. A reading conflates the
thing measured with the conditions it was measured under — a photograph's
brightness includes its exposure, its colour its white balance — so the loop
moves most of the way, re-measures, and moves again. Under-shooting converges;
over-shooting oscillates.

**Where the harder action types come from.** Rotation has no pixel evidence —
a photograph and a render agreeing on where a chair *is* say nothing about
which way it faces — so it is triggered by an unsatisfied `faces` relationship
on an object already known to be misplaced, which the graph records
deterministically. A stand-in asset yields *two* mutually exclusive
hypotheses (wrong asset, or wrong proportions feeding the matcher), and
ranking picks one. `StyleRefinement` only ever adopts a style the rest of the
building already has, area-weighted — never an invented one.

### ranking.py — estimates that only order the queue

```
gain = evidence × efficacy_prior × axis_headroom
cost = base (1 cycle, or 0.15 for camera-only) + 0.03 per object
priority = gain × (1 − ½ risk) / (½ + cost)
```

Nothing here predicts the future. The optimiser measures the real gain, so an
estimate that turns out wrong costs one iteration and is recorded as such —
which lowers the stakes considerably: the estimate has to be roughly right
about *which* action is more promising, not about how much either achieves.

`axis_headroom` is what stops a severe finding about an axis already at 0.97
from outranking a modest one about an axis at 0.4. An axis the evaluation
could not measure has **zero** headroom, because an action nothing can verify
is an action whose outcome is unknowable.

Camera corrections cost ~0.15 cycles against 1.0 for everything else: they
need no Blender rebuild, which is roughly 3 s against 40 s.

### dependencies.py — order, and contradictions

Every rule names the code that makes it true. A rule that cannot point at a
mechanism is a superstition, and ordering superstitions serialise a plan that
could have run in any order.

| Before | After | Because |
|---|---|---|
| CameraCorrection | Translation / Rotation / Scale | displacement is measured *through* that camera |
| StyleRefinement | Material / Palette / AssetReplacement | `styles.resolve_material` re-derives materials from style |
| PaletteAdjustment | MaterialAdjustment | `MaterialLibrary.surface` tints a finish against the palette |
| AssetReplacement | FurnitureScale | a new asset brings its own proportions |
| DecorDensity | FurnitureTranslation | admitting objects changes what collision resolution will move |

Rules are stated between *types* and bind only where the actions share a
scope — otherwise every camera correction in a building would gate every
translation in it.

**Contradictions** drop the lower-ranked half of a pair and record why: two
ways to fix one asset, two actions of one type on one target. Cycles are
broken deterministically at the edge into the highest-ranked action, because
an unexecutable plan is worse than a slightly mis-ordered one, and no action is
ever dropped for being in one.

---

## 4. The optimiser

```
next ready action
      ↓
validate (pre)     ── forbidden? record and skip, no render spent
      ↓
snapshot + apply
      ↓
validate (post)    ── broke an invariant? roll back
      ↓
rebuild + render + evaluate
      ↓
comparable?        ── measured fewer axes? roll back
      ↓
gain > epsilon?    ── no ─► roll back, record why
      ↓ yes
accept, new baseline
```

### The measurement-integrity guard

The subtlest failure a self-verifying loop can have, and one this
implementation actually hit in testing: a camera correction moved a viewpoint
somewhere its previews failed, four of five axes went unmeasured, the score
normalised over the survivor and read **1.0000** — and the loop accepted it and
declared victory.

A score is a weighted mean over the axes that could be measured, so two scores
drawn from different axis sets are not comparable. The loop now rejects any
evaluation that measured *fewer* axes than the one before it, with the reason
recorded. Gaining an axis is fine: more of the picture is a better-informed
score, not a suspect one.

### Constraints

**Immutable** — an action naming one is rejected outright, before any render:

* DXF geometry: walls, openings, doors, windows, structural elements
* objects the user locked

**Invariants** — the graph must still satisfy these afterwards:

* every material exists in `catalog.MATERIALS`, and suits the surface it is on
  (carpet is a real material and a nonsensical wall finish)
* every style resolves in `catalog.STYLES`
* every object stays inside its room's bounds, with a 0.35 m tolerance — a
  room's bounds are drawn around a segmented polygon and furniture against a
  wall legitimately sits on the line

Checked **twice**: the pre-check catches an action that *asks* for something
forbidden; the post-check catches one that asked for something permitted and
produced something forbidden anyway. Only the second is impossible to
anticipate, and a violation rolls back *before the render*, because a preview
that looks better while breaking a rule would get accepted.

Immutability is proven against a digest taken beforehand, not inferred from the
current state.

### Rollback

Whole-graph snapshots, restored **in place** so every existing reference sees
the restored state. Not per-action inverse operations: an inverse that drifts
from its forward operation produces a graph that is *nearly* the previous one,
and the discrepancy only surfaces after a rejected action — precisely when
nobody is looking. A snapshot costs milliseconds against an iteration costing
tens of seconds, and is exactly correct by construction.

### Stopping

| Reason | Meaning |
|---|---|
| `target_reached` | the score met the goal — the only happy ending |
| `max_iterations` | budget spent; says nothing about quality, and the report notes whether the score was still rising |
| `no_gain` | 3 consecutive rejections — the plan is aimed at the wrong things |
| `below_epsilon` | the last 3 accepted actions gained less than epsilon in total |
| `plan_exhausted` | everything was attempted |

Three consecutive rejections rather than one, because the queue is ordered by
an *estimate*: the best-estimated action failing says little about the next.

A gain of exactly epsilon is **not** an improvement — the graph is simpler
without a change that achieved nothing, and an accepted no-op would make the
history claim something that did not happen.

---

## 5. Explainability

Every attempt records what the spec requires and a little more:

```json
{
  "action": "lighting_adjustment:room_a",
  "outcome": "accepted",
  "parameters": {"ambient": 0.906, "color_temperature_k": 3940.0},
  "expected_gain": 0.336, "actual_gain": 0.09, "estimate_error": 0.246,
  "score_before": 0.62, "score_after": 0.71,
  "axis_deltas": {"lighting": 0.18, "colour": -0.01},
  "trigger_findings": ["lighting|LightingEnvironment|exposure|room_a|", "…"],
  "trigger_summaries": ["Render is darker than the reference", "…"],
  "affected": {"rooms": ["room_a"], "objects": [], "materials": []},
  "changes": [{"subject": "room_a", "field": "lighting.ambient",
               "before": 0.5, "after": 0.906}],
  "rollback_reason": ""
}
```

**Rejections are the valuable half.** A history of accepted changes is a
changelog; the rejected entries are the only record of what the reconstruction
*cannot* be improved by, and they carry the same detail plus a sentence saying
why.

`metrics.json` adds the reading: which axes moved, which **regressed** (a
positive total can hide a trade nobody asked for), where the gain came from by
type and room, and the **calibration** — expected against actual, per action
type. The efficacy priors in `ranking.py` are stated guesses; this is the
evidence for revising them.

---

## 6. Using it

```bash
# see what a run would do — no rebuilds, no changes
python modules/optimizer/pipeline.py --dry-run

# run it
python modules/optimizer/pipeline.py --max-iterations 8
python modules/optimizer/pipeline.py --only lighting_adjustment material_adjustment
python modules/optimizer/pipeline.py --write-graph      # keep the result

# as a pipeline step (implies --evaluate)
python main.py plan.dxf --images reference_images/ --refine
```

```python
from planner import Planner
from optimizer.pipeline import PipelineExecutor, refine

plan = Planner(graph).plan(evaluation)      # inspect before running anything
result = refine(graph, evaluation, PipelineExecutor(work_dir, base_dir), out_dir)
```

**Working copies.** Every rebuild happens in a scratch directory
(`.cache/optimize` by default), so a run never overwrites the project's
`data/scene_graph.json` or `output/scene.blend`. The improved graph is written
back only with `--write-graph`, and only when the run actually gained
something.

**Budget.** An iteration is a Blender rebuild (~40 s) plus previews (~1.5 s per
viewpoint) plus evaluation (~50 ms). A camera-only action skips the rebuild and
takes about three seconds. Phase 2's per-room preview cache means an action
touching one room re-renders that room's viewpoints and reuses the rest.

---

## 7. Testing

```
tests/test_planner_findings.py     31 tests   the boundary, grouping, synthesis
tests/test_planner_ranking.py      29 tests   estimates, ordering, contradictions
tests/test_planner.py              18 tests   end to end, report, determinism
tests/test_optimizer_constraints.py 39 tests  guardrails and mutations
tests/test_optimizer_control.py    34 tests   rollback, stopping, history, metrics
tests/test_optimizer.py            29 tests   the loop
```

All run without Blender in under a second. The loop tests use an executor
whose scores are a **function of the graph**, not a canned sequence — a fake
returning scripted numbers would pass whether or not the loop actually applied
and reverted anything, whereas one that reads the graph can only be satisfied
by a loop that really mutated it and really put it back.

The properties pinned:

* the planner never mutates the scene graph, and produces the same plan twice
* DXF geometry and locked objects survive a whole run
* a forbidden action costs no render
* a change that breaks an invariant is rolled back *before* rendering
* a higher score over fewer axes is rejected as not comparable
* every attempt, accepted or not, records its triggers, both gains, and its
  reason

---

## 8. Limitations

* **The estimates are priors, not measurements.** They order the queue; the
  calibration section of `metrics.json` is the evidence for revising them, and
  nobody has yet.
* **Grouping is by subsystem and scope.** Two findings that share a cause but
  not a subsystem stay separate and cost an extra iteration.
* **One action at a time.** Interactions between changes are invisible: a
  lighting and a material change that only help *together* are each rejected.
  Measuring combinations would need a search rather than a queue.
* **Replanning is capped** at three plans per run, and each replan reuses the
  evaluation already computed to judge the last action.
* **The optimiser cannot create.** It admits detections the confidence policy
  withheld, but it never invents an object, a material or a style that nothing
  observed.
