"""
ArchX3D — frozen backend entry point
====================================
The single executable the desktop app ships. It is two things at once:

    archx3d-backend.exe                        -> serve the FastAPI app
    archx3d-backend.exe --child <module> [...] -> run one pipeline stage

Why one binary and not seven
----------------------------
The pipeline is a chain of subprocesses (see ``modules/child_process.py``).
Freezing each stage separately would mean seven copies of numpy, ezdxf, OpenCV
and the rest — hundreds of megabytes of duplication — and seven builds to keep
in step. One binary that can re-invoke itself as any stage costs one copy and
one build.

``--child`` is deliberately not a subcommand name a user would guess: it is an
internal calling convention between our own processes, not a public CLI. The
public CLI is still ``main.py``, which the frozen build reaches through
``--child main``.
"""

from __future__ import annotations

import os
import runpy
import sys


def _bundle_root() -> str:
    """Directory holding the app's data files.

    Frozen: PyInstaller's extraction directory (``sys._MEIPASS``), which is
    where ``config.json`` and the ``modules`` tree are unpacked. Source: the
    repository root.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prepare_paths() -> str:
    """Make the bundled packages importable and return the working root.

    The pipeline modules import each other as top-level names (``from cad.reader
    import read``, ``import project_api``), because as loose scripts they run
    with ``modules/`` on ``sys.path``. Recreating that inside the bundle keeps
    every one of those imports working unmodified.
    """
    root = _bundle_root()
    modules = os.path.join(root, "modules")
    for entry in (modules, root):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root


def _run_child(module: str, argv: list) -> int:
    """Run one pipeline stage's ``__main__`` with the supplied argv.

    ``sys.argv[0]`` is rewritten to the module name so any usage text the stage
    prints names something meaningful rather than the backend executable.
    """
    sys.argv = [module, *argv]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exit_status:  # argparse and explicit sys.exit()
        code = exit_status.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _serve(root: str) -> int:
    """Run the FastAPI app.

    Host and port come from the environment so the Tauri shell can pick a free
    port and tell the backend which one it chose, rather than both sides
    hard-coding 8000 and failing when something else already has it.
    """
    import uvicorn

    # server.py resolves data/, output/, uploads/ and projects/ relative to its
    # own location, which inside the bundle is a read-only temp directory. The
    # launcher passes a writable location; without it the first upload fails.
    workdir = os.environ.get("ARCHX3D_WORKDIR")
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        os.chdir(workdir)

    host = os.environ.get("ARCHX3D_HOST", "127.0.0.1")
    port = int(os.environ.get("ARCHX3D_PORT", "8000"))

    import server  # noqa: F401  (imported for its `app`)

    uvicorn.run(server.app, host=host, port=port, log_level="info")
    return 0


def main() -> int:
    root = _prepare_paths()

    argv = sys.argv[1:]
    if argv and argv[0] == "--child":
        if len(argv) < 2:
            print("--child requires a module name", file=sys.stderr)
            return 2
        return _run_child(argv[1], argv[2:])

    return _serve(root)


if __name__ == "__main__":
    sys.exit(main())
