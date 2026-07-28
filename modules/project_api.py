"""
ArchX3D — Project API
=====================
Backend for the generation wizard: create a project, attach a DXF and any
number of reference images, run analysis, review the result, apply edits, and
generate.

Job model
---------
Analysis takes tens of seconds cold, so it runs on a background thread and the
client polls. This is an **in-process** registry, not a durable queue: jobs are
lost on restart and it does not span workers. That is the right size for a
local desktop tool, and the seam is deliberate — `JobRegistry` is the only
thing a Celery/Redis backend would need to replace.

Mounted by ``server.py``; kept in its own module so the FastAPI surface stays
readable and this logic stays testable without an HTTP client.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES_DIR = os.path.join(BASE_DIR, "modules")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

#: Ceiling on a single upload, so a stray file cannot exhaust the disk.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DXF_BYTES = 120 * 1024 * 1024
MAX_IMAGES_PER_PROJECT = 12


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    project_id: str
    kind: str  # analyse | generate
    status: str = "QUEUED"
    message: str = ""
    #: Ordered log, consumed by the progress UI.
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    #: Structured output the job wants to hand back, e.g. the pre-build
    #: validation report. Kept apart from ``events``, which is a human log.
    result: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def emit(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        self.events.append({"status": status, "message": message, "at": time.time()})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "events": self.events,
            "error": self.error,
            "result": self.result,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class JobRegistry:
    """Thread-safe in-memory job store."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, project_id: str, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], project_id=project_id, kind=kind)
        job.emit("QUEUED", f"{kind} queued")
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def for_project(self, project_id: str) -> List[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.project_id == project_id]


JOBS = JobRegistry()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def project_dir(project_id: str) -> str:
    return os.path.join(PROJECTS_DIR, project_id)


def _safe_project_id(project_id: str) -> str:
    """Reject anything that could escape the projects directory."""
    cleaned = "".join(c for c in project_id if c.isalnum() or c in "-_")
    if not cleaned or cleaned != project_id:
        raise ValueError("invalid project id")
    return cleaned


def create_project() -> Dict[str, Any]:
    project_id = uuid.uuid4().hex[:12]
    root = project_dir(project_id)
    os.makedirs(os.path.join(root, "images"), exist_ok=True)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    os.makedirs(os.path.join(root, "output"), exist_ok=True)

    manifest = {
        "project_id": project_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dxf": None,
        "images": [],
        "stage": "created",
    }
    _write_manifest(project_id, manifest)
    return manifest


