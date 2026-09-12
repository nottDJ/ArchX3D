"""
ArchX3D — FastAPI Server
=========================
Bridge between the Next.js frontend (localhost:3000) and the local
Python/Blender procedural generation pipeline.

Endpoints:
  POST /api/generate   — Upload a .dxf file and run the full pipeline
  GET  /output/*       — Static file access to generated assets (model.glb, etc.)

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import sys
import subprocess
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules"))
import credentials  # noqa: E402
import project_api  # noqa: E402
from child_process import child_command  # noqa: E402

# ---------------------------------------------------------------------------
# Paths — resolved relative to this file so it works regardless of CWD
# ---------------------------------------------------------------------------
from app_paths import CODE_ROOT, code_path, data_path, ensure_data_dirs  # noqa: E402

# Code and data are the same directory in a source checkout and different ones
# inside a frozen bundle; see modules/app_paths.py.
BASE_DIR = CODE_ROOT
UPLOAD_DIR = data_path("uploads")
OUTPUT_DIR = data_path("output")
PIPELINE_SCRIPT = code_path("main.py")

# Ensure required directories exist on startup
ensure_data_dirs()

# Put a saved API key into the environment before anything spawns a pipeline
# stage. Every stage reads GEMINI_API_KEY from its inherited environment, so
# doing this once here is what makes a key entered in the UI take effect
# without restarting the app or threading a credential through six subprocess
# invocations.
credentials.apply_to_environment()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ArchX3D-Server")

# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ArchX3D API",
    description="Converts 2D DXF floor plans into 3D GLB models and walkthrough videos.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# CORS — allow the Next.js dev server and the desktop shell through
# ---------------------------------------------------------------------------
#
# The desktop build is a webview, not a terminal: it enforces the same-origin
# policy, and its pages are served from Tauri's custom scheme rather than from
# this server. Without its origin here every request from the installed app
# fails CORS while the identical request from curl succeeds — which is exactly
# how this was missed until the app was opened.
#
# The server binds 127.0.0.1 only and a desktop instance spawns its own copy on
# a private port, so this list widens what a *local* page may call, not what
# the network may reach.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",    # Next.js dev server
        "http://127.0.0.1:3000",    # alternate loopback
        "http://tauri.localhost",   # desktop shell (Windows)
        "tauri://localhost",        # desktop shell (macOS/Linux)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static file serving — exposes output/ at /output/*
# e.g. http://localhost:8000/output/model.glb
# ---------------------------------------------------------------------------
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

# Per-project uploads and results, so the wizard can show thumbnails and the
# viewer can load the generated model.
os.makedirs(project_api.PROJECTS_DIR, exist_ok=True)
app.mount("/projects", StaticFiles(directory=project_api.PROJECTS_DIR), name="projects")


# ---------------------------------------------------------------------------
# Generation Wizard API
# ---------------------------------------------------------------------------
#
# Step 1  POST   /api/projects                      create + upload DXF
# Step 2  POST   /api/projects/{id}/images          upload one or more images
# Step 3  POST   /api/projects/{id}/analyse         start analysis (background)
#         GET    /api/jobs/{job_id}                 poll progress
#         GET    /api/projects/{id}/review          detections for review
#         POST   /api/projects/{id}/edits           apply the user's decisions
# Step 4  POST   /api/projects/{id}/generate        build the Blender scene
# Step 5  GET    /api/projects/{id}/model.glb       the finished model


@app.post("/api/projects", tags=["Wizard"])
async def create_project(file: UploadFile = File(...)):
    """Step 1 — create a project from a DXF floor plan."""
    manifest = project_api.create_project()
    try:
        contents = await file.read()
        manifest = project_api.attach_dxf(
            manifest["project_id"], file.filename or "plan.dxf", contents
        )
    except ValueError as exc:
        project_api.delete_project(manifest["project_id"])
        raise HTTPException(status_code=400, detail=str(exc))

    log.info(f"Project {manifest['project_id']} created from {manifest['dxf']['filename']}")
    return manifest


@app.post("/api/projects/{project_id}/images", tags=["Wizard"])
async def upload_images(project_id: str, files: List[UploadFile] = File(...)):
    """Step 2 — attach one or more reference images.

    Accepts a multi-select or a drag-and-drop of several files in one request.
    Rejected files are returned with reasons rather than dropped silently.
    """
    uploads = [(f.filename or "image.jpg", await f.read()) for f in files]
    try:
        result = project_api.attach_images(project_id, uploads)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info(f"Project {project_id}: +{len(result['accepted'])} images, "
             f"{len(result['rejected'])} rejected")
    return result


@app.delete("/api/projects/{project_id}/images/{filename}", tags=["Wizard"])
async def delete_image(project_id: str, filename: str):
    try:
        return project_api.remove_image(project_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/projects/{project_id}", tags=["Wizard"])
async def get_project(project_id: str):
    try:
        return project_api.load_manifest(project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/projects/{project_id}/analyse", tags=["Wizard"])
async def analyse_project(project_id: str, options: Optional[Dict[str, Any]] = Body(default=None)):
    """Step 3 — run DXF extraction and vision analysis in the background."""
    try:
        job = project_api.start_analysis(project_id, options or {})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job.to_dict()


@app.get("/api/jobs/{job_id}", tags=["Wizard"])
async def get_job(job_id: str):
    """Poll a background job. Used by both the analysis and generate steps."""
    job = project_api.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.get("/api/projects/{project_id}/review", tags=["Wizard"])
async def get_review(project_id: str):
    """Step 3 — everything the validation page renders."""
    try:
        return project_api.get_review(project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/projects/{project_id}/edits", tags=["Wizard"])
async def apply_edits(project_id: str, edits: Dict[str, Any] = Body(...)):
    """Step 3 — apply the user's corrections before generation."""
    try:
        return project_api.apply_review_edits(project_id, edits)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/projects/{project_id}/validate", tags=["Wizard"])
