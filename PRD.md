# Product Requirements Document (PRD) — ArchX3D

## 1. Product Vision & Overview
**ArchX3D** is an automated, AI-driven pipeline designed to bridge the gap between 2D architectural drafting and 3D visualization. The product seamlessly converts 2D DXF floor plans into fully styled, interactive 3D GLB models and cinematic walkthrough videos. By combining programmatic geometry extraction, generative AI for styling, and Blender for 3D extrusion and rendering, ArchX3D eliminates hours of manual 3D modeling work for architects and designers.

## 2. Target Audience
- **Architects & Draftspersons**: Looking to quickly visualize 2D CAD layouts in 3D without manual modeling.
- **Real Estate Developers & Agents**: Needing rapid generation of 3D walkthroughs for property marketing.
- **Game Developers & 3D Artists**: Seeking a procedural foundation to convert 2D layouts into 3D environments.

## 3. Core Features & Capabilities
### 3.1. Automated Geometry Extraction
- **Input:** Standard `.dxf` CAD files.
- **Process:** Parses DXF files to identify structural elements (e.g., WALLS, DOORS) based on layer names.
- **Output:** A structured intermediate JSON (`geometry.json`) containing normalized coordinates, scales, and segment metadata.

### 3.2. AI-Driven Material & Style Generation
- **Input:** `geometry.json` containing spatial data.
- **Process:** Integrates with **Google Gemini AI** to procedurally infer room types, suggest material properties (e.g., "matte white paint", "hardwood flooring"), and apply aesthetic context.
- **Output:** A stylistic blueprint (`styling.json`) to guide the 3D generation.

### 3.3. Procedural 3D Generation
- **Process:** A headless **Blender 5.0** Python script reads the geometry and styling data, automatically extruding walls, generating floors/ceilings, applying materials, and setting up lighting (HDRI or point lights).
- **Output:** An optimized `.glb` 3D model ready for web viewing, and a native `.blend` scene file.

### 3.4. Cinematic Walkthrough Rendering (Optional)
- **Process:** Blender automates camera path generation through the interior space, rendering a sequence of frames.
- **Output:** A compiled MP4 video walkthrough (`walkthrough.mp4`) showcasing the interior.

### 3.5. Web Integration & API Bridge
- **Process:** A FastAPI backend server handles `.dxf` uploads via a RESTful endpoint (`POST /api/generate`), isolates the Blender pipeline using subprocess execution, and serves the static output assets.
- **Output:** Enables seamless integration with modern web frontends (e.g., Next.js, React).

## 4. Architecture & Technical Stack
- **Backend Orchestrator**: Python 3.9+
- **API Server**: FastAPI (Uvicorn, Python-Multipart)
- **CAD Parsing**: `ezdxf`
- **Generative AI**: `google-generativeai` (Gemini API)
- **3D Engine**: Blender 5.0 (Headless via `bpy`)
- **Video Processing**: OpenCV (`opencv-python`)
- **Frontend Interoperability**: REST API + CORS enabled for `localhost:3000` (Next.js/React standard).

## 5. System Workflows

### 5.1. CLI Workflow
1. User executes `python main.py plan.dxf --layers "WALLS"`.
2. Pipeline runs `dxf_extractor.py` → `style_generator.py` → `blender_generator.py` → `video_stitcher.py`.
3. Assets are deposited in the `/output` folder.

### 5.2. Web/API Workflow
1. Frontend application sends a `multipart/form-data` POST request to `/api/generate` containing the DXF file.
2. Server saves the file uniquely to `/uploads`.
3. Server spawns `main.py` as a detached subprocess with a 15-minute timeout.
4. Server polls for completion and responds with a JSON payload containing static URLs (e.g., `/output/model.glb`).
5. Frontend renders the `.glb` file using a web viewer (e.g., Three.js).

## 6. System Requirements
- **OS**: Windows, macOS, or Linux (requires Blender executable compatibility).
- **Dependencies**: 
  - Blender 5.0 binary path configured in `main.py` (`BLENDER_EXECUTABLE_PATH`).
  - Active internet connection for Gemini API calls.
  - Adequate RAM (16GB+) for heavy Blender boolean operations and rendering.

## 7. Future Milestones (V2 & Beyond)
- **Furniture Generation**: Automatically populating rooms with basic generic furniture assets based on inferred room type.
- **Multi-Story Support**: Parsing DXF files with multiple elevation layers to generate stacked multi-story buildings.
- **WebSocket Progress Updates**: Replacing the blocking HTTP API with WebSockets to stream real-time pipeline status (e.g., "Extruding walls...", "Rendering frame 45/300") to the frontend.
- **Texture Baking**: Baking lighting and shadows into the GLB export for highly performant, photorealistic web viewing without client-side lighting calculations.
