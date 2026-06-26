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
3. **Step 3: Blender 3D Generation** (`modules/blender_generator.py`)
4. **Step 4: Video Stitching** (`modules/video_stitcher.py`)

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

### 2. Running the API Server
Start the FastAPI bridge server to connect with your web frontend:
```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```
**Endpoints:**
- `POST /api/generate` — Accepts a `.dxf` file, processes it, and returns paths to the generated assets.
- `GET /output/{filename}` — Serves static output files like `model.glb` or `walkthrough.mp4`.

## Outputs
All generated content is saved to the following directories:
- `data/` — Contains intermediate JSON files (`geometry.json`, `styling.json`).
- `output/` — Contains the final deliverables: `model.glb`, `scene.blend`, and `walkthrough.mp4`.
