"""
ArchX3D — code root vs data root
=================================
Where the program *is*, and where the program *writes*.

Running from a checkout these are the same directory, which is why the rest of
the codebase derives both from ``__file__`` and nothing has ever needed to tell
them apart. Inside a PyInstaller bundle they are emphatically not the same:

    code root   sys._MEIPASS — a temp directory, read-only in practice, and
                deleted when the process exits. Holds modules/ and config.json.
    data root   a writable per-user location. Holds data/, output/, projects/
                and uploads/, and must survive the process.

Writing outputs into the code root inside a bundle would either fail outright
or silently discard every generated model when the app closed, so the two are
resolved separately here and imported wherever a path is built.

``ARCHX3D_DATA_ROOT`` overrides the data root. The desktop shell sets it to the
platform's app-data directory; the CLI leaves it unset and keeps the historical
behaviour of writing beside the source.
"""

from __future__ import annotations

import os
import sys

#: Where modules/, config.json and the pipeline scripts live.
CODE_ROOT: str = getattr(
    sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _default_data_root() -> str:
    """Writable root when the environment does not name one.

    A frozen build has no sensible default beside the executable — it may sit
    in Program Files — so it falls back to the per-user app-data directory that
    the shell would have supplied anyway.
    """
    override = os.environ.get("ARCHX3D_DATA_ROOT")
    if override:
        return os.path.abspath(override)

    if getattr(sys, "frozen", False):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_DATA_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "share")
        )
        return os.path.join(base, "ArchX3D")

    return CODE_ROOT


DATA_ROOT: str = _default_data_root()

# Publish the resolved value so every child process agrees with its parent.
# Blender's interpreter cannot import this module (it runs the generator as a
# loose script), and a frozen build that fell back to the app-data default was
# never told the answer by anyone — so without this the parent and the child
# would each re-derive it and could disagree.
os.environ["ARCHX3D_DATA_ROOT"] = DATA_ROOT


def code_path(*parts: str) -> str:
    """A path to something shipped with the program."""
    return os.path.join(CODE_ROOT, *parts)


def data_path(*parts: str) -> str:
    """A path to something the program writes."""
    return os.path.join(DATA_ROOT, *parts)


def ensure_data_dirs() -> None:
    """Create the writable directories the pipeline assumes exist."""
    for name in ("data", "output", "uploads", "projects"):
        os.makedirs(data_path(name), exist_ok=True)
