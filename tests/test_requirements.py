"""
Every third-party package the shipped code imports is declared.

The desktop build freezes the backend from a virtualenv created from
``requirements.txt`` alone. A package imported by the engine but installed on
a developer's machine by hand passes every test there and ships a backend that
fails on the first drawing — which is how a build without shapely was made.
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Code that runs inside the frozen backend.
SHIPPED = [os.path.join(ROOT, "modules", "recon"), os.path.join(ROOT, "modules", "project_api.py"),
           os.path.join(ROOT, "modules", "dxf_extractor.py"), os.path.join(ROOT, "main.py"),
           os.path.join(ROOT, "server.py")]

#: Import name -> distribution name, where they differ.
DISTRIBUTION = {"cv2": "opencv-python", "PIL": "Pillow", "multipart": "python-multipart",
                "yaml": "PyYAML"}

#: The project's own top-level packages and modules, importable as bare names.
LOCAL = {name[:-3] if name.endswith(".py") else name
         for name in os.listdir(os.path.join(ROOT, "modules"))} | {"modules", "main", "server"}

#: Imported only inside Blender's bundled Python, or only when an optional
#: feature is used and guarded by ImportError.
OPTIONAL = {"bpy", "mathutils", "bmesh", "google", "keyring", "ifcopenshell", "matplotlib"}


def _files():
    for path in SHIPPED:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.endswith(".py"):
                    yield os.path.join(path, name)
        elif os.path.exists(path):
            yield path


def _top_level_imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module.split(".")[0]


def _declared():
    names = set()
    for line in open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=\[ ;]", line, 1)[0].lower())
    return names


def test_every_shipped_import_is_declared():
    stdlib = set(getattr(sys, "stdlib_module_names", ())) or None
    if stdlib is None:
        import pytest
        pytest.skip("needs Python 3.10+ for sys.stdlib_module_names")
    declared = _declared()
    missing = {}
    for path in _files():
        for name in _top_level_imports(path):
            if name in stdlib or name in LOCAL or name in OPTIONAL or name == "__future__":
                continue
            dist = DISTRIBUTION.get(name, name).lower()
            if dist not in declared:
                missing.setdefault(dist, []).append(os.path.relpath(path, ROOT))
    assert not missing, "imported but not in requirements.txt: %s" % missing


def test_the_engine_dependencies_are_declared():
    declared = _declared()
    for dist in ("ezdxf", "shapely", "numpy"):
        assert dist in declared, dist
