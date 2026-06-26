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

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Paths — resolved relative to this file so it works regardless of CWD
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
PIPELINE_SCRIPT = os.path.join(BASE_DIR, "main.py")

# Ensure required directories exist on startup
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

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
# CORS — allow the Next.js dev server through
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",   # Next.js dev server
        "http://127.0.0.1:3000",  # alternate loopback
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


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["System"])
async def health_check():
    """Simple liveness probe."""
    return {"status": "ok", "service": "ArchX3D API"}


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
    cmd = [
        sys.executable,       # same Python interpreter running this server
        PIPELINE_SCRIPT,
        saved_path,
        "--skip-styling",     # skip Gemini AI styling by default for speed
    ]

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