async def validate_project(
    project_id: str, options: Optional[Dict[str, Any]] = Body(default=None)
):
    """Step 3 — deterministic re-check of the edited scene graph.

    Runs no model and no network call, so it is safe to invoke after every
    edit. Report-only unless ``apply_corrections`` is set; even then a locked
    object is never moved, and a hand-edited one is only moved when
    ``respect_user_edits`` is explicitly false.
    """
    options = options or {}
    try:
        return project_api.recheck_project(
            project_id,
            apply_corrections=bool(options.get("apply_corrections", False)),
            respect_user_edits=bool(options.get("respect_user_edits", True)),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/projects/{project_id}/generate", tags=["Wizard"])
async def generate_project(project_id: str, options: Optional[Dict[str, Any]] = Body(default=None)):
    """Step 4 — build the Blender scene from the reviewed graph."""
    try:
        job = project_api.start_generation(project_id, options or {})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return job.to_dict()


@app.get("/api/projects/{project_id}/model.glb", tags=["Wizard"])
async def get_model(project_id: str):
    """Step 5 — the generated model, for the browser viewer."""
    try:
        root = project_api.project_dir(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    path = os.path.join(root, "output", "model.glb")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="model not generated yet")
    return FileResponse(path, media_type="model/gltf-binary", filename="model.glb")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["System"])