def load_manifest(project_id: str) -> Dict[str, Any]:
    project_id = _safe_project_id(project_id)
    path = os.path.join(project_dir(project_id), "manifest.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"project {project_id} not found")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_manifest(project_id: str, manifest: Dict[str, Any]) -> None:
    path = os.path.join(project_dir(project_id), "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def attach_dxf(project_id: str, filename: str, contents: bytes) -> Dict[str, Any]:
    """Store the project's floor plan. Exactly one per project."""
    manifest = load_manifest(project_id)

    if not filename.lower().endswith(".dxf"):
        raise ValueError("only .dxf files are accepted as the floor plan")
    if len(contents) > MAX_DXF_BYTES:
        raise ValueError(f"DXF exceeds the {MAX_DXF_BYTES // (1024*1024)} MB limit")
    if not contents:
        raise ValueError("the uploaded DXF is empty")

    safe_name = os.path.basename(filename).replace(" ", "_")
    path = os.path.join(project_dir(project_id), safe_name)
    with open(path, "wb") as fh:
        fh.write(contents)

    manifest["dxf"] = {"filename": safe_name, "bytes": len(contents)}
    manifest["stage"] = "dxf_uploaded"
    _write_manifest(project_id, manifest)
    return manifest


def attach_images(project_id: str, uploads: List[tuple]) -> Dict[str, Any]:
    """Add reference images. Called once per multi-select or drop.

    ``uploads`` is a list of ``(filename, bytes)``. Rejected files are reported
    rather than silently skipped, so the UI can tell the user which and why.
    """
    manifest = load_manifest(project_id)
    images_dir = os.path.join(project_dir(project_id), "images")
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []

    existing = {i["filename"] for i in manifest.get("images", [])}

    for filename, contents in uploads:
        base = os.path.basename(filename).replace(" ", "_")
        extension = os.path.splitext(base)[1].lower()

        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            rejected.append({"filename": base, "reason": f"unsupported type {extension or '?'}"})
            continue
        if len(contents) > MAX_IMAGE_BYTES:
            rejected.append({"filename": base, "reason": "larger than 25 MB"})
            continue
        if not contents:
            rejected.append({"filename": base, "reason": "file is empty"})
            continue
        if len(existing) + len(accepted) >= MAX_IMAGES_PER_PROJECT:
            rejected.append({"filename": base, "reason":
                             f"project limit of {MAX_IMAGES_PER_PROJECT} images reached"})
            continue

        # De-duplicate names so two "render.jpg" uploads do not overwrite.
        name = base
        counter = 1
        while name in existing or os.path.exists(os.path.join(images_dir, name)):
            stem, ext = os.path.splitext(base)
            name = f"{stem}_{counter}{ext}"
            counter += 1

        with open(os.path.join(images_dir, name), "wb") as fh:
            fh.write(contents)
        accepted.append({"filename": name, "bytes": len(contents)})
        existing.add(name)

    manifest.setdefault("images", []).extend(accepted)
    if accepted:
        manifest["stage"] = "images_uploaded"
    _write_manifest(project_id, manifest)

    return {"manifest": manifest, "accepted": accepted, "rejected": rejected}


def remove_image(project_id: str, filename: str) -> Dict[str, Any]:
    manifest = load_manifest(project_id)
    safe = os.path.basename(filename)
    path = os.path.join(project_dir(project_id), "images", safe)
    if os.path.exists(path):
        os.remove(path)
    manifest["images"] = [i for i in manifest.get("images", []) if i["filename"] != safe]
    _write_manifest(project_id, manifest)
    return manifest


def delete_project(project_id: str) -> None:
    project_id = _safe_project_id(project_id)
    shutil.rmtree(project_dir(project_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def start_analysis(project_id: str, options: Optional[Dict[str, Any]] = None) -> Job:
    """Kick off extraction + vision analysis on a background thread."""
    manifest = load_manifest(project_id)
    if not manifest.get("dxf"):
        raise ValueError("upload a DXF floor plan first")

    job = JOBS.create(project_id, "analyse")
    thread = threading.Thread(
        target=_run_analysis, args=(job, project_id, options or {}), daemon=True
    )
    thread.start()
    return job


def _run_analysis(job: Job, project_id: str, options: Dict[str, Any]) -> None:
    root = project_dir(project_id)
    manifest = load_manifest(project_id)

    geometry_path = os.path.join(root, "data", "geometry.json")
    graph_path = os.path.join(root, "data", "scene_graph.json")
    review_path = os.path.join(root, "data", "review.json")
    dxf_path = os.path.join(root, manifest["dxf"]["filename"])

    try:
        # --- 1. DXF extraction --------------------------------------------
        job.emit("EXTRACTING_DXF", "Parsing DXF layers and geometry...")
        extract = subprocess.run(
            [sys.executable, os.path.join(MODULES_DIR, "dxf_extractor.py"),
             dxf_path, geometry_path,
             options.get("layers", "WALLS"), str(options.get("scale", 1.0)), "16"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if extract.returncode != 0 or not os.path.exists(geometry_path):
            raise RuntimeError(f"DXF extraction failed: {_tail(extract.stderr)}")

        with open(geometry_path, "r", encoding="utf-8") as fh:
            segments = len(json.load(fh).get("walls", []))
        job.emit("EXTRACTING_DXF", f"Extracted {segments} wall segments.")

        # --- 2. Vision analysis -------------------------------------------
        images_dir = os.path.join(root, "images")
        has_images = bool(manifest.get("images"))

        if not has_images:
            job.emit("ANALYSING", "No reference images; building the unfurnished shell.")
            _write_empty_review(graph_path, review_path, geometry_path, options)
        else:
            job.emit("ANALYSING",
                     f"Analysing {len(manifest['images'])} reference image(s)...")
            command = [
                sys.executable, os.path.join(MODULES_DIR, "scene_analyzer.py"),
                geometry_path, graph_path,
                "--images", images_dir,
                "--review-out", review_path,
                "--wall-height", str(options.get("wall_height", 3.0)),
            ]
            if options.get("model"):
                command += ["--model", options["model"]]
            if options.get("single_room"):
                command.append("--single-room")

            analysis = subprocess.run(
                command, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
            # Exit code 2 means "no usable observations" — a degraded result,
            # not a failure; the shell is still valid and worth reviewing.
            if analysis.returncode not in (0, 2):
                raise RuntimeError(f"Scene analysis failed: {_tail(analysis.stderr)}")
            for line in (analysis.stdout or "").splitlines():
                if line.startswith("[VISION]"):
                    job.emit("ANALYSING", line.replace("[VISION] ", ""))

        manifest["stage"] = "analysed"
        _write_manifest(project_id, manifest)
        job.emit("READY_FOR_REVIEW", "Analysis complete. Review the detections.")
        job.finished_at = time.time()

    except subprocess.TimeoutExpired:
        job.error = "Analysis timed out."
        job.emit("FAILED", job.error)
        job.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        job.error = str(exc)
        job.emit("FAILED", str(exc))
        job.finished_at = time.time()


def _write_empty_review(graph_path, review_path, geometry_path, options) -> None:
    """Produce a reviewable graph for a DXF-only project."""
    sys.path.insert(0, MODULES_DIR)
    from vision import pipeline, review as review_mod

    with open(geometry_path, "r", encoding="utf-8") as fh:
        geometry = json.load(fh)

    config = pipeline.PipelineConfig(wall_height=options.get("wall_height", 3.0))
    result = pipeline.analyse([], geometry, config, log=lambda *a: None)
    result.graph.save(graph_path)
    with open(review_path, "w", encoding="utf-8") as fh:
        json.dump(review_mod.build_review(result.graph), fh, indent=2)


def get_review(project_id: str) -> Dict[str, Any]:
    """Serve the review payload, rebuilding it if it predates the current shape.

    ``review.json`` is a cache of ``build_review``. A project analysed before a
    field was added would otherwise serve a payload the UI cannot render, so a
    stale one is regenerated from the scene graph rather than returned as-is.
    """
    root = project_dir(_safe_project_id(project_id))
    path = os.path.join(root, "data", "review.json")
    if not os.path.exists(path):
        raise FileNotFoundError("run analysis before requesting the review")

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if "vocabulary" in payload:
        return payload

    graph_path = os.path.join(root, "data", "scene_graph.json")
    if not os.path.exists(graph_path):
        raise FileNotFoundError("re-run analysis; this project's review is out of date")

    sys.path.insert(0, MODULES_DIR)
    from vision import review as review_mod
    from vision.schema import SceneGraph

    payload = review_mod.build_review(SceneGraph.load(graph_path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return payload


def apply_review_edits(project_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the user's review decisions and refresh the review payload."""
    sys.path.insert(0, MODULES_DIR)
    from vision import review as review_mod
    from vision.schema import SceneGraph

    root = project_dir(_safe_project_id(project_id))
    graph_path = os.path.join(root, "data", "scene_graph.json")
    review_path = os.path.join(root, "data", "review.json")

    if not os.path.exists(graph_path):
        raise FileNotFoundError("run analysis before applying edits")

    graph = SceneGraph.load(graph_path)
    updated, report = review_mod.apply_edits(graph, edits)
    updated.save(graph_path)

    payload = review_mod.build_review(updated)
    with open(review_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    # Every edit is followed by a deterministic re-check so the user sees the
    # consequences of what they just did, not of what the analysis produced.
    # Report-only: nothing here overrules a manual placement.
    return {
        "report": report.to_dict(),
        "review": payload,
        "validation": recheck_project(project_id),
    }


def recheck_project(
    project_id: str,
    *,
    apply_corrections: bool = False,
    respect_user_edits: bool = True,
) -> Dict[str, Any]:
    """Re-validate the edited scene graph without re-running any model.

    Cheap enough to call after every edit: the checks are geometric predicates
    over the graph, with no network access and no VLM.
    """
    sys.path.insert(0, MODULES_DIR)
    from vision import recheck as recheck_mod
    from vision.schema import SceneGraph

    root = project_dir(_safe_project_id(project_id))
    graph_path = os.path.join(root, "data", "scene_graph.json")
    if not os.path.exists(graph_path):
        raise FileNotFoundError("run analysis before validating")

    graph = SceneGraph.load(graph_path)
    report = recheck_mod.recheck(
        graph,
        apply_corrections=apply_corrections,
        respect_user_edits=respect_user_edits,
    )

    if apply_corrections:
        from vision import review as review_mod

        graph.save(graph_path)
        review_path = os.path.join(root, "data", "review.json")
        with open(review_path, "w", encoding="utf-8") as fh:
            json.dump(review_mod.build_review(graph), fh, indent=2)

    return report.to_dict()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def start_generation(project_id: str, options: Optional[Dict[str, Any]] = None) -> Job:
    """Build the Blender scene from the reviewed scene graph."""
    manifest = load_manifest(project_id)
    graph_path = os.path.join(project_dir(project_id), "data", "scene_graph.json")
    if not os.path.exists(graph_path):
        raise ValueError("analyse the project before generating")

    job = JOBS.create(project_id, "generate")
    thread = threading.Thread(
        target=_run_generation, args=(job, project_id, options or {}), daemon=True
    )
    thread.start()
    return job


def _run_generation(job: Job, project_id: str, options: Dict[str, Any]) -> None:
    """Run main.py against the project's own data and output directories."""
    root = project_dir(project_id)
    manifest = load_manifest(project_id)

    try:
        job.emit("GENERATING_GEOMETRY", "Preparing the reviewed scene graph...")

        # The last deterministic gate before render time. It reports and does
        # not correct: reaching this point means the user looked at the scene
        # and chose it, so the build follows their decision and records what
        # was questionable rather than quietly rearranging their work.
        try:
            validation = recheck_project(project_id)
            job.result["validation"] = validation
            if validation["total_issues"]:
                job.emit(
                    "GENERATING_GEOMETRY",
                    f"Pre-build check: {validation['errors']} error(s), "
                    f"{validation['warnings']} warning(s); building as reviewed",
                )
        except Exception as error:  # never block a build on the checker itself
            job.emit("GENERATING_GEOMETRY", f"Pre-build check skipped: {error}")

        # main.py reads and writes the repo-level data/ and output/ folders, so
        # the project's files are staged in and the results copied back out.
        # Crude, but it avoids reworking the CLI's path handling for the API.
        repo_data = os.path.join(BASE_DIR, "data")
        repo_output = os.path.join(BASE_DIR, "output")
        os.makedirs(repo_data, exist_ok=True)

        for name in ("geometry.json", "scene_graph.json"):
            source = os.path.join(root, "data", name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(repo_data, name))

        job.emit("BUILDING_SCENE", "Building geometry, materials and lighting in Blender...")
        command = [
            sys.executable, os.path.join(BASE_DIR, "main.py"),
            os.path.join(root, manifest["dxf"]["filename"]),
            # Build from the graph the user just reviewed. --skip-vision would
            # be wrong here: it treats an existing graph as stale and deletes
            # it, discarding every edit made during review.
            "--use-scene-graph",
        ]
        if options.get("skip_render", True):
            command.append("--skip-render")

        result = subprocess.run(
            command, cwd=BASE_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Blender generation failed: {_tail(result.stderr)}")

        job.emit("EXPORTING_GLB", "Exporting the web model...")
        produced = []
        for name in ("model.glb", "scene.blend", "walkthrough.mp4"):
            source = os.path.join(repo_output, name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(root, "output", name))
                produced.append(name)

        if "model.glb" not in produced:
            raise RuntimeError("Blender finished but produced no model.glb")

        manifest["stage"] = "generated"
        manifest["outputs"] = produced
        _write_manifest(project_id, manifest)

        job.emit("COMPLETED", f"Generation complete ({', '.join(produced)}).")
        job.finished_at = time.time()

    except subprocess.TimeoutExpired:
        job.error = "Generation timed out."
        job.emit("FAILED", job.error)
        job.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        job.emit("FAILED", str(exc))
        job.finished_at = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail(text: Optional[str], lines: int = 8) -> str:
    if not text:
        return "no output"
    return " | ".join(text.strip().splitlines()[-lines:])
