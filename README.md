# ArchX3D

**ArchX3D** is an automated pipeline that converts 2D DXF floor plans into 3D GLB models and walkthrough videos using Python, Blender, and Gemini AI.

## Features
- **DXF Geometry Extraction**: Parses raw CAD floor plans to extract meaningful wall segments and structural layouts.
- **Generative AI Styling**: Uses Gemini AI to procedurally dictate materials and styles based on the floor plan context.
- **Automated Blender 3D Generation**: Extrudes 2D geometry into 3D objects, sets up lighting, applies materials, and exports to GLB format automatically.
- **FastAPI Bridge Server**: Provides a RESTful API to accept DXF uploads, trigger the background generation pipeline, and serve resulting 3D assets to a frontend (e.g., Next.js).

## Pipeline Architecture
The system is orchestrated by `main.py`, which sequences the following stages:
1. **Step 1: DXF Extraction** (`modules/dxf_extractor.py`)
2. **Step 2: AI Style Generation** (`modules/style_generator.py`) [Optional]
3. **Step 3: Blender 3D Generation** (`modules/blender_generator.py`) — also renders evaluation previews (`modules/render/`)
4. **Step 4: Video Stitching** (`modules/video_stitcher.py`)
5. **Step 5: Reconstruction Evaluation** (`modules/evaluation/`) [Optional, `--evaluate`]

## Prerequisites
- Python 3.9+
- **Blender 5.0** installed on your system. 
  *(Ensure the path in `main.py` under `BLENDER_EXECUTABLE_PATH` matches your Blender installation path. Default is `C:\Program Files\Blender Foundation\Blender 5.0\blender.exe`)*.
- **GEMINI_API_KEY** environment variable set (for AI styling).

## Installation

1. Install required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### 1. Running the CLI Pipeline
Run the full pipeline directly on a DXF file from the terminal:
```bash
python main.py path/to/your_file.dxf
```

**Options:**
- `--skip-styling`: Bypass the Gemini AI material generation for a faster, unstyled export.
- `--skip-render`: Skip rendering animation frames and stitching a video, exporting only the GLB model and Blend file.
- `--layers`: Define specific layer names to extract (e.g., `--layers "WALLS,DOORS"`).
- `--evaluate`: Score the reconstruction against the reference photographs and write `output/evaluation/`.

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

## Documentation
- [`docs/VISION_PIPELINE.md`](docs/VISION_PIPELINE.md) — How reference images become a scene graph.
- [`docs/MULTI_IMAGE.md`](docs/MULTI_IMAGE.md) — Room segmentation, image routing, and multi-image fusion.
- [`docs/EDITOR.md`](docs/EDITOR.md) — The object editor, the edit API, and incremental validation.
- [`docs/FIDELITY.md`](docs/FIDELITY.md) — Style, materials, palette, lighting, and reference-vs-generated similarity scoring.
- [`docs/APPEARANCE.md`](docs/APPEARANCE.md) — How Blender consumes that appearance data: procedural materials, palette bounds, lighting rig, viewpoint cameras.
- [`docs/RENDER_PIPELINE.md`](docs/RENDER_PIPELINE.md) — Deterministic preview renders: scene hashing, caching, incremental and parallel scheduling, auxiliary render passes, the manifest the evaluation engine consumes.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — The reconstruction evaluation engine: five axes, findings that name the subsystem to change, scoring that excludes what it could not measure.

## Testing
```bash
python -m pytest tests/ -q     # pipeline, editor, validation
cd web && npm test             # editor document, history, snapping, alignment
cd web && npm run typecheck
```

## Outputs
All generated content is saved to the following directories:
- `data/` — Contains intermediate JSON files (`geometry.json`, `styling.json`).
- `output/` — Contains the final deliverables: `model.glb`, `scene.blend`, and `walkthrough.mp4`.
- `output/preview/` — Evaluation renders (`<room>/viewpoint_NN.png`, auxiliary passes, `manifest.json`). Diagnostics, not deliverables.
- `output/evaluation/` — Scores, findings and the HTML report (`evaluation.json`, `per_viewpoint.json`, `per_room.json`, `building_summary.json`, `report.html`).
