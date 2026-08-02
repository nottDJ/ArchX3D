# ArchX3D — Design guidelines (v1.0)

How to write code that belongs in this codebase.

[`ENGINEERING_PRINCIPLES.md`](ENGINEERING_PRINCIPLES.md) says what ArchX3D
values. [`ARCHITECTURE.md`](ARCHITECTURE.md) says how it is structured. This
document is the day-to-day one: naming, module shape, types, comments, tests,
review, and the specific mistakes this system invites.

**Rule of thumb.** ArchX3D's existing code is unusually well documented and
unusually careful about *why*. Match it. A change that is correct but reads as
though it came from a different project makes the codebase worse.

---

## Contents

1. [The house style](#1-the-house-style)
2. [Module shape](#2-module-shape)
3. [Naming](#3-naming)
4. [Functions](#4-functions)
5. [Types](#5-types)
6. [Comments and docstrings](#6-comments-and-docstrings)
7. [Constants and tuning values](#7-constants-and-tuning-values)
8. [Errors](#8-errors)
9. [Numbers, units and floating point](#9-numbers-units-and-floating-point)
10. [Determinism in practice](#10-determinism-in-practice)
11. [Concurrency](#11-concurrency)
12. [Configuration and dependencies](#12-configuration-and-dependencies)
13. [Tests](#13-tests)
14. [TypeScript and the frontend](#14-typescript-and-the-frontend)
15. [Tooling](#15-tooling)
16. [Git and pull requests](#16-git-and-pull-requests)
17. [Review checklist](#17-review-checklist)
18. [Anti-patterns specific to this system](#18-anti-patterns-specific-to-this-system)

---

## 1. The house style

Four properties, visible in the existing code and worth naming so they survive
contact with new contributors.

### 1.1 Explain the decision, not the mechanism

```python
# Bad — restates the code
# Take a snapshot of the graph before applying.
snapshot = rollback.take(graph)

# Good — explains why this and not the obvious alternative
# Whole-state rather than an inverse per action: an inverse that drifts from its
# forward operation only shows up after a *rejected* action, which is exactly
# when nobody is looking.
snapshot = rollback.take(graph)
```

The second form is the standard. Every non-obvious decision in this codebase
carries its rejected alternative, and that is the single most valuable property
of the documentation.

### 1.2 Say what a module refuses to do

Every existing module docstring has a paragraph of the form "what this never
does". `evaluation`: *"The engine never modifies the scene graph."*
`optimizer`: *"No model of any kind is called."* `mutations`: *"It never
invents."*

These are not decoration. They are the contract a reader needs in order to reason
about the module without reading it, and they are what makes a violation
reviewable.

### 1.3 British spelling

`colour`, `optimise`, `normalise`, `behaviour`, `centre`, `analyse`. Consistent
throughout, including in identifiers (`ColourPalette`, `normalise_hex`,
`optimizer`). The one exception is the package name `optimizer`, which is already
established; do not "fix" it.

American spellings in third-party APIs are used as those APIs spell them.

### 1.4 Prose that a person reads

Docstrings are written for a human who is trying to decide whether this is the
code they need. Section headers, paragraphs, examples. Not a parameter list with
a verb glued to the front.

---

## 2. Module shape

### 2.1 Standard layout

```python
"""
ArchX3D — <what this is, in five words>
=======================================
<One paragraph: what problem this solves.>

<Section: the key design decision and the alternative rejected.>

What this never does
--------------------
<The contract. Two or three sentences.>
"""

from __future__ import annotations

import <stdlib>                        # 1. standard library

from archx3d.core import <…>            # 2. lower layers, in layer order
from archx3d.scene import <…>
from archx3d.ports import <…>

from . import <sibling>                 # 3. same package

#: Module constants, each with a unit and a justification.
MAX_ITERATIONS = 8

# ---------------------------------------------------------------------------
# Section
# ---------------------------------------------------------------------------

...
```

The `# ---` section separators are the existing convention and they work well in
files over 300 lines. Use them.

### 2.2 Size

| Lines | Action |
| --- | --- |
| < 400 | fine |
| 400–700 | fine if it is one cohesive concept |
| 700–1,000 | justify it in the docstring, or split |
| > 1,000 | split |

v1 has four files over 900 lines (`vision/review.py` at 1,328,
`vision/schema.py` at 1,146, `vision/grounding.py` at 1,013,
`planner/grouping.py` at 966). Each is doing several things. New code does not
add to that set.

Splitting is by **concern**, not by line count. `vision/review.py` splits into
"build the review payload" and "apply edits" because those are two directions of
one bridge, not because 1,328 is a large number.

### 2.3 Imports

- **Absolute for cross-package**, relative for same-package. `from archx3d.scene
  import ops` and `from . import scoring`.
- **No deferred imports to break a cycle.** v1 has several
  (`from planner.action_graph import ActionType` inside `mutations.apply`,
  `from vision import assets` inside `_asset`). They hide the cycle from tooling
  instead of removing it. If you need one, the dependency is wrong.
- **Deferred imports for genuinely optional dependencies are fine**, and must be
  wrapped in a helper that produces a clear error:

  ```python
  def _require_pillow():
      try:
          from PIL import Image
      except ImportError as exc:
          raise UnsupportedError(
              "image comparison needs Pillow", remedy="pip install 'archx3d[imaging]'"
          ) from exc
      return Image
  ```

- **No `import *`.** Anywhere.
- **No `sys.path` mutation.** A test asserts the source tree contains none.

### 2.4 Public surface

Every module declares `__all__`. Anything not in it is internal and may change
without notice. Names beginning `_` are private and are never imported across
module boundaries — v1's `mutations._score_for` calling `assets._proportion_score`
is exactly the coupling this prevents.

---

## 3. Naming

### 3.1 Conventions

| Kind | Style | Example |
| --- | --- | --- |
| Package, module | `lower_snake` | `archx3d.scene.ops` |
| Class, protocol | `PascalCase` | `RenderBackend`, `ColourPalette` |
| Function, method, variable | `lower_snake` | `compile_plan`, `room_hash` |
| Constant | `UPPER_SNAKE` | `MAX_DIMENSION`, `HASH_VERSION` |
| Type variable | `PascalCase`, short | `T`, `ComponentT` |
| Private | leading `_` | `_normalise` |
| Test | `test_<what>_<condition>` | `test_undo_restores_prior_digest` |

### 3.2 Domain vocabulary

The words below have one meaning in this codebase. Using them for anything else
is a defect, and inventing a synonym is worse.

| Term | Means | Not |
| --- | --- | --- |
| **scene** | the graph document | a rendered image |
| **space** | an enclosed area (`IfcSpace`) | `room` in new code; `Room` survives as a view alias |
| **level** | a storey, with an elevation and a plan frame | "level of detail" — that is `lod` |
| **entity** | an addressable thing in the scene | `object`, which is a Python word |
| **component** | typed data attached to an entity | a UI component — that is a `Panel` |
| **operation** | one atomic, invertible change | a "task", which is a unit of execution |
| **commit** | a validated batch of operations | a git commit, though the analogy is intended |
| **action** | a planned change, from a finding | an operation; an action *compiles to* operations |
| **finding** | one named, quantified, attributable difference | a score |
| **axis** | one dimension of evaluation | a coordinate axis — that is `x`/`y`/`z` |
| **viewpoint** | a stored camera fitted to a reference image | a camera in a render request |
| **observation** | what a model claimed about an image | scene state |
| **provider** | an adapter over an external model | a plugin, which may contain several |
| **backend** | an adapter over a renderer | a server |
| **artefact** | an immutable output blob | an intermediate |
| **task** | one unit of distributed work | a job, which is a DAG of tasks |
| **degraded** | completed, with a stage that could not run | failed |
| **unmeasured** | could not be assessed | zero |

### 3.3 Units in names

Any quantity whose unit is not obvious carries it:

```python
elevation_m      power_w      colour_temperature_k     duration_s
rotation_z       # degrees — stated in the docstring and consistent everywhere
vertical_fov_deg
```

`rotation_z` is the one that has bitten people: it is **degrees**, counter-
clockwise, with 0 meaning the object's front faces +Y. Any code converting to
radians does so at the boundary and names the local variable `theta`.

---

## 4. Functions

### 4.1 Shape

- **One thing.** If the docstring needs "and", split it.
- **Under 50 lines.** Over that, name the parts.
- **Under 5 parameters.** Over that, take a record.
- **Keyword-only for anything optional.** `def render(plan, *, samples=16)`. This
  is also what makes adding a parameter a minor rather than a major change.
- **No boolean positional arguments.** `evaluate(scene, True)` tells the reader
  nothing.
- **Return a value; do not mutate an argument** — unless mutation is the
  function's stated purpose, in which case say so in the name and the docstring.

### 4.2 Guard clauses

Early return over nesting. The existing mutation handlers do this well:

```python
def _translate(action, graph) -> MutationResult:
    obj = graph.object_by_id(action.target_id)
    if obj is None:
        return MutationResult(reason=f"{action.target_id} is not in the graph")

    dx = float(action.parameters.get("dx", 0.0))
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return MutationResult(reason="the planned move is zero")
    ...
```

Note the second guard: it returns a *reason*, not a silent no-op. The docstring
explains why — *"a silent no-op would be recorded as a change that achieved
nothing, which is a different and misleading claim."* That instinct generalises:
when a function declines to act, it says why.

### 4.3 Purity

Prefer functions that are pure. Where they cannot be, isolate the impurity:

```python
# Good: the decision is pure and testable; the writing is not and is trivial.
def decide_invalidations(manifest, hashes) -> list[str]: ...
def write_manifest(path, manifest) -> None: ...

# Bad: cannot test the decision without a filesystem.
def refresh_manifest(path, hashes) -> None: ...
```

This is why the optimiser's execution is injected and why its tests run in
milliseconds. Apply the same reasoning everywhere.

---

## 5. Types

### 5.1 Everything is annotated

Public functions, private functions, module constants, class attributes. `mypy
--strict` on `core`, `scene` and `ports`; `mypy` default elsewhere; both in CI.

```python
from __future__ import annotations   # first line of every module, always
```

### 5.2 Prefer precise types

| Instead of | Use |
| --- | --- |
| `dict` | `TypedDict`, or a dataclass |
| `str` for an id | `EntityId`, `CommitId` — `NewType` at minimum |
| `float` for a length | `Metres` where the confusion is real |
| `str` for a closed set | `Literal["floor", "wall", "ceiling", "on_object"]` |
| `Any` | a protocol, or a union, or genuinely `object` |
| `Optional[X]` returned on failure | an explicit result type, or raise |

`Any` is permitted in exactly two places: parsing genuinely unknown external
input (model responses, user JSON), and generic plumbing where the type is the
caller's. Both need a comment.

### 5.3 Dataclasses

```python
@dataclass(frozen=True, slots=True)
class RenderRequest:
    viewpoint: EntityId
    width: int
    height: int
    samples: int = 16
```

- **Frozen by default.** Mutable dataclasses exist where mutation is the point
  (an accumulator, a builder) and are the exception.
- **`slots=True`** on anything that may exist in quantity — it removes the
  per-instance dict, which at 100,000 entities is the difference between 400 MB
  and 150 MB.
- **`field(default_factory=list)`** for mutable defaults, never `= []`.

### 5.4 Protocols over base classes

```python
class RenderBackend(Protocol):
    def capabilities(self) -> RenderCapabilities: ...
```

Structural typing means an implementation does not import ArchX3D to satisfy the
interface, which is what makes plugins and test fakes cheap. Abstract base
classes are used only where shared implementation genuinely justifies inheritance
— which, in this codebase, is almost nowhere.

---

## 6. Comments and docstrings

### 6.1 Module docstring

Required. Follows the shape in §2.1. States what the module is for, the key
decision, and what it never does.

### 6.2 Function docstring

Required for anything public or non-obvious. Prose first, then details.

```python
def merge(findings: Sequence[Finding]) -> list[Finding]:
    """Collapse findings that say the same thing about the same scope.

    Keeps the most severe instance and records how many viewpoints agreed —
    corroboration is information, and three viewpoints reporting the same dark
    room is a stronger signal than one.

    Confidence rises with corroboration but never to certainty: several views of
    one room share a light rig, so they are not independent observations.
    """
```

Note what this does *not* contain: a `:param:` list restating the signature. The
signature is typed; repeating it is noise. Parameters are documented only where
the name and type do not carry the meaning.

### 6.3 Inline comments

Reserved for the non-obvious. Three legitimate kinds:

```python
# 1. Why this and not the obvious thing
# Saturation first, then tint. Tinting toward a reference colour already carries
# that colour's saturation, so scaling afterwards would undo half the blend.

# 2. A constraint from outside this file
# Blender's bundled Python has no third-party packages, so this import must stay
# stdlib-only.

# 3. A known, accepted limitation
# A room's hash covers that room only, so repainting the kitchen does not
# re-render a living-room view that sees it through a doorway. Accepted: the
# alternative degrades to "one room invalidates the building" in open plan.
```

Never a comment that restates the line below it.

### 6.4 `#:` for attribute documentation

The existing convention, and it should continue — Sphinx picks it up and it reads
well inline:

```python
#: Perceptual roughness in [0, 1]; drives the Blender Principled BSDF.
roughness: float = 0.7
```

---

## 7. Constants and tuning values

### 7.1 Every constant has a unit, a range and a reason

```python
#: Bounds on a hand-edited dimension, in metres. Not taste — anything outside
#: this range produces geometry the generator cannot build sensibly.
MIN_DIMENSION = 0.05
MAX_DIMENSION = 20.0
```

A bare `MIN_DIMENSION = 0.05` is unreviewable: nobody can tell whether 0.05 is
right, and nobody will dare change it.

### 7.2 Thresholds live in one place

`ConfidencePolicy.ACCEPT = 0.65` is defined once and referenced everywhere.
Duplicating a threshold means two of them, and one will move.

The specific failure this codebase is exposed to: `MIN_DIMENSION` exists in
`vision/review.py` **and** in `web/lib/wizard.ts`. Under v2 both are generated
from one definition ([`SCENE_GRAPH_SPEC.md` §6.5](SCENE_GRAPH_SPEC.md#65-validation)).
Until then, a comment in each names the other.

### 7.3 Version constants

Anything that changes the meaning of a cached or persisted value carries a
version, and bumping it invalidates everything at once:

```python
#: Bump when the *meaning* of a hash changes — new inputs folded in, a different
#: attribution rule — to invalidate every existing key at once.
HASH_VERSION = "1"
```

This is `render/cache.py`'s pattern and it is exactly right. Copy it wherever a
computation's output is persisted.

### 7.4 Magic numbers

Zero tolerance in domain logic. `0.65`, `1e-6`, `0.15`, `3.0` all get names.
Exceptions: `0`, `1`, `2` where they mean what they say, and array indices.

---

## 8. Errors

Full hierarchy and rules in [`ARCHITECTURE.md` §12](ARCHITECTURE.md#12-error-handling).
The day-to-day rules:

```python
# Structured, with a remedy.
raise GeometryError(
    "wall segment is degenerate",
    entity=wall_id, length_m=1.4e-9, source="dxf:LINE#4A2F",
    remedy="check the DXF unit scale; 1e-9 m suggests a unit mismatch",
)
```

| Rule | |
| --- | --- |
| Never `except Exception: pass` | ever |
| Never a bare `except Exception` | except at a `@stage_boundary` |
| Never swallow and return a default | raise, or return an explicit "not computed" |
| Always `raise ... from exc` when re-raising | the original traceback is the evidence |
| Never format data into the message only | put it in fields |
| Always include a `remedy` where one exists | |

### Parsing external input is not error handling

`schema._f` turning `"1.2m"` into `1.2` is a parser doing its job, and its
docstring says why: *"VLM output routinely contains `null`, `"1.2m"` or `""` in
numeric slots; a bad number should degrade to the default, not kill the run."*
That is correct and stays.

What changes under v2: the coercion is **recorded**, so a downstream consumer can
tell a cleanly-parsed value from a rescued one.

---

## 9. Numbers, units and floating point

### 9.1 Canonical units

Metres, degrees, kelvin, watts, seconds. Conversion happens at the boundary
(import, export, UI), never in the middle.

### 9.2 Never compare floats with `==`

```python
if abs(a - b) < TOLERANCE:      # and TOLERANCE has a name and a reason
```

Tolerances are named per domain: `POSITION_TOLERANCE_M = 1e-4` (0.1 mm — below
any measurable placement difference), `ANGLE_TOLERANCE_DEG = 0.01`.

### 9.3 Rounding

**Round at the boundary, not during computation.** This is `evaluation.schema`'s
rule and its reasoning is right: *"Rounding at the boundary rather than during
measurement keeps the maths honest and the JSON diffable."*

Digest inputs round to 6 decimal places, per `render/cache._PRECISION`, because
that is far below any difference a preview could show and it stops float noise in
a JSON round trip producing spurious cache misses.

### 9.4 Non-finite values

`nan` and `inf` are rejected at every persistence and digest boundary. `nan`
encodes as invalid JSON and poisons a cache key; `inf` propagates silently through
an average and produces a score of `nan` three stages later.

```python
if not math.isfinite(value):
    raise ValidationError("non-finite value", field=name, value=repr(value))
```

### 9.5 Integers stay integers

Counts are `int`. `evaluation.schema._round` deliberately leaves them alone
because *"rounding turns 2 into 2.0, which reads as a measurement rather than a
tally."* Correct, and it applies everywhere.

---

## 10. Determinism in practice

Principle 3 as a set of habits.

| Do | Do not |
| --- | --- |
| `sorted(some_set)` before iterating into output | iterate a `set` |
| `sorted(d.items())` | rely on dict order |
| Tie-break until the order is total | sort on one key where ties exist |
| Seed from an explicit parameter | `random.random()` |
| Take time from an injected `Clock` | `time.time()` in logic |
| Accumulate in a defined order | reduce over a thread pool |
| `json.dumps(..., sort_keys=True)` for digests | default `json.dumps` |

### The total-order test

```python
def sort_key(action: Action) -> tuple[float, float, str]:
    """Total order: priority, then cheapness, then id.

    Total rather than merely sorted — two actions with identical priority and
    cost still order the same way every run, because the id breaks the tie. A
    plan whose order wobbles cannot be diffed between runs.
    """
    return (-round(action.priority, 6), round(action.cost, 6), action.id)
```

Every ranking function in the codebase ends on an id for this reason. When you
write a new one, check: can two elements compare equal? If yes, add a key.

---

## 11. Concurrency

### 11.1 The default is: do not

Most code in ArchX3D should be single-threaded and pure. Concurrency belongs in
`runtime`, in the render scheduler, and at the API edge. Domain code that spawns
a thread is almost always solving a scheduling problem in the wrong layer.

### 11.2 When you must

| Work | Mechanism |
| --- | --- |
| I/O bound (model calls, network, blob transfer) | `asyncio` with a bounded semaphore |
| CPU bound | a separate **process** — the GIL makes threads pointless here |
| External process (Blender) | subprocess, always, with a timeout and a killed process tree |
| Scene mutation | never concurrent; one writer per scene |

### 11.3 Rules

- **No shared mutable state without a lock**, and prefer having neither.
- **A lock protects data, not code.** Name it for what it protects
  (`self._jobs_lock`), and document the invariant it maintains.
- **Never hold a lock across I/O.** Copy what you need, release, then do the work.
- **Every subprocess has a timeout.** `subprocess.run(..., timeout=...)` is not
  optional. On Windows, killing a process does not kill its children — use a Job
  Object or the process-group helper in `archx3d.runtime.process`.
- **Concurrency must not change results** (§10). If it would, the concurrent
  version is wrong.

---

## 12. Configuration and dependencies

### 12.1 No module-level globals for state

```python
# Bad — and this exists today
BLENDER_EXECUTABLE_PATH = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"

# Good
def render(ctx: AppContext, plan: BuildPlan) -> RenderResult:
    backend = ctx.backends.resolve(ctx.config.render.backend)
```

Module-level constants that are genuinely constant (`MAX_DIMENSION`,
`HASH_VERSION`) are fine. Anything environment-dependent is configuration and
travels in the context.

### 12.2 Dependencies are parameters

A function that needs a repository takes one. It does not import a singleton, read
an environment variable, or construct one. This is what makes the test suite fast
and what makes two tenants in one process possible.

### 12.3 Adding a third-party dependency

Answer these in the PR:

1. What does it do that we would otherwise write?
2. How much would we write? (Under ~200 lines, write it.)
3. Is it maintained? Last release, open issues, bus factor.
4. Licence — compatible with Apache-2.0?
5. Transitive dependency count and total install size.
6. Which layer? (`core` and `scene` accept none.)
7. Which platforms does it have wheels for?

The default answer is no. A dependency is permanent.

---

## 13. Tests

Philosophy in [`ARCHITECTURE.md` §20](ARCHITECTURE.md#20-testing-philosophy).
The writing rules:

### 13.1 Name the condition

```python
def test_unmeasured_axis_is_excluded_from_normalisation(): ...
def test_locked_object_rejects_a_transform_edit(): ...
def test_two_viewpoints_of_one_room_merge_into_one_finding(): ...
```

Not `test_evaluate`, `test_apply`, `test_works`.

### 13.2 Arrange, act, assert — visibly

```python
def test_rejected_action_leaves_the_graph_unchanged():
    graph  = fixture_graph("living_room")
    before = graph.digest()
    action = Action(type=ActionType.FURNITURE_TRANSLATION, target="object:sofa_1",
                    parameters={"dx": 0.4, "dy": 0.0})

    result = optimizer.run(graph, plan_of(action), executor=always_worse())

    assert result.attempts[0].status == REJECTED
    assert graph.digest() == before
```

### 13.3 One behaviour per test

A test asserting five things fails on the first and hides the other four.

### 13.4 Fakes over mocks

A fake implementing the port is reusable, catches interface drift, and does not
break when an implementation is refactored. `unittest.mock.patch` on an internal
function couples the test to the implementation and is forbidden across module
boundaries.

Every port ships a fake in `archx3d.testing.fakes`.

### 13.5 Fixtures are shared and named for what they are

`tests/conftest.py`'s `rect_geometry` ("A 6 × 4 m rectangular room, in the shape
`dxf_extractor` emits") is the standard: a docstring saying what it represents
and where the shape comes from.

### 13.6 Every bug fix ships a test

The test fails without the fix. No exceptions. This is the single rule that
prevents the same bug twice.

### 13.7 Never test private functions across a boundary

If a private function needs testing directly, it wants to be public, or it wants
to be in its own module with a public surface.

---

## 14. TypeScript and the frontend

### 14.1 Types come from Python

`@archx3d/schema` is generated. Never hand-write a type that mirrors a Python
one; the generator exists so they cannot drift, and a hand-written mirror defeats
it silently.

### 14.2 Same rules, different syntax

`strict: true`, no `any` (use `unknown` and narrow), explicit return types on
exported functions, `readonly` for anything not meant to be mutated, discriminated
unions over optional-field soup.

### 14.3 State is immutable

The existing `EditorDoc` is right about this: *"every pending change lives in one
immutable `EditorDoc`, and history is a stack of those documents."* v2 keeps the
immutability and replaces the document stack with an operation journal
([`ARCHITECTURE.md` §19](ARCHITECTURE.md#19-frontend-architecture)) — but the
reasoning in that docstring, that undo must restore *everything* a step changed
and scattered `useState` cannot express that, is exactly correct and should be
read by anyone touching the editor.

### 14.4 Components

- Presentational components take props and render. No fetching, no business
  logic.
- Business logic lives in `lib/`, is pure, and is tested with `node:test` — no
  DOM, no React, no browser.
- Hooks compose logic; they do not contain it.
- No component over 300 lines.

### 14.5 Performance

- `key` on every list item, and never the array index for a reorderable list.
- Memoise expensive derivations, not cheap ones. Measure first.
- The spatial index and diff computation run in a Worker, not on the main thread.
- Virtualise any list that can exceed 100 rows.

---

## 15. Tooling

All enforced in CI. None of it is negotiable in review, because a tool argument
in a PR is a wasted argument.

| Tool | Purpose | Config |
| --- | --- | --- |
| **ruff** | lint + format | line length 88, `pyproject.toml` |
| **mypy** | types | `--strict` on `core`/`scene`/`ports` |
| **import-linter** | layering | contracts in `pyproject.toml` |
| **pytest** | tests | `-q`, `--strict-markers` |
| **pytest-randomly** | order independence | seeded, reported |
| **coverage** | reported, gated only where §20 says | |
| **bandit** | security lint | |
| **pip-audit** | dependency advisories | |
| **eslint + prettier** | TS lint and format | |
| **tsc --noEmit** | TS types | `strict: true` |
| **pre-commit** | run the fast subset locally | |

`ruff format` is the arbiter of formatting. Nobody reviews whitespace.

---

## 16. Git and pull requests

### 16.1 Branches

`<type>/<short-description>` — `feat/level-abstraction`, `fix/render-cache-key`,
`docs/plugin-spec`.

### 16.2 Commits

Conventional Commits, because the changelog is generated from them:

```
feat(scene): add the Level abstraction

Levels define a plan-coordinate frame at an elevation, which is what makes
multi-floor work without turning every 2-D coordinate into a 3-D one.

Closes #142
```

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`.
`!` or a `BREAKING CHANGE:` footer marks a break.

### 16.3 Pull requests

Small, focused, one concern. A PR that changes behaviour *and* refactors is two
PRs, because a reviewer cannot separate the intentional changes from the
incidental ones.

Required in the description:

- **What** changed, in one sentence.
- **Why** — the problem, not the solution.
- **Alternatives considered** and why they lost. Skip only for trivial changes.
- **Tests** — what proves it works.
- **Docs** — which document was updated, or why none needed to be.
- **Breaking?** — and the migration.
- **Golden-file diffs** — explained, one by one.

### 16.4 Review

Two approvals for `core`, `scene`, `ports` and anything touching the journal.
One elsewhere. A `CODEOWNERS` approval is required for the owning package.

Reviewers are expected to run the code, not only read it, for anything that
changes behaviour.

---

## 17. Review checklist

For the author before requesting review, and for the reviewer.

**Correctness**
- [ ] Every new decision path has a test.
- [ ] Boundary cases: empty, one, many, degenerate, non-finite.
- [ ] Failure paths are exercised, not just the happy one.
- [ ] Nothing catches a bare `Exception` outside a stage boundary.
- [ ] No `except: pass`.

**Principles**
- [ ] Nothing predicts an improvement it did not measure (§1).
- [ ] Nothing invents data to fill a gap (§2).
- [ ] Every ordering is total; no set/dict iteration reaches output (§3).
- [ ] Every value written carries provenance (§4).
- [ ] Nothing scores an unmeasured quantity zero (§5).
- [ ] All mutation goes through the operation algebra (§6).
- [ ] `core`/`scene` gained no dependency (§7).
- [ ] Degradation is at a seam and is a terminal state, not a log line (§8).
- [ ] Nothing overrides a `user`-provenance value (§9).
- [ ] Every expensive call is cached, counted and bounded (§10).
- [ ] No interface with one implementation and no named second (§11).
- [ ] Docs updated in this commit (§12).

**Architecture**
- [ ] The change is in the right layer.
- [ ] No forbidden import; import-linter passes.
- [ ] No deferred import hiding a cycle.
- [ ] No `sys.path` mutation.
- [ ] Dependencies are parameters, not globals.

**Style**
- [ ] Module docstring says what it is and what it never does.
- [ ] Non-obvious decisions carry their rejected alternative.
- [ ] Constants have units and reasons.
- [ ] Types are precise; no unjustified `Any`.
- [ ] British spelling.
- [ ] Domain vocabulary used correctly (§3.2).

**Performance**
- [ ] No O(n²) over entities. No linear scan where an index exists.
- [ ] No whole-document copy where a delta would do.
- [ ] Anything in a hot path has a benchmark.

---

## 18. Anti-patterns specific to this system

The mistakes this codebase invites, each with the reason it is tempting.

### 18.1 The confidence ratchet

```python
# Tempting: the demo looks sparse.
confidence = min(1.0, confidence * 1.2)
```

Raising a confidence to clear a threshold converts an honest uncertainty into a
false assertion, and it does it invisibly. Thresholds move by argument, in one
place, with a changelog entry. Confidences are never scaled to reach them.

### 18.2 The silent default

```python
dimensions = detected_dimensions or CATEGORY_PRIOR[category]
```

Now nobody can tell what was measured. If a prior is used, it is written with
`source: "prior"` and its own confidence. This is the single easiest way to
destroy the value of the whole provenance system.

### 18.3 The helpful correction

```python
if overlaps(obj, other):
    obj.position.x += nudge          # "just move it a bit"
```

The user may have placed it there deliberately. Report, do not correct. The
pre-build check exists and is report-only for exactly this reason.

### 18.4 The convenient linear scan

```python
for obj in graph.objects:
    parent = graph.object_by_id(obj.support_id)     # O(n) inside O(n)
```

The most common performance bug in this codebase, and it is invisible at forty
objects. Use the index.

### 18.5 The second writer

"I just need to set one field, going through the operation algebra is overkill."
That is how three mutation paths happened. There is one writer.

### 18.6 The unversioned cache key

Adding an input to a computation without bumping its `HASH_VERSION` leaves every
existing cache entry claiming to describe the new computation. The failure is a
wrong image scored against a photograph, three stages downstream, with no error.

### 18.7 Optimising the wrong thing

See [`PERFORMANCE_GUIDE.md` §8](PERFORMANCE_GUIDE.md#8-what-should-never-be-optimised).
Briefly: the evaluation engine's determinism, the confidence policy's
conservatism, rollback correctness and the cache's conservative invalidation are
all *deliberately* not the fastest option, and each has an argument behind it.
Speeding them up is a regression wearing a benchmark.

### 18.8 The bpy import

Any `import bpy` outside `archx3d-blender` breaks the whole backend-independence
design, and it starts as one small convenience. There is exactly one place
Blender is imported.

### 18.9 The model in the middle

Calling a model from inside fusion, grounding, evaluation or the optimiser
destroys the determinism boundary and makes the affected stage untestable,
uncacheable and unreproducible. Models are called in one stage, and that stage
produces observations.

### 18.10 Prose as data

```python
obj.flags.append(f"optimiser moved by ({dx:+.3f}, {dy:+.3f}) m")
```

Useful for a human, and it is history encoded as an unqueryable string. Under v2
the journal records the change structurally; the human-readable note is derived
from it, not the record of it.

---

## Related

- [`ENGINEERING_PRINCIPLES.md`](ENGINEERING_PRINCIPLES.md) — the twelve rules these guidelines apply.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — layering, error hierarchy, testing tiers, versioning.
- [`SCENE_GRAPH_SPEC.md`](SCENE_GRAPH_SPEC.md) — the operation algebra and component conventions.
- [`PERFORMANCE_GUIDE.md`](PERFORMANCE_GUIDE.md) — what to optimise and what to leave alone.
- [`PLUGIN_SPEC.md`](PLUGIN_SPEC.md) — plugins are reviewed against this document.
