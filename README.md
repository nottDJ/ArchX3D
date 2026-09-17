# ArchX3D

**ArchX3D** converts 2D architectural DXF floor plans into 3D GLB models and
walkthrough videos. The geometry engine is deterministic and runs entirely on
the CPU: **no API key is needed to reconstruct a building**. AI is an optional
enrichment layer that adds furniture, finishes and lighting from reference
photographs — it never decides where a wall is.

## Features
- **Deterministic CAD reconstruction** (`modules/recon/`): resolves the
  drawing's units from its own evidence, classifies every entity as building
  or documentation, reconstructs wall systems with measured thicknesses,
  separates the sheet into structures, derives rooms from wall topology, and
  finds doors and windows as real openings. Offline, reproducible, and
  validated before anything is built.
- **Buildings and storeys from the drawing's own words**: two plans on one
  sheet become two buildings or two storeys of one building only on the
  evidence of plan titles, layer level tokens and block designations — and are
  flagged as ambiguous, not guessed at, when the drawing does not settle it.
- **Validation before 3D**: a reconstruction that does not describe a building
  is refused with diagnostics rather than exported as a distorted model, and
  the exported GLB is checked back against the model it came from
  (`modules/glb_validate.py`).
- **Measured against ground truth**: a 79-drawing corpus with exact truth
  (`tests/corpus/`) holds the engine to a floor per dataset group on openings,
  walls, rooms, footprint and scale. Current numbers:
  [`docs/CORPUS_REPORT.md`](docs/CORPUS_REPORT.md).
- **Diagnostics bundle**: per-stage SVG and JSON for every run, so a bad
  result can be traced to the stage that produced it.
- **Automated Blender 3D generation**: a direct extrusion of the validated 2D
  model — walls at their own thicknesses, floors on the real footprint,
  openings cut as holes — plus lighting, materials and GLB export.
- **Optional vision enrichment**: with reference photographs, Gemini produces a
  scene graph of furniture, finishes and luminaires.
- **FastAPI bridge server** and an installable **Windows desktop app**.

## Pipeline Architecture
The system is orchestrated by `main.py`, which sequences the following stages:

1. **Step 1: DXF Reconstruction** (`modules/recon/`) — deterministic, CPU-only,
   and the only step whose failure stops the build:

       DXF -> read + classify -> units -> opening evidence -> walls
           -> structures -> openings -> rooms -> buildings and storeys
           -> evidence bridge -> validate -> building.json

2. **Step 2: Scene Analysis** (`modules/scene_analyzer.py`) [Optional, needs a key]
3. **Step 3: Blender 3D Generation** (`modules/blender_generator.py` +
   `modules/blender_build.py`) — also renders evaluation previews (`modules/render/`)
4. **Step 4: Video Stitching** (`modules/video_stitcher.py`)
5. **Step 5: Reconstruction Evaluation** (`modules/evaluation/`) [Optional, `--evaluate`]
6. **Step 6: Planning & Optimisation** (`modules/planner/`, `modules/optimizer/`) [Optional, `--refine`]

Step 1 is documented in [`docs/RECONSTRUCTION.md`](docs/RECONSTRUCTION.md),
and measured in [`docs/CORPUS_REPORT.md`](docs/CORPUS_REPORT.md).

## Prerequisites
- Python 3.9+
- **Blender 5.0** installed on your system. 
  *(Ensure the path in `main.py` under `BLENDER_EXECUTABLE_PATH` matches your Blender installation path. Default is `C:\Program Files\Blender Foundation\Blender 5.0\blender.exe`)*.
- **GEMINI_API_KEY** environment variable — **optional**. Needed only for
  vision-based furnishing and styling. Without it the pipeline still produces
  a complete, geometrically correct building; run `--offline` to state that
  intent explicitly.

## Installation

1. Install required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

## Desktop application (Windows)

