# ArchX3D — Engineering principles (v1.0)

The rules that do not change when the roadmap does.

Everything else in this documentation set — the architecture, the schema, the
plugin contract, the API — is a *consequence*. Consequences get revised. These
do not, and a change to this document is a change to what ArchX3D is.

Twelve principles. Each states the rule, the reason, and what it forbids. A
principle that forbids nothing is a slogan.

---

## Contents

1. [Measure, do not predict](#1-measure-do-not-predict)
2. [A missing thing beats an invented thing](#2-a-missing-thing-beats-an-invented-thing)
3. [Determinism is a feature, not an accident](#3-determinism-is-a-feature-not-an-accident)
4. [Everything asserted carries its provenance](#4-everything-asserted-carries-its-provenance)
5. [Unmeasured is not zero](#5-unmeasured-is-not-zero)
6. [One writer, one vocabulary](#6-one-writer-one-vocabulary)
7. [The core knows nothing about the backend](#7-the-core-knows-nothing-about-the-backend)
8. [Degrade at the seams, fail inside them](#8-degrade-at-the-seams-fail-inside-them)
9. [The user's stated ground truth outranks the machine](#9-the-users-stated-ground-truth-outranks-the-machine)
10. [Cost is a design parameter](#10-cost-is-a-design-parameter)
11. [Make the seam before you need it, not the abstraction](#11-make-the-seam-before-you-need-it-not-the-abstraction)
12. [Documentation is part of the change](#12-documentation-is-part-of-the-change)

---

## 1. Measure, do not predict

**Rule.** No automatic change to a reconstruction is kept on the strength of an
estimate. It is applied, re-rendered, re-scored, and kept only if the score
actually rose. An estimate may order the work; it may never authorise it.

**Reason.** The system's central claim is that its output resembles a
photograph. That claim is checkable. Any subsystem allowed to assert an
improvement it did not measure converts a checkable claim into an unfalsifiable
one, and the whole evaluation apparatus becomes decoration.

This is already how `optimizer.optimizer` works, and it is the reason the loop
is safe to run unattended: the worst case for a bad estimate is a wasted
iteration, not a degraded reconstruction.

**Forbids.**
- Applying a planner action without a post-hoc score comparison.
- "Confidence-weighted acceptance" that skips the render on high confidence.
- Any heuristic that writes to a scene graph and reports success from its own
  parameters rather than from a measurement.
- Reporting an *expected* gain anywhere a reader could mistake it for an
  achieved one. Where both appear they are labelled and adjacent.

**Escape hatch.** A change explicitly requested by a human is not a prediction;
see principle 9. It is applied because they asked, and recorded as such.

---

## 2. A missing thing beats an invented thing

**Rule.** When the evidence for an entity is below the accept threshold, the
entity is recorded and withheld — never built, never silently dropped. The
default is omission.

**Reason.** ArchX3D reconstructs real interiors. A sofa that exists and is
absent from the model is a gap the user can see and fill. A sofa that does not
exist and is present in the model is a lie the user has to detect first. The
first failure is legible; the second is corrosive, and at scale it destroys the
only thing that makes the output worth anything — that it is *about* a
particular room.

`ConfidencePolicy` encodes this with three bands, and the middle band's default
is "keep the record, do not build it". That default does not get flipped
because a demo looks sparse.

**Forbids.**
- Filling empty floor area with plausible furniture.
- Substituting a category prior for an unobserved dimension without flagging
  the record as derived.
- Discarding a low-confidence detection without keeping it in diagnostics. What
  was thrown away is part of the output; the review step exists to show it.
- Raising a confidence value to clear a threshold. Thresholds move by argument
  and measurement, in one place, with a changelog entry.

---

## 3. Determinism is a feature, not an accident

**Rule.** Given identical inputs, every non-model stage produces byte-identical
outputs. Every ordering is total. No dictionary iteration order, wall-clock
time, PID, hostname, or unseeded random reaches an artefact.

**Reason.** Three separate capabilities depend on this and all three collapse
without it:

- **Caching.** The render cache keys on input digests. Non-determinism means a
  cache that never hits, and the difference between a five-second iteration and
  a five-minute one.
- **Regression testing.** An evaluation is a baseline only if re-running it on
  an unchanged scene reproduces it. Otherwise every diff is noise.
- **Reproducible research.** A published number that cannot be regenerated is
  not a result.

Model calls are the exception, and they are quarantined behind a
content-addressed cache and a record/replay fixture layer precisely so that
*everything downstream of them* stays deterministic. See principle 4.

**Forbids.**
- `set` iteration reaching an output without `sorted()`.
- Ranking on a single key where ties are possible. Tie-breaks continue until
  the order is total — `planner.action_graph.sort_key` ends on the action id
  for exactly this reason.
- `time.time()` in a hash input, a filename, or an id.
- Parallelism that changes results. Concurrency may change *speed*; a scheduler
  that reorders output is a bug, not a tuning knob.
- Floating-point accumulation whose order depends on thread completion.

---

## 4. Everything asserted carries its provenance

**Rule.** Every value in a scene graph can answer: who produced me, from what
evidence, how sure were they, and when. A value that cannot is a defect.

**Reason.** ArchX3D fuses observation, inference, prior, and human decision into
one document, and downstream consumers weigh them differently. An optimiser must
not "correct" a dimension a person typed. An evaluation must not cite a
style-prior palette as evidence of what a room looks like. A researcher must be
able to separate what the model saw from what the catalogue assumed. None of
that is possible if the four sources are indistinguishable once written.

The existing `source: "observed" | "style_prior" | "inferred" | "default"`
fields, `source_images`, `observation_count`, `flags`, and the graph-level
`provenance` block are all instances of this principle. v2 generalises them into
one provenance record per component rather than ad-hoc fields per type.

**Forbids.**
- A default value written into the graph indistinguishably from a measured one.
- A model response entering the graph directly. Model output becomes an
  **Observation** with a provenance record; a deterministic fusion step turns
  observations into graph operations. The graph never holds raw generation.
- Dropping provenance on serialisation "to keep the file small". Provenance is
  the file's reason to exist.

---

## 5. Unmeasured is not zero

**Rule.** A quantity that could not be measured reports `measured: false` and is
excluded from normalisation. It is never scored zero, never imputed, and never
quietly omitted from the denominator without saying so.

**Reason.** Scoring an unmeasured axis zero asserts "the reconstruction is wrong
here", which is a different and false claim from "this was not assessed".
Silently dropping it asserts a completeness the run does not have. The honest
form carries the cost in a confidence figure and a `weight_used` fraction, so a
0.9 over two axes cannot masquerade as a 0.9 over five.

`AxisScore.unmeasured` and `ScoreSet.weight_used` implement this. The principle
generalises: it applies to coverage statistics, to per-room scores where a room
had no photograph, and to any aggregate anywhere in the system.

**Forbids.**
- `score = score or 0.0`.
- Averaging over a denominator that includes absent terms.
- A summary number without the count and coverage it was computed over.

---

## 6. One writer, one vocabulary

**Rule.** There is exactly one way to change a scene graph: emit a typed
**Operation** and commit it through the store. Every mutating actor — the review
editor, the optimiser, a plugin, a collaborative client, the CLI, a migration —
uses the same operation vocabulary, the same validation, and the same journal.

**Reason.** v1 has three independent mutation paths that do overlapping work:
`vision.review.apply_edits` (deep-copy, per-key validation, `EditReport`),
`optimizer.mutations.apply` (in-place, per-action handlers, `MutationResult`),
and `web/lib/editor.ts` (immutable document, client-side clamping, its own undo
stack). Each has its own idea of what a legal position is, its own undo model,
and its own audit format. A tolerance fixed in one does not reach the others,
and a rule the optimiser respects — never move a `locked` object — is enforced
by code the editor does not call.

That is not three features. It is one feature, implemented three times, drifting.

**Forbids.**
- A new mutation path. If a subsystem needs a change the operation vocabulary
  cannot express, the answer is a new operation type with an inverse, a
  validator, and a test — not a bespoke writer.
- Direct field assignment on a loaded graph outside the store's transaction
  boundary.
- Client-side validation that is not generated from, or verified against, the
  server's rules. Divergence here is a security bug as well as a correctness one.
- Two audit formats for the same event.

---

## 7. The core knows nothing about the backend

**Rule.** `archx3d.core` and `archx3d.scene` do not import Blender, bpy, numpy,
PIL, an HTTP client, a database driver, or any model SDK. They depend on the
standard library. Everything else depends on them.

**Reason.** Two hard constraints and one soft one.

The hard ones: the scene model has to load inside Blender's bundled Python,
which has no third-party packages, and it has to load inside a browser-adjacent
toolchain via a generated TypeScript mirror. Both are boundaries a dependency
cannot cross.

The soft one, which matters more over five years: a core that can import a
renderer will, eventually, and then the scene graph cannot be loaded without a
GPU, and the test suite takes minutes instead of milliseconds, and a research
user who wants only the schema installs 800 MB.

v2 hardens this further by removing ArchX3D code from Blender entirely — the
generator receives a **BuildPlan**, a flat versioned document, and interprets it
without importing anything of ours. See `ARCHITECTURE.md` §6.

**Forbids.**
- Any import in `core`/`scene` outside the standard library. Enforced in CI by
  an import-linter contract, not by review.
- A rendering concept (sample count, denoiser, colour management) in the scene
  schema. Those belong to a render request.
- A core type whose `to_dict` produces something only one backend can consume.

---

## 8. Degrade at the seams, fail inside them

**Rule.** A stage boundary may degrade: vision fails, the shell still builds;
evaluation fails, the build is still a build. *Inside* a stage, an unexpected
state raises. There is no third behaviour.

**Reason.** Degradation is valuable exactly where a partial result is still
useful to a user, and the pipeline's stage boundaries are where that is true —
this is why `main.py` marks the vision, video, evaluation and refinement steps
`critical=False` and the DXF and Blender steps critical. Degradation *within* a
stage is different: it produces a half-computed artefact that looks whole, and
the failure surfaces three stages later as a wrong number.

The distinction has to be explicit, because "log a warning and continue" is
always the locally easier option and always the globally wrong one.

**Forbids.**
- `except Exception: pass`. Ever.
- A bare `except Exception` anywhere except a designated stage boundary, where
  it must record the error into the run's diagnostics and mark the stage
  degraded — not merely log it.
- Returning a default from an internal function that could not compute its
  result. Raise, or return an explicit "not computed" value the caller must
  handle (principle 5).
- A degraded run reporting success. Degradation is a distinct terminal state
  with its own exit code and its own field in the job record.

**Note on model output.** Coercing malformed model output is not degradation —
it is parsing, and it belongs in the parser. `schema._f` turning `"1.2m"` into
`1.2` is correct; a fusion step turning an unparseable dimension into a category
default without flagging it is not.

---

## 9. The user's stated ground truth outranks the machine

**Rule.** A value a human set is not subject to automatic correction. It is
carried, respected, and — where an automatic process disagrees with it — the
disagreement is *reported*, never resolved in the machine's favour.

**Reason.** The review step's entire value is that the user's knowledge of the
room enters the system. A pipeline that then nudges their placement to satisfy a
collision heuristic has thrown away the one input it could not have generated.
`locked`, `confirmed_by_user`, `category_set_by_user`, and the `respect_user_edits`
flag on `recheck` are the existing expression of this; the pre-build check
"reports and does not correct" for the same reason.

**Forbids.**
- Automatic collision resolution moving a locked object.
- An optimiser action targeting a user-set field.
- A migration silently reinterpreting a user-set value.
- Discarding the lock on re-analysis. Re-running vision must merge into human
  decisions, not overwrite them.

**Corollary.** Because human decisions are privileged, they must be *visible*.
Any field a human can set is rendered as human-set in the review UI and in the
serialised graph. An invisible privilege is a trap.

---

## 10. Cost is a design parameter

**Rule.** Every expensive operation — a model call, a Blender launch, a render,
a full rebuild — is counted, attributed, cached, and bounded. A code path that
can call a paid API in a loop without a budget is unfinished.

**Reason.** ArchX3D is a system whose unit of work costs real money and real
minutes. A refinement run is tens of Blender rebuilds; a vision analysis is a
handful of frontier-model calls over high-resolution images. At one user this is
an annoyance. At a thousand concurrent projects it is the business.

Designing for cost late means retrofitting caches into code that assumed calls
were free, which is where correctness bugs breed. Designing for it early costs
one digest function and one counter.

**Forbids.**
- A model call without a content-addressed cache key.
- A loop over entities that can trigger a render, without an iteration budget
  and a stopping policy.
- A background job with no timeout.
- Reporting a run's outcome without its resource cost. Every job record carries
  model tokens, render seconds, and worker seconds.

---

## 11. Make the seam before you need it, not the abstraction

**Rule.** When a future variant is foreseeable, introduce the *boundary* — a
narrow interface, a data record that crosses it, one implementation behind it —
and stop. Do not build the second implementation, the registry, the
configuration surface, or the plugin loader until a real second case exists.

**Reason.** This is the discipline that separates a system that can grow from
one that is merely large. `render.scheduler` is the model: it takes batches and
an executor callable, has one sequential implementation, and a future process
pool or render farm changes how batches are dispatched without changing what a
batch is. That is a seam. It cost about forty lines and it makes distributed
rendering a new class rather than a rewrite.

The failure mode in the other direction is equally real: a plugin system with
one plugin, an abstract base class with one subclass, a strategy pattern where
the strategy never varies. These are not extensibility; they are indirection
with a cost and no benefit, and they make the code harder to change, not easier.

**Forbids.**
- An interface with one implementation *and* no identified second case. Name the
  second case in the docstring or delete the interface.
- Configuration for behaviour nobody has asked to vary.
- A generic mechanism introduced by the same change that introduces its first
  and only use, unless the second use is in the same release.

**Test.** Write down the second implementation's name. If you cannot, you are
building an abstraction, not a seam.

---

## 12. Documentation is part of the change

**Rule.** A change to observable behaviour is not complete until the document
that describes that behaviour is correct. Documentation lands in the same
commit, not the same sprint.

**Reason.** The existing `docs/` set is unusually good and unusually load-bearing
— it explains *why* the render cache under-invalidates, *why* rollback is
whole-state, *why* an axis can be unmeasured. That knowledge is not recoverable
from the code; a reader can see what the code does and not why it is allowed to
do it. Once one document is wrong, every document becomes suspect, and the
project loses the thing that lets a new contributor be useful in a day.

The bar is specific: explain the decision and the alternative rejected. "Uses a
snapshot for rollback" is a comment. "Uses a whole-state snapshot rather than
inverse operations, because a drifting inverse only shows up after a rejected
action, which is exactly when nobody is looking" is documentation.

**Forbids.**
- A merged PR whose behaviour contradicts a `docs/` file.
- A new module without a module docstring stating what it is for and what it
  refuses to do.
- A tuning constant without its unit and its justification.
- A "TODO: document" in a shipped release.

---

## How these interact

The principles are not independent, and where two of them pull in different
directions the resolution is fixed:

| Tension | Resolution |
| --- | --- |
| 1 (measure) vs 9 (human outranks) | Human wins. A human-set value is not measured against; it is the target. |
| 2 (omit rather than invent) vs product pressure for a full room | Omit. Sparse output is a product decision made at the presentation layer — offer to furnish explicitly, labelled as generated. Never in the reconstruction path. |
| 3 (determinism) vs 10 (cost, via parallelism) | Both. Parallelism must not change results; if it would, the parallel version is wrong. |
| 5 (unmeasured ≠ zero) vs a simple headline number | Both. The headline number exists and carries its coverage next to it. |
| 7 (pure core) vs 10 (performance) | Core stays pure. Optimised implementations live in an adjacent package the core does not import, selected at the boundary. |
| 11 (seam not abstraction) vs 6 (one vocabulary) | 6 wins where mutation is concerned. The operation vocabulary is not speculative generality; three writers already exist. |

---

## Amending this document

These principles change by pull request, with:

1. The principle being changed, quoted.
2. The concrete situation that made it wrong — not a hypothetical.
3. What breaks in the codebase if it changes, enumerated.
4. Sign-off from two maintainers.

A principle that has been amended keeps its history in the file. Knowing that
"unmeasured is not zero" was once "unmeasured scores zero" and why it changed is
worth more than a clean document.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the structure these principles produce.
- [`DESIGN_GUIDELINES.md`](DESIGN_GUIDELINES.md) — how to apply them in code.
- [`SCENE_GRAPH_SPEC.md`](SCENE_GRAPH_SPEC.md) — principles 2, 4, 6 and 9 made concrete.
- [`PERFORMANCE_GUIDE.md`](PERFORMANCE_GUIDE.md) — principle 10, and what principle 3 forbids optimising.
