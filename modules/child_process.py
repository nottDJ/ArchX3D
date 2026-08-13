"""
ArchX3D — child process invocation
==================================
Builds the argv for re-invoking one of this project's own scripts.

The problem this solves
-----------------------
The pipeline is a chain of subprocesses: ``server`` spawns ``main.py``, which
spawns ``dxf_extractor.py``, ``scene_analyzer.py`` and the rest. Every call
site wrote that as ``[sys.executable, "modules/dxf_extractor.py", ...]``, which
is correct under a normal interpreter and *wrong* inside a PyInstaller bundle:
there ``sys.executable`` is the frozen executable itself, not Python, and the
``.py`` files do not exist on disk at all.

So the frozen build re-invokes itself with a ``--child <module>`` prefix, and
the bundle's entry point (``desktop/backend_main.py``) dispatches that back to
the right module's ``__main__``. Development runs are untouched and still spawn
a plain interpreter.

Keeping the mapping here — rather than at each call site — means a new pipeline
step needs one entry in ``CHILD_MODULES``, not a frozen-vs-source branch
wherever it happens to be spawned from.
"""

from __future__ import annotations

import os
import sys
from typing import List, Sequence

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Path (relative to the repo root, POSIX separators) -> importable module name
#: that the frozen bundle runs with ``runpy.run_module(..., "__main__")``.
#:
#: The nested entries keep their package prefix because ``evaluation.engine``
#: and ``optimizer.pipeline`` rely on ``__package__`` being set for their
#: relative imports — running them as bare ``engine`` / ``pipeline`` would
#: break those, and the two names would collide besides.
CHILD_MODULES = {
    "main.py": "main",
    "modules/dxf_extractor.py": "dxf_extractor",
    "modules/scene_analyzer.py": "scene_analyzer",
    "modules/style_generator.py": "style_generator",
    "modules/video_stitcher.py": "video_stitcher",
    "modules/evaluation/engine.py": "evaluation.engine",
    "modules/optimizer/pipeline.py": "optimizer.pipeline",
}


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def module_for(script_path: str) -> str:
    """The importable module name for one of this project's scripts.

    Raises rather than guessing: a script spawned in a frozen build that is not
    in ``CHILD_MODULES`` would otherwise fail deep inside the child with an
    unhelpful error, long after the cause.
    """
    relative = os.path.relpath(os.path.abspath(script_path), BASE_DIR)
    key = relative.replace(os.sep, "/")
    if key not in CHILD_MODULES:
        raise KeyError(
            f"{key!r} is spawned as a subprocess but is not registered in "
            f"child_process.CHILD_MODULES, so it cannot run in a frozen build"
        )
    return CHILD_MODULES[key]


def child_command(script_path: str, args: Sequence[str] = ()) -> List[str]:
    """Argv to run ``script_path`` as a child of the current process.

    Source runs get ``[python, script.py, ...]``; frozen runs get
    ``[archx3d-backend.exe, --child, module.name, ...]``.
    """
    if is_frozen():
        return [sys.executable, "--child", module_for(script_path), *args]
    return [sys.executable, script_path, *args]