An installable app that bundles the frontend and the whole Python pipeline —
**no Python, Node or terminal required** to run it. Blender is detected rather
than bundled, and must be installed separately.

```powershell
.\desktop\build.ps1
# -> desktop/src-tauri/target/release/bundle/nsis/ArchX3D_2.0.0_x64-setup.exe
```

Projects are written to `%LOCALAPPDATA%\ArchX3D\` and survive uninstalling.
See [`docs/DESKTOP.md`](docs/DESKTOP.md) for the architecture, build
prerequisites and troubleshooting.

## Usage

### 1. Running the CLI Pipeline
Run the full pipeline directly on a DXF file from the terminal:
```bash
python main.py path/to/your_file.dxf
```

**Options:**
- `--offline`: Deterministic CPU-only build — no vision, no styling, no
  network. The geometry engine never needed a key; this says so out loud.
- `--skip-styling`: Bypass the Gemini AI material generation for a faster, unstyled export.
- `--skip-render`: Skip rendering animation frames and stitching a video, exporting only the GLB model and Blend file.
- `--scale`: Metres per DXF unit, overriding the engine's own unit resolution.
  Use only when the drawing's evidence is genuinely wrong.
- `--diagnostics DIR`: Where to write the per-stage diagnostics bundle
  (default `output/diagnostics`; pass `''` to disable).
- `--evaluate`: Score the reconstruction against the reference photographs and write `output/evaluation/`.
- `--refine`: Plan improvements from the evaluation and run the optimisation loop (implies `--evaluate`; budget minutes).

The reconstruction can also be run on its own, with no Blender and no config:

```bash
python -m modules.recon.pipeline plan.dxf building.json --diagnostics diag/
```

### 2. Running the API Server
Start the FastAPI bridge server to connect with your web frontend:
```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```
**Endpoints:**

*One-shot pipeline*
- `POST /api/generate` — Accepts a `.dxf` file, processes it, and returns paths to the generated assets.
- `GET /output/{filename}` — Serves static output files like `model.glb` or `walkthrough.mp4`.

*Wizard (upload → analyse → review and edit → generate)*
- `POST /api/projects` — Create a project from a DXF.
- `POST /api/projects/{id}/images` — Attach reference images.
- `POST /api/projects/{id}/analyse` — Run the vision pipeline.
- `GET  /api/projects/{id}/review` — Everything the review step renders.
- `POST /api/projects/{id}/edits` — Apply the user's corrections.
- `POST /api/projects/{id}/validate` — Deterministic re-check of the edited graph (no AI).
- `POST /api/projects/{id}/generate` — Build the Blender scene from the reviewed graph.
- `GET  /api/projects/{id}/model.glb` — Download the result.

### 3. Running the web app
```bash
cd web && npm install && npm run dev
```

### 4. Evaluation previews
Deterministic low-resolution renders — one per stored viewpoint — used to score
the reconstruction against the reference photographs. They are produced
automatically after generation, and can be re-run incrementally against an
existing `output/scene.blend`:
```bash
python modules/render/preview.py                  # whole building
python modules/render/preview.py --room room_1    # one room
python modules/render/preview.py --force          # ignore the cache
```
Only the previews affected by a change are re-rendered; a fully cached pass
costs milliseconds. Each viewpoint also emits albedo, depth, normal and ID
passes for the evaluation engine. See
[`docs/RENDER_PIPELINE.md`](docs/RENDER_PIPELINE.md).

### 5. Evaluating the reconstruction
Scores the build against the reference photographs and says **which subsystem
to change** — not just how similar it is:
```bash
python main.py plan.dxf --images reference_images/ --evaluate
python modules/evaluation/engine.py            # re-run on an existing build
```
Five axes (colour, material, lighting, layout, objects) produce findings like
*"walnut floor appears too desaturated → MaterialSpecies"* and
*"coffee table sits 38 cm from where the reference places it →
SceneGraphTransform"*. Writes `output/evaluation/` — four JSON documents plus
an HTML report with reference, render and difference overlay side by side. The
engine never modifies the scene graph. See
[`docs/EVALUATION.md`](docs/EVALUATION.md).

### 6. Refining the reconstruction
Plans changes from the evaluation's findings and executes them, keeping only
what measurably improves the score:
```bash
python modules/optimizer/pipeline.py --dry-run     # see the plan, change nothing
python modules/optimizer/pipeline.py               # run it
python main.py plan.dxf --images reference_images/ --refine
```
Three lighting complaints about one room become **one** `LightingAdjustment`
rather than three edits. Each action is applied, re-rendered, re-evaluated, and
rolled back unless the score actually rose. DXF geometry, doors, windows and
locked objects are never touched, and no model of any kind is called. Writes
`output/refinement/` — the plan, every attempt including the rejected ones, and
the metrics. See [`docs/REFINEMENT.md`](docs/REFINEMENT.md).

## Documentation

### Platform specifications
The definitive technical specification for where ArchX3D is going. Written for
contributors; normative where they disagree with the current code.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — The target architecture: layering, package hierarchy, dependency rules, the 22 ports, lifecycle, threading, storage, caching, distributed execution, rendering backends, AI providers, frontend, testing, versioning, and the decision log.
- [`docs/SCENE_GRAPH_SPEC.md`](docs/SCENE_GRAPH_SPEC.md) — Scene Graph v2: entity–component model, identity, the Level abstraction, the operation algebra, journal, snapshots, collaboration, spatial indexing, queries, streaming, IFC alignment, and migration.
- [`docs/PLUGIN_SPEC.md`](docs/PLUGIN_SPEC.md) — The plugin contract: extension points, manifest, lifecycle, discovery, versioning, dependency resolution, capabilities, sandboxing, security, and marketplace readiness.
- [`docs/API_SPEC.md`](docs/API_SPEC.md) — REST, GraphQL, WebSocket, SDK and CLI: resource model, auth, operation-based mutation, errors, rate limits, versioning and deprecation.
- [`docs/PERFORMANCE_GUIDE.md`](docs/PERFORMANCE_GUIDE.md) — Where the time goes, the ranked bottleneck list with expected speedups, budgets, and what must never be optimised.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — v2 through v5, plus the research programme, datasets, evaluation protocol, publications, and the open-source and commercial strategy.
- [`docs/ENGINEERING_PRINCIPLES.md`](docs/ENGINEERING_PRINCIPLES.md) — The twelve rules that do not change when the roadmap does.
- [`docs/DESIGN_GUIDELINES.md`](docs/DESIGN_GUIDELINES.md) — House style, module shape, naming, types, tests, review checklist, and the anti-patterns this system invites.

### Subsystems
How the code works today.

- [`docs/RECONSTRUCTION.md`](docs/RECONSTRUCTION.md) — The deterministic DXF engine: the intermediate representation, unit resolution, entity classification, wall reconstruction, structures, openings, topology, buildings and storeys, validation and diagnostics — and why each stage is shaped the way it is.
- [`docs/CORPUS_REPORT.md`](docs/CORPUS_REPORT.md) — What the engine measures on 79 drawings with exact ground truth, per group and per drawing. Generated by `tools/corpus/evaluate.py`; [`tests/corpus/README.md`](tests/corpus/README.md) says where each drawing came from, what is real in it, and why each release threshold is what it is.
- [`docs/VIEWER.md`](docs/VIEWER.md) — The interactive architectural viewer: camera modes, roof detection, BVH collision, view modes, room navigation, GLB metadata and performance.
- [`docs/DESKTOP.md`](docs/DESKTOP.md) — The installable Windows app: how the frontend, the frozen Python backend and the Tauri shell fit together, how to build it, and where user data lives.

### Frontend and design
- [`docs/UI_GUIDELINES.md`](docs/UI_GUIDELINES.md) — The UX audit that motivated the redesign, ranked by severity, plus the rules for hierarchy, layout, colour, copy, states and motion.
- [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md) — Every token: OKLCH colour ramps, typography, spacing, radius, elevation, motion, and how light and dark are derived.
- [`docs/COMPONENT_LIBRARY.md`](docs/COMPONENT_LIBRARY.md) — All 25 components, their variants, and when *not* to use them.
- [`docs/ACCESSIBILITY.md`](docs/ACCESSIBILITY.md) — WCAG 2.2 AA conformance, measured contrast in both themes, how to test, and the known gaps.
- [`docs/FRONTEND_ARCHITECTURE.md`](docs/FRONTEND_ARCHITECTURE.md) — Structure, state, data flow, bundle budgets, and an honest code-quality review.
- [`docs/VISION_PIPELINE.md`](docs/VISION_PIPELINE.md) — How reference images become a scene graph.
- [`docs/MULTI_IMAGE.md`](docs/MULTI_IMAGE.md) — Room segmentation, image routing, and multi-image fusion.
- [`docs/REGISTRATION.md`](docs/REGISTRATION.md) — Fitting reference sheets to the drawing from their room labels: robust transform fitting, composite-sheet detection, and why the CAD outranks the imagery.
- [`docs/EDITOR.md`](docs/EDITOR.md) — The object editor, the edit API, and incremental validation.
- [`docs/FIDELITY.md`](docs/FIDELITY.md) — Style, materials, palette, lighting, and reference-vs-generated similarity scoring.
- [`docs/APPEARANCE.md`](docs/APPEARANCE.md) — How Blender consumes that appearance data: procedural materials, palette bounds, lighting rig, viewpoint cameras.
- [`docs/RENDER_PIPELINE.md`](docs/RENDER_PIPELINE.md) — Deterministic preview renders: scene hashing, caching, incremental and parallel scheduling, auxiliary render passes, the manifest the evaluation engine consumes.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — The reconstruction evaluation engine: five axes, findings that name the subsystem to change, scoring that excludes what it could not measure.
- [`docs/REFINEMENT.md`](docs/REFINEMENT.md) — Planning and optimisation: findings into ranked actions, a dependency graph, and a loop that keeps only what it can measure an improvement from.

## Testing
```bash
python -m pytest tests/ -q     # pipeline, editor, validation
cd web && npm test             # editor document, history, snapping, alignment
cd web && npm run typecheck
```

The reconstruction engine is also *measured*, not only asserted on. The corpus
gates run with the rest of the suite and fail the build with the number that
moved; remeasure and rewrite the report with:

```bash
python -m pytest tests/test_corpus_metrics.py -q
python tools/corpus/evaluate.py
```

## Outputs
All generated content is saved to the following directories:
- `data/` — Intermediate JSON. `building.json` is the validated 2D building
  model and the authority for the 3D build; `geometry.json` is projected from
  it for the vision, furnishing and viewer stages; `styling.json` is legacy.
- `output/diagnostics/` — Per-stage evidence for the run: `entities.json`,
  `units.json`, `walls.json`, `rooms.json`, `doors.json`, `windows.json`,
  `levels.json`, `validation.json`, and the SVG series `debug_raw`,
  `debug_normalized`, `debug_walls`, `debug_rooms`, `debug_openings`,
  `debug_topology`, `debug_structures`, `reconstruction`. Written on failure as
  well as on success.
- `output/` — Contains the final deliverables: `model.glb`, `scene.blend`, and `walkthrough.mp4`.
- `output/preview/` — Evaluation renders (`<room>/viewpoint_NN.png`, auxiliary passes, `manifest.json`). Diagnostics, not deliverables.
- `output/evaluation/` — Scores, findings and the HTML report (`evaluation.json`, `per_viewpoint.json`, `per_room.json`, `building_summary.json`, `report.html`).
- `output/refinement/` — The action plan and what came of it (`planner_report.json`, `optimization_history.json`, `metrics.json`).