async def health_check():
    """Simple liveness probe."""
    return {"status": "ok", "service": "ArchX3D API"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
#
# The desktop app has no shell to export GEMINI_API_KEY from, so the key can be
# saved through the UI instead. See modules/credentials.py for the precedence
# rules and what this does and does not protect.
#
# The key is never sent back to the client — only whether one is configured,
# where it came from, and a masked hint identifying which key it is.


@app.get("/api/settings/api-key", tags=["System"])
async def get_api_key_status():
    """Whether a Gemini key is configured, and where it came from."""
    return credentials.status()


@app.put("/api/settings/api-key", tags=["System"])
async def put_api_key(payload: Dict[str, Any] = Body(...)):
    """Save a Gemini API key for this machine."""
    # `externally_set()`, not a live read of os.environ: the server exports the
    # effective key for its subprocesses, so os.environ is set either way once
    # a key has been saved here.
    if credentials.externally_set():
        raise HTTPException(
            status_code=409,
            detail=(
                "GEMINI_API_KEY is set in the environment and takes precedence. "
                "Unset it to manage the key from here."
            ),
        )
    try:
        credentials.save_key(payload.get("key", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info("Gemini API key saved (%s)", credentials.masked())
    return credentials.status()


@app.delete("/api/settings/api-key", tags=["System"])
async def delete_api_key():
    """Forget the saved key. Leaves an environment-supplied key alone."""
    try:
        credentials.clear_key()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("Gemini API key removed")
    return credentials.status()


# ---------------------------------------------------------------------------
# POST /api/generate — core pipeline endpoint
# ---------------------------------------------------------------------------
@app.post("/api/generate", tags=["Pipeline"])
async def generate_model(file: UploadFile = File(...)):
    """
    Accept a .dxf upload, run the ArchX3D pipeline, and return
    the path to the generated GLB model.

    Flow:
      1. Validate the uploaded file is a .dxf
      2. Save to uploads/ with a unique timestamped name
      3. Invoke `python main.py <file> --skip-styling` via subprocess
      4. Return the output paths on success, or a 500 on failure
    """

    # --- 1. Validate file extension -------------------------------------------
    if not file.filename or not file.filename.lower().endswith(".dxf"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only .dxf files are accepted.",
        )

    # --- 2. Save uploaded file with a unique timestamped name -----------------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = file.filename.replace(" ", "_")
    saved_filename = f"{timestamp}_{safe_name}"
    saved_path = os.path.join(UPLOAD_DIR, saved_filename)

    try:
        contents = await file.read()
        with open(saved_path, "wb") as f:
            f.write(contents)
        log.info(f"Saved upload: {saved_path} ({len(contents):,} bytes)")
    except Exception as e:
        log.error(f"Failed to save upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    # --- 3. Run the pipeline via subprocess -----------------------------------
    #
    # We deliberately shell out instead of importing main.py directly.
    # Blender's `bpy` module manipulates global state and can crash if loaded
    # inside a live ASGI server.  subprocess isolates the entire Blender
    # context in its own process.
    #
    cmd = child_command(
        PIPELINE_SCRIPT,
        [
            saved_path,
            "--skip-styling",  # skip Gemini AI styling by default for speed
        ],
    )

    log.info(f"Launching pipeline: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,          # run from project root so relative paths work
            capture_output=True,
            text=True,
            timeout=900,           # 15 min hard timeout (Blender renders can be heavy)
        )
    except subprocess.TimeoutExpired:
        log.error("Pipeline timed out (>900s)")
        raise HTTPException(
            status_code=500,
            detail="Pipeline timed out. The model may be too complex.",
        )
    except Exception as e:
        log.error(f"Pipeline execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Pipeline error: {e}")

    # --- 4. Evaluate result ---------------------------------------------------
    if result.returncode != 0:
        # Log the last 20 lines of stderr for debugging
        stderr_tail = "\n".join(result.stderr.strip().split("\n")[-20:])
        log.error(f"Pipeline FAILED (exit {result.returncode}):\n{stderr_tail}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "Pipeline failed. Check server logs for details.",
                "stderr": stderr_tail,
            },
        )

    # Pipeline succeeded — log stdout summary
    if result.stdout:
        for line in result.stdout.strip().split("\n")[-10:]:
            log.info(f"  pipeline | {line}")

    # Build response with all available output paths
    output_glb = "/output/model.glb"
    output_video = "/output/walkthrough.mp4"

    response_data = {
        "status": "success",
        "message": "Generation complete",
        "output_glb": output_glb,
    }

    # Conditionally include video path if it was generated
    video_path = os.path.join(OUTPUT_DIR, "walkthrough.mp4")
    if os.path.exists(video_path):
        response_data["output_video"] = output_video

    log.info(f"Pipeline complete — GLB ready at {output_glb}")
    return response_data


# ---------------------------------------------------------------------------
# Entry point (alternative to `uvicorn server:app`)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
