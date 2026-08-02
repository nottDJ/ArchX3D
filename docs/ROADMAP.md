# ArchX3D — Roadmap (v2 through v5, plus the research programme)

Where ArchX3D goes over the next three to five years: four major engineering
releases, the research programme that runs alongside them, and the open-source
and commercial strategy that pays for both.

```
 v1 ──────────► v2 ──────────► v3 ──────────► v4 ──────────► v5
 prototype      foundation     platform       ecosystem      scale
 (now)          12 months      +9 months      +12 months     +12 months

               packaging      distributed    plugins        research
               scene v2       cloud          marketplace    platform
               operations     collaboration  BIM            federation
               BuildPlan      multi-backend  enterprise     10⁷ scenes
```

**Status.** Living document. Dates are relative to v2 kickoff, not calendar
dates; effort is in engineer-months. Every release has explicit success criteria,
and a release that does not meet them does not ship — the date moves.

---

## Contents

**Part I — Engineering**
1. [How releases are planned](#1-how-releases-are-planned)
2. [v2 — Foundation](#2-v2--foundation)
3. [v3 — Platform](#3-v3--platform)
4. [v4 — Ecosystem](#4-v4--ecosystem)
5. [v5 — Scale](#5-v5--scale)
6. [Cross-cutting risks](#6-cross-cutting-risks)
7. [What is explicitly not on the roadmap](#7-what-is-explicitly-not-on-the-roadmap)

**Part II — Research**
8. [The research thesis](#8-the-research-thesis)
9. [Research directions](#9-research-directions)
10. [Datasets](#10-datasets)
11. [Evaluation protocol](#11-evaluation-protocol)
12. [Human studies](#12-human-studies)
13. [Publication plan](#13-publication-plan)
14. [Intellectual property](#14-intellectual-property)

**Part III — Strategy**
15. [Open-source strategy](#15-open-source-strategy)
16. [Commercial strategy](#16-commercial-strategy)
17. [Industry partnerships](#17-industry-partnerships)
18. [Governance](#18-governance)

---

# Part I — Engineering

## 1. How releases are planned

### The sequencing rule

**Each release removes the technical debt that blocks the next one.** Not
"delivers features and also does some cleanup" — the debt removal *is* the
release, and the features are what the removal makes possible.

This is why v2 contains comparatively little that a user can see. Packaging, the
scene store and the operation algebra are not features. They are the reason v3
can have collaboration, v4 can have a plugin marketplace, and v5 can hold ten
million scenes. Attempting any of those on v1's foundation produces a system that
works in a demo and cannot be maintained.

### Release contract

Every release states, before work begins:

| Section | Meaning |
| --- | --- |
| **Objective** | one paragraph; what is different afterwards |
| **Architectural changes** | what moves, what is created, what is deleted |
| **Breaking changes** | exhaustive; each with a migration |
| **Migration plan** | how an existing user gets across |
| **Effort** | engineer-months, with the largest items itemised |
| **Debt removed** | which defect from `ARCHITECTURE.md` §2 this closes |
| **Risks** | with mitigation and a trigger for abandoning the approach |
| **Success criteria** | measurable; the release does not ship without them |

### Never

- No flag day. At every commit, the pipeline produces a model.
- No unmigrated data. Every document from every released version opens.
- No feature that requires abandoning a principle. The principle wins; the
  feature is redesigned or dropped.

---

## 2. v2 — Foundation

**12 months · ~34 engineer-months · 3–4 engineers**

### Objective

Turn a working prototype into a codebase that a team can build on for five years.
Nothing in v2 requires a new idea; everything in it is the difference between a
project and a platform.

At the end of v2, ArchX3D is `pip install`-able, has one way to change a scene,
holds 100,000 entities, runs multi-floor, renders through a backend-neutral
document, and stores its history.

### Scope

#### 2.0 — Packaging and hygiene · *2 months · 3 em*

Closes **D4**.

- `pyproject.toml`; six distributions per [`ARCHITECTURE.md` §8](ARCHITECTURE.md#8-distributions-and-installation-profiles).
- `modules/` → `archx3d/`; all nine `sys.path.insert` sites deleted; a test
  asserts none return.
- Six-layer structure with `import-linter` contracts in CI.
- `AppContext`; module-level globals (`BLENDER_EXECUTABLE_PATH`, `BASE_DIR`,
  `DATA_DIR`, `CONFIG_PATH`) removed.
- Deep, validated, layered config; `archx3d config show --explain`.
- Structured logging; every `print` outside the CLI and the Blender adapter
  removed.
- CI: ruff, mypy, import-linter, pytest, coverage, pip-audit, bandit.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `LICENSE` (Apache-2.0),
  `CODEOWNERS`, issue and PR templates, ADR directory.
- **The quadratic `validate_graph` fix** — one line, and currently the difference
  between validation completing and not.

#### 2.1 — Operation algebra · *2 months · 4 em*

Closes **D3**.

- The twenty operations, with apply, materialised inverse, validator and tests.
- `Transaction`, `Commit`, `Journal` over the existing `SceneGraph` — storage
  unchanged at this stage, so the change is isolated.
- `ConstraintRule` port; `optimizer.constraints`' immutable set re-expressed as
  provenance predicates.
- `review.apply_edits` → an operation compiler.
- `optimizer.mutations.apply` → an operation compiler. Its per-handler discipline
  is preserved verbatim.
- `rollback` → journal inversion; snapshots retained as checkpoints.
- `@archx3d/schema` generated; `MIN_DIMENSION` and every other duplicated
  constant now has exactly one definition.
- `web/lib/editor.ts` emits operations.

#### 2.2 — Scene store · *3 months · 8 em*

Closes **D1** and **D2**.

- Entity–component model; typed views so call sites stay readable.
- `EntityId` (UUIDv7), `StableKey`, `ExternalRef`.
- SQLite repository; `.arx` format; `SceneRepository` contract suite.
- Identity, containment, kind, relationship indexes.
- Spatial index: per-level grid + BVH; Hilbert ordering.
- Typed query API with three compilation targets.
- Lazy and partial loading.
- Migration from v1 `scene_graph.json`.
- The `tower` fixture and the §23 performance budgets, in CI.

#### 2.3 — Levels · *1 month · 2 em*

- `level` entity kind; plan frames; elevations.
- Single-level scenes get a synthesised level, so nothing existing changes.
- Multi-floor DXF extraction (layer-per-storey and elevation-based).
- `connects_level` relationships; stairs and lifts.
- Multi-level review UI and viewport.

#### 2.4 — BuildPlan and backend independence · *2 months · 6 em*

Closes **D7**.

- The BuildPlan document and its schema.
- `build.compile`: scene → BuildPlan. Pure, deterministic, cached.
- `archx3d-blender`: a dependency-free BuildPlan interpreter. **No ArchX3D module
  is imported into Blender's Python again**, which deletes the stdlib-only
  constraint at its root rather than policing it.
- `RenderBackend` port with capability negotiation; `blender.eevee` and
  `blender.cycles`.
- Evaluation axes declare required AOVs; a backend that cannot supply one yields
  `measured=False` with a reason naming the backend.

#### 2.5 — Pipeline and performance · *1 month · 4 em*

- `archx3d.pipelines`: stages as functions, in one process. Blender stays a
  subprocess.
- Parallel model calls (bounded concurrency).
- Render batching and a process-pool `BatchExecutor`.
- Vectorised evaluation imaging, gated on identical golden output.
- Spatial hashing in fusion.
- `archx3d` CLI replacing `main.py`; `archx3d doctor`.

#### 2.6 — Hardening · *1 month · 7 em*

- Migration tooling and the `historical` fixture corpus.
- Determinism suite.
- Documentation rewrite against the shipped code.
- Beta programme; performance validation on real projects.

### Breaking changes

| Change | Migration |
| --- | --- |
| `modules.*` → `archx3d.*` | `archx3d migrate imports` rewrites them; a compatibility shim for one minor with a `DeprecationWarning` |
| `scene_graph.json` → `.arx` | `archx3d migrate scene` |
| `review.apply_edits(graph, edits)` → operations | shim translating `ReviewEdits` to operations, one minor |
| `main.py` → `archx3d` | `main.py` becomes a shim printing the new command |
| `config.json` → `archx3d.toml` | `archx3d migrate config` |
| `MutationResult` / `EditReport` → commits | both remain as views over a commit for one minor |
| `bpy` imports of ArchX3D modules | removed; the adapter is standalone |

### Effort

| Phase | em |
| --- | --- |
| 2.0 Packaging | 3 |
| 2.1 Operations | 4 |
| 2.2 Scene store | 8 |
| 2.3 Levels | 2 |
| 2.4 BuildPlan | 6 |
| 2.5 Pipeline | 4 |
| 2.6 Hardening | 7 |
| **Total** | **34** |

### Debt removed

D1 (scene scalability), D2 (storage), D3 (mutation duplication), D4 (packaging),
D7 (Blender coupling). D5 and D6 are deliberately deferred: a plugin system needs
stable ports, and enterprise architecture needs a durable store — both of which
v2 creates.

### Risks

| Risk | Likelihood | Impact | Mitigation | Abandon trigger |
| --- | --- | --- | --- | --- |
| Scene store rewrite overruns | high | high | phased behind `SceneRepository`; v1 stays the implementation until the contract suite passes | 2.2 exceeds 12 em |
| Entity–component hurts readability | medium | medium | typed views built in the same phase, not after; readability reviewed as an explicit acceptance criterion | domain code becomes measurably harder to review |
| BuildPlan cannot express current Blender output | medium | high | build it against the *existing* generator's output first and diff renders before deleting anything | render diff > 2% perceptual on the fixture corpus |
| Determinism lost to parallelism | medium | high | determinism suite lands in 2.0, before any concurrency | any suite failure blocks the concurrent path |
| Scope creep into v3 features | high | medium | v3 features are listed and refused by name | — |

### Success criteria

- [ ] `pip install archx3d` works on Linux, macOS and Windows.
- [ ] Zero `sys.path` mutations; import-linter passes with no exemptions.
- [ ] One mutation path; `grep` finds no second writer.
- [ ] `tower` fixture (110k entities) meets every budget in `SCENE_GRAPH_SPEC.md` §23.
- [ ] Multi-floor DXF produces a correct multi-level scene.
- [ ] A BuildPlan renders identically through the Blender adapter and the v1
      generator, on the whole fixture corpus.
- [ ] `archx3d-blender` imports nothing from ArchX3D.
- [ ] Unit suite under 60 s with no GPU, network, Blender or database.
- [ ] Full pipeline on `apartment` under 4 minutes cold (from ~6.5).
- [ ] Every v1 `scene_graph.json` in the corpus migrates losslessly.
- [ ] Documentation matches the code; every example runs in CI.

---

## 3. v3 — Platform

**9 months · ~28 engineer-months · 5–6 engineers**

### Objective

Make ArchX3D a service. Multiple users on one scene, work distributed across a
fleet, several rendering backends, and an API a third party can build against.

### Scope

#### 3.0 — Server and persistence · *2 months · 6 em*

Closes **D6**, part one.

- PostgreSQL repository passing the same contract suite as SQLite.
- Multi-tenancy with row-level security.
- Auth: API keys, OAuth 2.1 + PKCE, OIDC/SAML.
- RBAC.
- Object storage adapter (S3/R2/MinIO/filesystem).
- API v1 per [`API_SPEC.md`](API_SPEC.md); the legacy `server.py` surface behind
  a deprecation shim.

#### 3.1 — Distributed execution · *2 months · 6 em*

Closes **D6**, part two.

- Job/task DAG; content-addressed task keys; `UNIQUE (task_key)` deduplication.
- Lease-based pull scheduling with `SKIP LOCKED`.
- Worker classes: `cpu.light`, `cpu.heavy`, `gpu.render`, `gpu.infer`,
  `net.model`.
- Retry policy by error class; dead-letter with replay.
- Distributed render cache over shared blob storage.
- Autoscaling; spot instances with checkpointing.
- Per-job budgets, enforced.
- OpenTelemetry: metrics, traces, structured logs, run records.

#### 3.2 — Collaboration · *2 months · 6 em*

- WebSocket live channel; operation broadcast and rebase.
- Conflict resolution per `SCENE_GRAPH_SPEC.md` §9.
- Presence: cursors, selections, frusta, soft locks.
- Offline queue and sync; the same rebase algorithm.
- History UI: timeline, blame, diff, named versions, branches.

#### 3.3 — Multi-backend rendering · *1.5 months · 4 em*

- USD export and import; Hydra render delegate (`hydra.storm`).
- glTF pipeline: Draco, KTX2, instancing, per-level splitting.
- Cloud rendering: GPU workers, spot, tile checkpointing.
- Backend conformance suite.

#### 3.4 — Frontend · *1.5 months · 6 em*

- Viewport abstraction; Three.js implementation.
- Instanced rendering, LOD, frustum and room-graph occlusion culling.
- Streaming load in Hilbert order.
- Journal-based undo in the client.
- Web Workers for the spatial index and diff.
- Tauri desktop shell.
- PWA with offline support.

### Breaking changes

| Change | Migration |
| --- | --- |
| API v0 (`/api/*`) → v1 (`/v1/*`) | 12-month deprecation with `Sunset` headers; shim for everything except `POST /api/generate`'s blocking semantics |
| Auth required by default | `--no-auth` for local, with a warning |
| `projects/<id>/` directory layout → database | `archx3d migrate projects` |

### Effort

| Phase | em |
| --- | --- |
| 3.0 Server | 6 |
| 3.1 Distributed | 6 |
| 3.2 Collaboration | 6 |
| 3.3 Rendering | 4 |
| 3.4 Frontend | 6 |
| **Total** | **28** |

### Risks

| Risk | Mitigation |
| --- | --- |
| Collaboration conflict resolution is subtle and bugs are data loss | property-based tests over random operation interleavings; every conflict path has a golden trace; the losing operation is always retained in the journal |
| Cloud cost exceeds the model | budgets from day one; cost per reconstruction is a tracked metric with an alert |
| Spot reclamation causes user-visible failures | checkpointing plus idempotent tasks; measured reclamation-to-completion rate as a release gate |
| Two backends diverge in output | conformance suite plus perceptual diff on the fixture corpus, nightly |

### Success criteria

- [ ] 10 concurrent editors on one scene, no data loss under adversarial testing.
- [ ] 1,000 concurrent jobs across an autoscaled fleet.
- [ ] Distributed render cache hit rate > 70% on a realistic workload.
- [ ] API p99 < 200 ms.
- [ ] Offline edit for 24 h, then sync, with no manual conflict for commuting ops.
- [ ] 100k-entity scene at 60 fps in the browser.
- [ ] A scene renders through Blender and Hydra with < 2% perceptual difference.
- [ ] Cost per reconstruction < $1.00.
- [ ] 99.9% availability over a 30-day soak.

---

## 4. v4 — Ecosystem

**12 months · ~32 engineer-months · 6–8 engineers**

### Objective

Stop being the only people who can extend ArchX3D. Plugins, BIM, enterprise
deployment, and the beginnings of a marketplace.

### Scope

#### 4.0 — Plugin system · *3 months · 8 em*

Closes **D5**.

- The full contract in [`PLUGIN_SPEC.md`](PLUGIN_SPEC.md): manifest, lifecycle,
  discovery, registration, resolution, capabilities, isolation levels.
- Contract suites published in `archx3d.testing`.
- Registry with signing, transparency log, revocation, nightly compatibility runs.
- Every built-in extension converted to a plugin, dogfooding the contract.
- Plugin SDK, templates, documentation.

#### 4.1 — BIM and interoperability · *2.5 months · 7 em*

- IFC 4.3 import and export, streaming and chunked.
- Round-trip fidelity with `GlobalId` preservation and an explicit loss report.
- Quantity take-off (`core:quantities`).
- Revit and ArchiCAD bridges (via IFC first, native later).
- Point cloud import (E57, LAS) for scan-to-BIM.
- USD layer composition for DCC workflows.

#### 4.2 — Enterprise · *2.5 months · 7 em*

- SSO, SCIM provisioning, audit export.
- Data residency; regional deployment; customer-managed keys.
- Air-gapped installation with a local model provider.
- SOC 2 Type II readiness; GDPR tooling (export, erasure, DPA).
- Admin console; usage analytics; chargeback.
- SLA monitoring and support tooling.

#### 4.3 — AI expansion · *2 months · 6 em*

- Segmentation, depth and embedding provider ports, with reference
  implementations (SAM 2, Depth Anything V2, SigLIP).
- Local VLM support (llama.cpp / vLLM / Ollama).
- Material and lighting estimation from imagery.
- Asset retrieval over manufacturer catalogues via pgvector.
- 3D generation as a last-resort provider, always labelled `generated`.
- Record/replay fixtures for every provider.

#### 4.4 — Marketplace foundations · *2 months · 4 em*

- Entitlement and licence validation with offline grace.
- Publisher onboarding and identity verification.
- Discovery, ratings, reviews.
- Payment and revenue share.

### Breaking changes

Contract version 1 is introduced here, so there is nothing to break yet.
`SCHEMA_VERSION` moves to 2.2 additively for IFC-derived components.

### Effort

| Phase | em |
| --- | --- |
| 4.0 Plugins | 8 |
| 4.1 BIM | 7 |
| 4.2 Enterprise | 7 |
| 4.3 AI | 6 |
| 4.4 Marketplace | 4 |
| **Total** | **32** |

### Risks

| Risk | Mitigation |
| --- | --- |
| Plugin API freezes before it is right | v4.0 ships contract 1 as *provisional* for one minor; feedback from the first ten third-party plugins before it is frozen |
| A malicious plugin damages trust in the platform | container isolation is mandatory in multi-tenant cloud; capability declaration; transparency log; incident playbook rehearsed |
| IFC round-trip fidelity is worse than promised | measured on a public corpus; the loss report is a product feature, not an apology |
| Enterprise requirements pull the roadmap | one enterprise-only feature per release, chosen deliberately, not by whoever shouted loudest |

### Success criteria

- [ ] 10 third-party plugins, at least 5 from outside the core team.
- [ ] Plugin API unchanged for 6 months after freeze.
- [ ] IFC round-trip preserves geometry, spaces, storeys and `GlobalId`s on a
      public building corpus, with a complete loss report.
- [ ] SOC 2 Type II achieved.
- [ ] An air-gapped install completes a full reconstruction with no network.
- [ ] Three enterprise customers in production.
- [ ] A local-only pipeline (local VLM, local SAM, local depth) matches hosted
      quality within 10% on the benchmark.

---

## 5. v5 — Scale

**12 months · ~30 engineer-months · 8–10 engineers**

### Objective

Portfolio scale, and a research platform other people publish on.

### Scope

#### 5.0 — Data-plane scale · *3 months · 8 em*

- Scene sharding; regional deployment; global routing.
- Read replicas; tiered storage; journal compaction at scale.
- Cross-region replication and disaster recovery drills.
- 10⁷ scenes, 10¹⁰ entities under management.

#### 5.1 — Portfolio · *2.5 months · 6 em*

- Multi-building sites and campuses.
- Cross-project asset and material libraries.
- Portfolio-level analytics via DuckDB over exported journals.
- Batch operations across thousands of scenes.
- Template and standard enforcement.

#### 5.2 — Research platform · *3 months · 8 em*

- Public benchmark suite with a leaderboard.
- Reproducible experiment runner; every run record complete and replayable.
- Dataset publication tooling with licensing and consent tracking.
- Ablation and sweep infrastructure.
- Public API for academic use.

#### 5.3 — Advanced reconstruction · *3.5 months · 8 em*

Whatever the research programme has proven by then. Candidates, in current order
of promise:

- Metric depth and multi-view geometry replacing single-image back-projection.
- Differentiable rendering for appearance and lighting recovery.
- Learned layout priors constrained by observation.
- Neural material estimation.
- Video and walkthrough input rather than stills.

The specific list will be wrong by then; the *mechanism* — research proves it,
then it ships behind a provider port — is the durable part.

### Effort

| Phase | em |
| --- | --- |
| 5.0 Data plane | 8 |
| 5.1 Portfolio | 6 |
| 5.2 Research platform | 8 |
| 5.3 Reconstruction | 8 |
| **Total** | **30** |

### Success criteria

- [ ] 10⁷ scenes, 10¹⁰ entities, p99 < 100 ms.
- [ ] Cost per reconstruction < $0.10.
- [ ] Benchmark adopted by at least three external groups.
- [ ] Five external publications using the platform.
- [ ] Reconstruction accuracy improved 30% over v2 on the benchmark.
- [ ] 99.95% availability.

---

## 6. Cross-cutting risks

| Risk | Horizon | Mitigation |
| --- | --- | --- |
| **Frontier models make the pipeline obsolete** — a single model does image → 3D scene end to end | v3–v4 | The pipeline's value is not the model. It is metric accuracy, evaluation, refinement, provenance, editability and BIM interop. A better model plugs into `VisionProvider` and makes ArchX3D better. Being *provider-agnostic from v2* is the hedge, and it is already the design. |
| **Blender changes incompatibly** | continuous | BuildPlan means one adapter, not a codebase. Pin versions; test the matrix; a second backend from v3. |
| **Model provider pricing or terms change** | continuous | Multi-provider from v2; local providers from v4; content-addressed caching means a re-run costs nothing. |
| **A large incumbent ships the same product** | v3–v5 | Open source and the research platform are the moat a feature race is not. Compete on evaluation rigour, extensibility and interop — the parts that take years to copy. |
| **Team growth degrades the codebase** | v3+ | Layer enforcement in CI, contract suites, CODEOWNERS, the review checklist, and a documentation standard that is enforced rather than aspired to. |
| **Research does not deliver** | v5 | v5.3 is the only research-dependent scope, it is last, and everything before it stands alone. |
| **Open-source community does not materialise** | v3–v4 | Plugins are useful even with three authors. The platform does not depend on a community; a community would accelerate it. |
| **Key-person dependency** | continuous | Two reviewers on core packages; documentation that explains *why*; ADRs; no undocumented subsystem. |

---

## 7. What is explicitly not on the roadmap

Naming these prevents them being re-proposed every quarter.

| Not doing | Why |
| --- | --- |
| **Generative furnishing as a default** | Violates principle 2. Offered explicitly, labelled, never in the reconstruction path. |
| **Our own renderer** | Blender, Cycles, Hydra and the game engines are decades ahead. A renderer is not where our value is. |
| **Our own 3D file format as an interchange target** | `.arx` is our document; USD, glTF and IFC are interchange. A fourth format helps nobody. |
| **A visual programming/node editor** | Enormous surface, narrow audience. A Python SDK serves the same users better. |
| **Real-time multiplayer walkthrough (VR/game)** | Export to Unreal/Unity/Godot. That is what they are for. |
| **Structural, MEP or energy simulation** | Deep, regulated domains. Plugin points and BIM export; not first-party. |
| **A mobile capture app** | Real value, but a different product and a different team. Partner or plugin. |
| **Automatic code compliance** | Jurisdiction-specific, liability-heavy. Data model supports it; we do not assert compliance. |
| **Replacing CAD** | ArchX3D reads plans; it is not a drafting tool. |
| **Our own model training as a core capability** | Fine-tune where it clearly wins; the platform's value is provider-agnostic. |

---

# Part II — Research

## 8. The research thesis

> **Photorealistic reconstruction of a specific interior from sparse, uncalibrated
> imagery and a floor plan can be made measurable, and a measurable
> reconstruction can be improved by closed-loop optimisation without human
> supervision.**

Three claims, each independently defensible and each already partly demonstrated
in v1:

1. **Reconstruction fidelity is decomposable.** Not one similarity number, but
   colour, material, lighting, layout and objects — each measurable, each
   attributable to a subsystem. The `Finding` design is this claim made concrete.
2. **Attribution enables automatic repair.** A difference that names the
   responsible subsystem can be turned into an executable action. The planner
   demonstrates this.
3. **Measured acceptance beats predicted acceptance.** A loop that applies,
   re-renders, re-scores and rolls back what did not help converges without
   supervision and cannot degrade. The optimiser demonstrates this.

**What makes this a research contribution rather than engineering** is the
combination. There is substantial work on single-image 3D reconstruction, on
scene graph generation, and on procedural interior generation. There is very
little on *closed-loop, measurement-driven refinement of a reconstruction against
its own source imagery*, and essentially none with subsystem attribution.

### Why the negative results matter too

The system's design contains several honest limits that are themselves publishable:
what a per-viewpoint metric cannot see; where render-cache under-invalidation
costs accuracy; how far a fitted camera can be trusted; what fraction of a room a
sparse photo set actually covers. Coverage-aware evaluation — reporting what could
not be assessed rather than scoring it zero — is an unusual and defensible
position in a field where most metrics quietly average over what they could
measure.

---

## 9. Research directions

Nine directions, ordered by expected contribution. Each states the question, the
approach, the evaluation, and the risk.

### R1 — Decomposed, attributable reconstruction metrics · **highest value**

**Question.** Can reconstruction fidelity be decomposed into axes that are both
perceptually meaningful and mechanically actionable?

**Approach.** Formalise the five-axis decomposition; validate each axis against
human judgement; establish which axes humans weight and how that differs from the
default weights; extend to acoustics and accessibility.

**Evaluation.** Correlation between axis scores and human pairwise preference;
inter-rater reliability; ablation of each axis against overall preference.

**Risk.** Low. The apparatus exists. The work is validation and formalisation.

**Why it is first.** Everything else in the programme depends on the metric being
trustworthy. A benchmark with an unvalidated metric is a benchmark nobody adopts.

### R2 — Closed-loop reconstruction refinement

**Question.** Does measurement-driven refinement converge, and how far?

**Approach.** Characterise convergence over the corpus; compare action-selection
strategies (greedy by expected gain, bandit, Bayesian optimisation over the
action space); analyse where the loop stalls and why.

**Evaluation.** Score improvement versus iterations; wall-clock and monetary cost
to a target; comparison against single-shot reconstruction and against a human
editor given the same time.

**Risk.** Medium. The loop may converge quickly and shallowly, which is itself a
result worth publishing — it would say the action vocabulary is the limit, not
the search.

### R3 — Multi-view metric grounding

**Question.** How much does metric depth plus multi-view consistency improve
placement over single-image back-projection?

**Approach.** Replace fitted-camera back-projection with metric depth (Depth
Anything V2, UniDepth, Metric3D) plus multi-view triangulation, constrained by
the floor plan. The floor plan is a strong prior almost nobody in single-image
reconstruction has.

**Evaluation.** Placement error against measured ground truth; ablation of plan
constraint, depth, and multi-view.

**Risk.** Low technically. Likely the largest single accuracy win available.

### R4 — Confidence calibration for reconstruction

**Question.** Are VLM-reported confidences calibrated, and can a calibrated
confidence be recovered?

**Approach.** Measure calibration across providers and object categories;
post-hoc calibration; corroboration-aware fusion; propagate calibrated confidence
to the accept/review/discard bands.

**Evaluation.** Expected calibration error; the precision/recall curve of the
accept threshold; downstream reconstruction score as a function of threshold.

**Risk.** Low. Highly likely to produce a usable result, and directly improves the
product.

**Why it matters beyond ArchX3D.** "A missing object is better than an invented
one" is a policy with a tuning parameter, and nobody has characterised that
parameter for VLM-driven scene reconstruction.

### R5 — Differentiable appearance and lighting recovery

**Question.** Can differentiable rendering recover materials and lighting more
accurately than VLM estimation plus procedural priors?

**Approach.** Differentiable rendering (Mitsuba 3 / nvdiffrast) over the
reconstructed geometry, optimising material and lighting parameters against the
reference photographs, initialised from the VLM estimate.

**Evaluation.** Colour, material and lighting axis scores; convergence cost;
robustness to geometry error.

**Risk.** High. Depends on geometry accuracy, and differentiable rendering is
notoriously sensitive to initialisation. Which is exactly why the VLM estimate as
an initialiser is the interesting part.

### R6 — Layout priors under observational constraint

**Question.** How should a learned layout prior be combined with sparse
observation without overriding it?

**Approach.** Learned priors over furniture arrangement conditioned on room type,
style and geometry; used *only* to place what was observed but not localised, and
to detect implausible observed placements — never to add unobserved objects.

**Evaluation.** Layout axis score; human plausibility rating; measured rate of
prior overriding observation, which must be near zero.

**Risk.** Medium. The scientific interest is precisely in the constraint: this is
the opposite of the usual generative framing, and the constraint is what makes it
honest.

### R7 — Coverage-aware evaluation

**Question.** How should a reconstruction be scored when the imagery covers only
part of it?

**Approach.** Formalise coverage; measure how score reliability varies with it;
develop a coverage-conditioned confidence; study what a per-viewpoint metric
systematically cannot see.

**Evaluation.** Score stability under varying photo counts and placements;
correlation with human judgement at each coverage level.

**Risk.** Low. Novel, useful, and directly extends the `measured=False` design.

### R8 — Cross-modal grounding: plan, photograph and BIM

**Question.** How should conflicts between a floor plan, photographs and a BIM
model be resolved?

**Approach.** Formal conflict taxonomy; evidence-weighted resolution using the
provenance model; propagate residual conflict as uncertainty rather than picking
a winner.

**Evaluation.** Resolution accuracy against known-conflicting corpora; downstream
score.

**Risk.** Medium. Requires paired plan/photo/BIM data, which is scarce — and
producing it is itself a contribution.

### R9 — Human-in-the-loop reconstruction

**Question.** What is the optimal division of labour between an automatic
reconstruction loop and a human editor?

**Approach.** Instrument the review step; measure which corrections humans make,
how long they take, and how much they improve the score; learn which findings to
route to a human rather than to the optimiser.

**Evaluation.** Score per human-minute; comparison against fully automatic and
fully manual baselines.

**Risk.** Low. Needs users, which the product provides.

**Commercially the most valuable direction**, because it directly answers "how
much human time does a good result cost".

---

## 10. Datasets

### 10.1 Public datasets to build on

| Dataset | Content | Use | Limitation |
| --- | --- | --- | --- |
| **ScanNet / ScanNet++** | 1,500+ RGB-D indoor scans with instance labels | ground-truth geometry and placement | scans, not photographs; no floor plans |
| **Matterport3D** | 90 buildings, panoramas, meshes | multi-room, multi-floor | commercial licence; capture style ≠ photography |
| **Replica** | 18 photorealistic reconstructions | appearance and lighting ground truth | small |
| **Hypersim** | 77k photorealistic renders with full ground truth | material and lighting supervision | synthetic |
| **3D-FRONT / 3D-FUTURE** | 18k synthetic rooms, 16k furniture models | layout priors, asset matching | synthetic; furniture-catalogue distribution |
| **Structured3D** | 3.5k synthetic scenes with plans | **plan + photo pairs — the closest to our task** | synthetic |
| **CubiCasa5K / FloorPlanCAD** | 5k annotated floor plans | plan parsing | plans only |
| **IFC open BIM corpora** | real building models | BIM interop testing | no photographs |
| **Objaverse-XL** | 10M+ 3D objects | asset retrieval | quality varies enormously |

**The gap is stark and is the opportunity:** no public dataset pairs a *real* floor
plan with *real* photographs of the finished interior and ground-truth
measurements. Every dataset above has two of the three at most.

### 10.2 ArchX3D-Interiors — the dataset to publish

**The single most valuable research output available to this project.**

| Property | Target |
| --- | --- |
| Buildings | 200 |
| Rooms | 1,500 |
| Levels | multi-floor for 60 buildings |
| Photographs | 15,000 (8–15 per room), casual and professional |
| Floor plans | DXF, as-built, dimensionally verified |
| BIM | IFC for 50 buildings |
| Ground truth | laser-measured room dimensions; furniture positions to ±2 cm; spectrophotometer surface colours; lux and CCT readings |
| Annotations | instance segmentation, materials, styles, furniture with catalogue links |
| Diversity | 12 countries, 6 building types, all price points, occupied and staged |

**Why the ground truth is the contribution.** Anyone can collect photographs.
Laser-measured furniture positions and spectrophotometer colour readings are what
turn a corpus into a benchmark, and they are why no such dataset exists.

**Licensing.** CC BY-NC-SA 4.0 for research; a commercial tier funds collection.
Explicit consent from every occupant; a face and personal-effect redaction pass;
a documented takedown process. Interior photographs of real homes carry genuine
privacy obligations, and the dataset's credibility depends on handling them
visibly well.

**Cost.** ~£180k over 18 months: field teams, instrumentation, annotation,
legal and hosting. Realistically grant-funded or partner-funded (§17).

**Staging.** Release in three tranches (50 / 100 / 200 buildings) so it is useful
early and the protocol can be corrected before the expensive majority is
collected.

### 10.3 Synthetic corpus

`ArchX3D-Synth`: procedurally generated buildings with perfect ground truth,
rendered under controlled conditions — varying photo count, camera placement,
lighting, clutter and occlusion.

Its purpose is not to substitute for real data. It is to enable **controlled
ablation**: measure how score varies with photo count when *nothing else*
changes, which is impossible with real buildings. Free to produce, infinitely
scalable, and honest about being synthetic.

---

## 11. Evaluation protocol

Publishing a protocol is what turns a metric into a benchmark. This is the
proposal.

### 11.1 Tasks

| Task | Input | Output | Primary metric |
| --- | --- | --- | --- |
| **T1 Shell reconstruction** | plan | geometry | IoU vs. ground truth |
| **T2 Furnished reconstruction** | plan + photographs | full scene | five-axis composite |
| **T3 Appearance recovery** | plan + geometry + photographs | materials, palette, lighting | colour + material + lighting |
| **T4 Layout recovery** | plan + photographs + object list | placements | mean placement error, mean orientation error |
| **T5 Closed-loop refinement** | an initial reconstruction + budget | improved reconstruction | score gain per unit cost |
| **T6 Coverage robustness** | T2 with `k ∈ {1,2,4,8,16}` photos | full scene | score vs. `k` |

T5 and T6 are the ones nobody else offers, and they are the ones that follow
directly from this system's design.

### 11.2 Metrics

**Primary** — the five axes, weighted as `DEFAULT_WEIGHTS`, reported with
`weight_used` and `confidence`, and **never** reported as a bare number without
its coverage.

**Secondary**
- Placement: mean and 90th-percentile position error (m), orientation error (°).
- Detection: precision, recall and F1 at the accept threshold, per category.
- Appearance: ΔE₀₀ against spectrophotometer readings; roughness and metallic
  error where measurable.
- Lighting: illuminance error (lux), CCT error (K).
- Perceptual: LPIPS, DISTS, FID between render and reference.
- Human: pairwise preference against a reference reconstruction.
- Cost: wall-clock seconds, GPU-seconds, model tokens, USD.

**Reported alongside, always**
- Coverage: fraction of floor area and of surface area observed.
- Unmeasured axes and why.
- Confidence.

### 11.3 Rules

1. **Ground truth is never an input.** Enforced by the harness, not by trust.
2. **A submission is a container** that runs offline against a held-out split.
3. **Compute and cost are reported** and are part of the leaderboard. A method
   that wins by spending 100× belongs in a different column.
4. **Determinism is required.** Two runs must agree within a stated tolerance.
5. **The held-out split is never released.** A public validation split exists for
   development.
6. **Human evaluation is run centrally** on a fixed protocol, so it is comparable.

### 11.4 Baselines

Published with the benchmark, so a first submission has something to beat:

- Empty shell from the plan alone.
- Style-prior furnishing with no observation.
- Single-image reconstruction (the current state of the art).
- ArchX3D v2 without refinement.
- ArchX3D v2 with refinement.
- A human expert given 30 / 60 / 120 minutes.

The human baselines are the honest ones and the most useful: they say what the
automatic system is actually competing with.

---

## 12. Human studies

Four studies. Each with a pre-registered protocol, IRB or equivalent ethics
approval, and published data.

### S1 — Metric validation *(prerequisite for everything)*

**Question.** Do the five axes predict human judgement of reconstruction fidelity?

**Design.** 200 participants (100 architecture/design professionals, 100
laypeople). Pairwise comparison of reconstructions against reference
photographs. 40 pairs each, balanced, randomised.

**Analysis.** Correlation of each axis and of the composite with preference;
learn empirical weights and compare with `DEFAULT_WEIGHTS`; test whether
professionals and laypeople weight differently.

**Output.** Validated weights, or evidence that the current ones are wrong — both
publishable, and the second more useful.

### S2 — What people actually notice

**Question.** Which reconstruction errors do people notice, and in what order?

**Design.** Eye-tracking plus think-aloud, 40 participants, controlled error
injection (one error class at a time, at graded magnitudes).

**Analysis.** Detection rate and time-to-notice per error class and magnitude.

**Output.** An empirically-grounded severity function, replacing the current
hand-set severity mapping. Directly improves the planner's ranking.

### S3 — Human-in-the-loop efficiency

**Question.** How much human time buys how much fidelity, and where is it best
spent?

**Design.** 60 participants, three conditions: manual editing, automatic
refinement, and human + automatic. Instrumented review sessions.

**Analysis.** Score per human-minute; which correction types have the highest
return; which findings are better routed to a human than to the optimiser.

**Output.** Both a research result and a product decision about what the review UI
should surface first.

### S4 — Professional acceptability

**Question.** What fidelity threshold makes a reconstruction usable for a real
professional task?

**Design.** 30 architects and interior designers, realistic tasks (client
presentation, material scheduling, spatial planning), reconstructions at graded
fidelity.

**Analysis.** Task success and self-reported acceptability versus score.

**Output.** A defensible answer to "is it good enough", which is the question
every commercial conversation eventually reaches and which no current metric
answers.

---

## 13. Publication plan

Realistic, sequenced, with the dependency chain explicit.

| # | Paper | Venue | Timing | Depends on |
| --- | --- | --- | --- | --- |
| P1 | **Attributable reconstruction metrics for interior scenes** | CVPR / ECCV | v2 + 6 mo | R1, S1 |
| P2 | **ArchX3D-Interiors: a plan-and-photograph benchmark with measured ground truth** | NeurIPS D&B / CVPR | v3 | dataset tranche 2 |
| P3 | **Closed-loop refinement of interior reconstruction under measurement** | SIGGRAPH / ICCV | v3 + 6 mo | R2, P1 |
| P4 | **Floor-plan-constrained metric grounding from sparse photographs** | 3DV / WACV | v3 | R3 |
| P5 | **Confidence calibration for VLM-driven scene reconstruction** | EMNLP / NeurIPS | v3 | R4 |
| P6 | **Coverage-aware evaluation: what a sparse photo set cannot tell you** | CVPR / TPAMI | v4 | R7, P2 |
| P7 | **Observation-constrained layout priors** | SIGGRAPH Asia / Eurographics | v4 | R6 |
| P8 | **Differentiable appearance recovery for reconstructed interiors** | SIGGRAPH / EGSR | v4/v5 | R5, P4 |
| P9 | **What people notice in an imperfect reconstruction** | CHI / UIST | v4 | S2, S3 |
| P10 | **ArchX3D: an open platform for measurable interior reconstruction** | JOSS / SoftwareX | v4 | the platform |

### Sequencing rationale

**P1 first, always.** Every other paper cites the metric. Publishing the
benchmark (P2) or the loop (P3) before the metric is validated invites the
reviewer question that sinks both: *"why should we believe your score?"*

**P2 is the highest-impact paper**, because a dataset that fills a real gap is
cited for a decade and a method paper is cited for two years. It is also the most
expensive, which is why it is grant-funded.

**P10 costs almost nothing** and is worth doing: a JOSS paper makes the software
citable, which materially increases adoption in academia.

### Norms

Preprint on arXiv at submission. Code and data released with the paper. Every
reported number reproducible from the published run record. Negative results
published — including where the loop fails to converge and where the metric
disagrees with humans.

---

## 14. Intellectual property

### Patentable candidates

Assessed honestly, including the ones that probably are not.

| Candidate | Novelty | Value | Recommendation |
| --- | --- | --- | --- |
| Closed-loop refinement with subsystem attribution and measured acceptance | plausibly high | high | **File.** The strongest candidate. |
| Three-digest surgical render-cache invalidation for scene-derived images | moderate | moderate | **Defensive publication.** More valuable as prior art than as a patent. |
| Coverage-aware scoring that excludes unmeasured axes from normalisation | moderate | moderate | **Publish.** Adoption is worth more than exclusivity. |
| Plan-constrained metric grounding from single images | moderate | high | **File** if R3 delivers. |
| Provenance-weighted conflict resolution across plan, photo and BIM | plausibly high | moderate | **File** if R8 delivers. |
| Operation algebra with materialised inverses | low — well-trodden | low | **No.** Prior art is extensive. |

### Strategy

**Defensive, not offensive.** Patents exist to prevent a large incumbent
patenting the same idea and excluding us. Explicit commitments:

- **A non-assertion pledge for open-source and research use**, published, binding.
- Membership of a patent non-aggression pool (OIN or equivalent).
- Never assert against a research use, ever.
- No patent on anything the open-source core depends on.

The last point is the one that keeps the open-source project credible. A core
that depends on a patent the company can revoke is not open source in any sense
that matters.

### Trade secrets

Almost nothing. The architecture is public, the algorithms are published, the
code is open. The defensible assets are the **dataset**, the **held-out benchmark
split**, operational know-how, and the **ecosystem**. That is a deliberate
position: a strategy that depends on secrecy is incompatible with being an
open-source research platform, and the platform is worth more.

---

# Part III — Strategy

## 15. Open-source strategy

### Licence

| Component | Licence | Reason |
| --- | --- | --- |
| `archx3d-core`, `archx3d`, `archx3d-blender` | **Apache-2.0** | permissive drives adoption; the explicit patent grant matters for a project with patents |
| Web frontend, desktop shell | **Apache-2.0** | |
| Plugin SDK, contract suites | **Apache-2.0** | must be trivially adoptable |
| Cloud control plane, billing, admin | **proprietary** | the commercial layer |
| Enterprise connectors, SSO/SCIM, audit export | **proprietary** | |
| `ArchX3D-Interiors` dataset | **CC BY-NC-SA 4.0** | research-free, commercial-paid |
| `ArchX3D-Synth` | **CC BY 4.0** | free; costs nothing to produce |
| Documentation | **CC BY 4.0** | |

**Apache-2.0 over AGPL.** AGPL would prevent a cloud provider running a
competing hosted service. It would also prevent adoption by the architecture
firms and software vendors who are the intended integrators, and it would make
the plugin ecosystem legally awkward. Adoption is the strategy; the moat is the
dataset, the benchmark and the operational service, none of which a licence
protects anyway.

### The open-core line

Stated precisely, because a vague line erodes:

> **Everything needed to reconstruct, evaluate, refine, edit and export a scene
> is open source, forever, and works fully offline with no account.**
>
> Proprietary code exists only for *operating a multi-tenant service* — billing,
> quotas, tenant administration, enterprise identity, support tooling.

Consequences, accepted deliberately:

- A competitor can self-host the full reconstruction pipeline. Fine.
- A user can run everything locally with local models and never pay. Fine — and
  it is a market.
- No feature is ever moved from open to proprietary. Ever. That single act
  destroys the trust the strategy depends on, and it has destroyed other
  projects.

### Community

| Phase | Goal | Actions |
| --- | --- | --- |
| **v2 — Foundation** | be usable by outsiders | packaging, docs, `CONTRIBUTING.md`, `good first issue` labels, responsive review, public roadmap |
| **v3 — Contributors** | 20 external contributors | RFC process, public design discussion, contributor recognition, monthly community call |
| **v4 — Ecosystem** | 50 plugins | plugin SDK, templates, tutorials, a plugin grant programme, hackathons |
| **v5 — Independence** | governance beyond one company | technical steering committee, foundation evaluation, elected maintainers |

**Commitments made now and honoured regardless**: public roadmap; design in the
open via RFC; issues triaged within 5 working days; PRs reviewed within 10; a
published security policy with a disclosure window; release notes that credit
contributors; no surprise licence changes.

---

## 16. Commercial strategy

### Segments

| Segment | Need | Product | Price |
| --- | --- | --- | --- |
| **Individual designers** | fast client visuals | Cloud Pro | £30/mo |
| **Small studios (2–20)** | collaboration, brand assets | Cloud Team | £25/seat/mo |
| **Large practices (20+)** | BIM interop, standards, SSO | Enterprise | contract |
| **Real estate** | volume listing visualisation | API, usage-priced | per reconstruction |
| **Furniture retail** | product placement in customer rooms | API + catalogue plugin | per reconstruction + licence |
| **Software vendors** | embedding | OEM licence | contract |
| **Academia** | research | free | £0 |

### Revenue mix at maturity

```
Enterprise contracts   ████████████████████████  45%
Cloud subscriptions    ██████████████            27%
API / usage            ████████                  15%
Marketplace share      ████                       8%
Dataset licensing      ███                        5%
```

### Pricing principles

- **The open-source version is genuinely sufficient.** Paying buys convenience,
  scale and support — never capability.
- **Usage pricing tracks cost.** A reconstruction has a real marginal cost;
  pricing that ignores it produces either loss-making users or arbitrary caps.
- **Academic use is free** and generously so. It produces citations, contributors
  and hires.
- **No per-project fees.** Nothing that makes a user hesitate to try something.

### The commercial argument for the research programme

The benchmark and the dataset are not philanthropy. They are how ArchX3D becomes
the reference point that every competitor is measured against, which is a durable
position no feature is. A vendor whose fidelity claim must be stated in someone
else's metric has already lost the argument.

---

## 17. Industry partnerships

Ordered by what ArchX3D actually needs.

| Partner type | We need | They get | Example targets |
| --- | --- | --- | --- |
| **Architecture practices** | real plans, photographs, ground truth, workflow feedback | early access, influence, free enterprise tier during the programme | mid-size practices with a research appetite |
| **Furniture manufacturers** | catalogues with dimensions and materials | their products placed in reconstructions; a retail channel | IKEA, Herman Miller, Vitra, catalogue aggregators |
| **BIM vendors** | interop testing, IFC edge cases | reconstruction as a feature | Autodesk, Graphisoft, Nemetschek, Trimble |
| **Cloud providers** | GPU credits for the research programme | a reference workload, a case study | AWS/GCP/Azure research programmes |
| **Model providers** | early access, rate limits, pricing | a demanding vertical benchmark and public evaluation | Google, OpenAI, Anthropic, Mistral |
| **Universities** | students, rigour, grants | a platform, a dataset, publications | groups in vision, graphics and architectural computing |
| **Real estate platforms** | volume, distribution | automated listing visualisation | portals, virtual-tour vendors |
| **Standards bodies** | interop correctness, credibility | an open reference implementation | buildingSMART (IFC), Khronos (glTF), AOUSD (USD) |

### The three that matter most

1. **Architecture practices with the dataset programme.** They are the only source
   of real plan + photograph + ground-truth triples. Without them, R1, R3, R7 and
   the benchmark are all built on synthetic data, and the whole research programme
   is weaker. Structure it as a funded partnership with co-authorship, not as data
   extraction.

2. **buildingSMART.** IFC certification is the difference between "we can read
   IFC" and "we are a BIM tool". It is slow, procedural, and worth it.

3. **One furniture manufacturer with a real catalogue.** Asset retrieval against
   a dimensioned, materialled, real catalogue is a step change over procedural
   assets, and it is the most direct commercial validation available.

---

## 18. Governance

### Now → v3: benevolent dictatorship, documented

A core team decides. Every decision is public, every architectural decision has
an ADR, and the roadmap is open. This is honest and appropriate at this size; the
failure mode is pretending otherwise.

### v3 → v5: technical steering committee

| Aspect | Rule |
| --- | --- |
| Composition | 5–7 members; at least 2 from outside the sponsoring company by v4 |
| Selection | maintainers elect; 2-year terms, staggered |
| Remit | architecture, breaking changes, principles, roadmap priorities |
| Process | RFC → public comment (14 days minimum) → decision → ADR |
| Escalation | lazy consensus; then a vote; the chair breaks ties |

### v5+: foundation

Evaluate transferring the core to a neutral foundation (Linux Foundation, Apache,
or a domain body). The test is whether the project's health has become
independent of one company's commercial fortunes. If it has, the transfer
protects it. If it has not, the transfer is theatre.

### What is never delegated

Some things stay with the maintainers regardless of governance structure, because
they are the project's identity rather than its direction:

- The twelve engineering principles. Amendable, by the documented process, but not
  by a partner's commercial preference.
- The open-core line (§15).
- Security disclosure handling.
- The benchmark's held-out split.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the target this roadmap builds toward, and the seven defects it closes.
- [`SCENE_GRAPH_SPEC.md`](SCENE_GRAPH_SPEC.md) — v2's largest single piece of work.
- [`PLUGIN_SPEC.md`](PLUGIN_SPEC.md) — v4's foundation.
- [`API_SPEC.md`](API_SPEC.md) — v3's public surface.
- [`PERFORMANCE_GUIDE.md`](PERFORMANCE_GUIDE.md) — the stage roadmap this release plan implements.
- [`ENGINEERING_PRINCIPLES.md`](ENGINEERING_PRINCIPLES.md) — what does not change across any of it.
