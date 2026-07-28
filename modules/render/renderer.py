"""
ArchX3D — Render settings and the Blender boundary
==================================================
Two things live here: the *specification* of a deterministic preview render,
and the machinery that runs one in a background Blender.

Determinism is the whole point
------------------------------
A similarity score is only meaningful if re-rendering an unchanged scene
produces unchanged pixels. Otherwise "the wall got 3% warmer" and "the sampler
rolled differently" are the same signal, and every regression test is a coin
toss. Blender's defaults are tuned for interactive work and are stochastic in
several places, so every one of them is pinned:

======================  ==========  ===================================
Setting                 Value       Why
======================  ==========  ===================================
engine                  EEVEE       Rasterised: no path-tracing noise at all.
taa_render_samples      16          Fixed count; TAA is deterministic given
                                    a fixed count and no reprojection.
taa_reprojection        off         Reprojection carries state between frames,
                                    making a render depend on what preceded it.
raytracing (EEVEE Next) off         Screen-space tracing is temporally
                                    accumulated and denoised; both are
                                    history-dependent.
shadow jitter           off         Jittered soft shadows are stochastic.
motion blur             off         Samples across time; irrelevant to a
                                    still and expensive.
frame                   1 (fixed)   The walkthrough orbit is animated, so the
                                    current frame changes light and object
                                    positions. Always render the same frame.
view_transform          Standard    Filmic/AgX tone-map, and *which* one is
                                    the default changed between Blender
                                    versions. Standard is stable across them.
look / exposure / gamma None/0/1    Any of these silently rescale every pixel.
curve mapping           off         Same, and it is per-file state.
white balance           off         Introduced in 4.5; defaults could drift.
display device          sRGB        The manifest promises sRGB PNGs.
dither                  0.0         Blender dithers 8-bit output by default,
                                    adding ±1 LSB noise to flat surfaces —
                                    exactly the areas the colour axis reads.
filter_size             1.5         Explicit, since it is per-scene state that
                                    the generated .blend could carry.
compositing/sequencer   off         A stray node tree in the .blend must not
                                    be able to alter an evaluation image.
resolution_percentage   100         Per-scene state; 50% would halve the size.
stamp metadata          all off     Date and RenderTime are wall-clock and are
                                    written into PNG text chunks even with
                                    burn-in off — enough to make two identical
                                    renders differ in bytes.
seed (Cycles)           0           For the optional Cycles path.
denoising (Cycles)      off         Denoisers are the least reproducible part
                                    of any renderer.
======================  ==========  ===================================

Measured: two separate Blender processes rendering the same scene produce
byte-identical PNGs. What determinism does *not* cover is a different Blender
build, GPU driver or platform, any of which may differ by a least-significant
bit. The contract is "same machine, same Blender, same inputs, same pixels",
which is what a regression test and a refinement loop actually need. The settings fingerprint folds the
pipeline version in, so a change to this table re-renders everything once.

Why a subprocess
----------------
The ``.blend`` produced by ``blender_generator`` is loaded and rendered as-is:
generation is never repeated. Blender is started with ``--factory-startup`` so
a user's preferences, add-ons or colour-management overrides cannot reach an
evaluation render, and ``--background`` so no window opens.

Process startup dominates: ~3 s to start Blender and compile the scene's
shaders, against ~250 ms per 640x360 frame afterwards. That is why the job file
describes a *batch* — one launch renders every viewpoint in it, so a five-view
building costs ``startup + 5 x 250 ms`` rather than five times the lot.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import cache as cache_mod
from . import passes as passes_mod
from .scheduler import Batch, RenderOutcome

#: Re-exported by the package as ``render.RENDER_PIPELINE_VERSION``. It lives
#: here, beside the settings table it describes, so there is one value to bump
#: rather than two that can drift apart.
PIPELINE_VERSION = "1.0"

#: The in-Blender half of the pipeline, run via ``--python``.
BLENDER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_blender_render.py")

#: Where ``main.py`` expects Blender on this project's reference machine.
_WINDOWS_DEFAULT = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class RenderSettings:
    """Everything that decides what a preview looks like.

    A plain dataclass with no Blender import, so the settings can be built,
    fingerprinted and tested outside Blender; ``_blender_render`` applies them
    on the other side of the process boundary.
    """

    #: EEVEE or CYCLES. EEVEE is the default and the only one that meets the
    #: performance targets; Cycles exists for the occasional reference render.
    engine: str = "EEVEE"
    #: Fixed sample count. 16 is enough for flat architectural surfaces and
    #: fast enough to render a room in well under the 300 ms budget.
    samples: int = 16

    width: int = 640
    height: int = 360
    #: Derive the height from the viewpoint's aspect ratio instead of using
    #: ``height`` verbatim. On by default because a photograph's viewpoint
    #: rendered into a different aspect shows scene the photograph never
    #: contained, and the similarity engine would score that as a difference.
    match_aspect: bool = True

    #: Alpha instead of the world background — useful for masking experiments.
    transparent: bool = False

    #: Colour management, pinned. See the table in the module docstring.
    view_transform: str = "Standard"
    look: str = "None"
    exposure: float = 0.0
    gamma: float = 1.0

    #: Output encoding.
    file_format: str = "PNG"
    color_depth: str = "8"
    compression: int = 15
    dither: float = 0.0

    #: Anti-aliasing filter width, in pixels.
    filter_size: float = 1.5
    #: Which frame to stand on; the orbit animation makes this matter.
    frame: int = 1
    #: Keep Blender's evaluated data between frames in one batch. A cache, not
    #: a sampling change: it makes the second and later renders in a batch
    #: markedly faster without touching the result.
    persistent_data: bool = True
    #: EEVEE Next screen-space ray tracing. Off: history-dependent.
    raytracing: bool = False
    #: Ambient occlusion (EEVEE legacy only; EEVEE Next folds it into tracing).
    ambient_occlusion: bool = True

    #: Auxiliary passes rendered beside the beauty image, for the evaluation
    #: engine. Each costs one extra render of the same scene (~250 ms), which
    #: is why they are a list rather than a flag: a run that only needs colour
    #: can ask for ``albedo`` alone. See :mod:`render.passes`.
    passes: Tuple[str, ...] = passes_mod.DEFAULT_PASSES
    #: Metres that map onto the depth pass's full 0–255 range.
    depth_range: float = passes_mod.DEFAULT_DEPTH_RANGE

    def resolution_for(self, aspect: float) -> "tuple[int, int]":
        """Pixel dimensions for a viewpoint of this aspect ratio.

        Width is held fixed so every preview costs the same; only the height
        moves. Rounded to an even number because some encoders and half-res
        pipelines dislike odd dimensions, and it costs nothing.
        """
        if not self.match_aspect or not aspect or aspect <= 0:
            return int(self.width), int(self.height)
        height = int(round(self.width / aspect))
        height = max(2, height + (height % 2))
        return int(self.width), height

    def fingerprint(self) -> str:
        """Digest of every setting plus the pipeline version.

        Folded into every cache key: changing the sample count or the view
        transform changes every pixel of every preview, so it should — exactly
        once — re-render the whole building.
        """
        return cache_mod.digest({"pipeline": PIPELINE_VERSION, **self.to_dict()})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "RenderSettings":
        """Build from a config block, ignoring keys we do not know.

        Tolerant on purpose: ``config.json`` is user-edited, and an unknown or
        misspelled key should not stop a preview pass.
        """
        defaults = RenderSettings()
        if not d:
            return defaults
        known = {f: getattr(defaults, f) for f in defaults.to_dict()}
        values: Dict[str, Any] = {}
        for key, fallback in known.items():
            raw = d.get(key, fallback)
            # The pass list is the one field that is neither scalar nor
            # free-form; it has its own validator, which also accepts the
            # "albedo,depth" spelling a config file is likely to use.
            if key == "passes":
                values[key] = passes_mod.normalise(raw)
                continue
            try:
                if isinstance(fallback, bool):
                    values[key] = bool(raw)
                elif isinstance(fallback, int):
                    values[key] = int(raw)
                elif isinstance(fallback, float):
                    values[key] = float(raw)
                else:
                    values[key] = str(raw)
            except (TypeError, ValueError):
                values[key] = fallback
        return RenderSettings(**values)


# ---------------------------------------------------------------------------
# Locating Blender
# ---------------------------------------------------------------------------


def blender_executable(explicit: str = "") -> Optional[str]:
    """Find a Blender to render with.

    In order: an explicit path, ``ARCHX3D_BLENDER``, the path ``main.py`` uses,
    ``blender`` on ``PATH``, then the usual install locations per platform. The
    caller is expected to handle ``None`` by reporting a skipped preview pass
    rather than failing the build — previews are diagnostics, and a machine
    without Blender can still run everything upstream of them.
    """
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("ARCHX3D_BLENDER"):
        candidates.append(os.environ["ARCHX3D_BLENDER"])
    candidates.append(_WINDOWS_DEFAULT)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    found = shutil.which("blender")
    if found:
        return found

    patterns = [
        r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        "/snap/bin/blender",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)  # newest version first
        for match in matches:
            if os.path.isfile(match):
                return match
    return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class SubprocessRenderer:
    """Renders a batch by launching ``blender --background`` on the .blend.

    Callable, so it satisfies ``scheduler.Executor`` directly. One instance is
    shared by every batch; it holds no per-batch state, which is what makes it
    safe to use from the threaded scheduler.
    """

    def __init__(
        self,
        blend_path: str,
        settings: RenderSettings,
        executable: str = "",
        timeout: int = 600,
        verbose: bool = False,
    ) -> None:
        self.blend_path = os.path.abspath(blend_path)
        self.settings = settings
        self.executable = blender_executable(executable)
        self.timeout = timeout
        self.verbose = verbose
        #: Blender's stdout from the last batch, kept for diagnostics.
        self.last_log = ""
        #: Object and material index maps reported by the ID passes, merged
        #: across batches. They describe the build, not a viewpoint, so the
        #: pipeline reads them once at the end and stores them in the manifest.
        self.index_maps: Dict[str, Dict[str, str]] = {}

    def available(self) -> bool:
        return bool(self.executable) and os.path.exists(self.blend_path)

    def unavailable_reason(self) -> str:
        if not self.executable:
            return ("no Blender executable found; set ARCHX3D_BLENDER or "
                    "config.preview.blender_executable")
        if not os.path.exists(self.blend_path):
            return f"generated .blend not found at {self.blend_path}"
        return ""

    # -- the executor -------------------------------------------------------

    def __call__(self, batch: Batch) -> List[RenderOutcome]:
        if not self.available():
            reason = self.unavailable_reason()
            return [
                RenderOutcome(viewpoint_id=t.viewpoint_id, ok=False, error=reason)
                for t in batch
            ]

        job_path, result_path = self._write_job(batch)
        try:
            self._run_blender(job_path)
            return self._read_results(batch, result_path)
        finally:
            for path in (job_path, result_path):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass

    # -- internals ----------------------------------------------------------

    def _write_job(self, batch: Batch) -> "tuple[str, str]":
        """Serialise the batch to a temp file Blender reads on startup.

        A file rather than command-line arguments: a batch of twenty cameras
        exceeds what is comfortable to pass as argv, and quoting rules differ
        between the platforms this has to run on.
        """
        directory = tempfile.mkdtemp(prefix="archx3d_render_")
        job_path = os.path.join(directory, "job.json")
        result_path = os.path.join(directory, "result.json")
        payload = {
            "version": PIPELINE_VERSION,
            "settings": self.settings.to_dict(),
            "result_path": result_path,
            "tasks": [task.to_dict() for task in batch],
        }
        with open(job_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return job_path, result_path

    def _command(self, job_path: str) -> List[str]:
        return [
            self.executable,
            "--background",
            # Ignore user preferences, add-ons and startup files: an
            # evaluation render must not depend on who is logged in.
            "--factory-startup",
            self.blend_path,
            "--python",
            BLENDER_SCRIPT,
            "--",
            "--job",
            job_path,
        ]

    def _run_blender(self, job_path: str) -> None:
        command = self._command(job_path)
        if self.verbose:
            print(f"[PREVIEW] {' '.join(command)}")
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Blender timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise RuntimeError(f"could not start Blender: {exc}") from exc

        self.last_log = completed.stdout or ""
        if self.verbose and self.last_log:
            for line in self.last_log.splitlines():
                if line.startswith("[PREVIEW]"):
                    print(f"  {line}")

        # A non-zero exit is reported, but the result file is still read: a
        # batch where nine of ten cameras rendered should keep the nine.
        if completed.returncode != 0 and self.verbose:
            tail = (completed.stderr or "").strip().splitlines()[-5:]
            for line in tail:
                print(f"[PREVIEW] blender: {line}")

    def _read_results(self, batch: Batch, result_path: str) -> List[RenderOutcome]:
        """Read the result file the in-Blender script wrote.

        Results come from a file rather than stdout because Blender prints
        freely to stdout — add-on chatter, GPU warnings, the render progress
        line — and parsing structure out of that is a losing game.
        """
        if not os.path.exists(result_path):
            log_tail = "; ".join(self.last_log.strip().splitlines()[-3:])
            error = "Blender produced no result file" + (f" ({log_tail})" if log_tail else "")
            return [
                RenderOutcome(viewpoint_id=t.viewpoint_id, ok=False, error=error)
                for t in batch
            ]
        try:
            with open(result_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            return [
                RenderOutcome(viewpoint_id=t.viewpoint_id, ok=False,
                              error=f"unreadable render result: {exc}")
                for t in batch
            ]

        for kind, mapping in (payload.get("index_maps") or {}).items():
            self.index_maps.setdefault(kind, {}).update(
                {str(k): str(v) for k, v in mapping.items()}
            )

        outcomes = [RenderOutcome.from_dict(r) for r in payload.get("results", [])]
        produced = {o.viewpoint_id: o for o in outcomes}
        return [
            produced.get(
                task.viewpoint_id,
                RenderOutcome(viewpoint_id=task.viewpoint_id, ok=False,
                              error="Blender did not report this viewpoint"),
            )
            for task in batch
        ]


class InlineRenderer:
    """Renders inside the Blender process that is already running.

    Used by ``blender_generator`` immediately after it builds the scene: the
    geometry, materials and cameras are already in memory, so launching a
    second Blender to load a file we just wrote would be pure waste.

    Same executor signature as :class:`SubprocessRenderer`, so the scheduler,
    cache and manifest cannot tell the difference.
    """

    def __init__(self, settings: RenderSettings) -> None:
        self.settings = settings
        self.index_maps: Dict[str, Dict[str, str]] = {}

    def available(self) -> bool:
        return "bpy" in sys.modules or _bpy_importable()

    def unavailable_reason(self) -> str:
        return "" if self.available() else "not running inside Blender"

    def __call__(self, batch: Batch) -> List[RenderOutcome]:
        from . import _blender_render

        payloads, index_maps = _blender_render.render_tasks(
            [task.to_dict() for task in batch], self.settings.to_dict()
        )
        for kind, mapping in (index_maps or {}).items():
            self.index_maps.setdefault(kind, {}).update(
                {str(k): str(v) for k, v in mapping.items()}
            )
        return [RenderOutcome.from_dict(p) for p in payloads]


def _bpy_importable() -> bool:
    try:
        import bpy  # noqa: F401
    except ImportError:
        return False
    return True
