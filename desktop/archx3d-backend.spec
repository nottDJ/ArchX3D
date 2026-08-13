# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the ArchX3D desktop backend.

Build:  pyinstaller desktop/archx3d-backend.spec --noconfirm

One binary, seven entry points — see desktop/backend_main.py. Everything the
pipeline can spawn has to be *importable* from the bundle, which is why the
pipeline packages are listed as hidden imports: PyInstaller's static analysis
cannot see them, because they are reached through runpy at runtime rather than
by an import statement anywhere in the graph.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
MODULES = os.path.join(ROOT, "modules")

# The pipeline's own packages. Reached via runpy.run_module, so nothing imports
# them statically and PyInstaller would otherwise leave every one of them out.
PIPELINE_PACKAGES = [
    "cad", "vision", "semantic", "registration", "furnish",
    "evaluation", "optimizer", "planner", "render", "blender",
]

hiddenimports = [
    # Top-level pipeline stages dispatched by --child.
    "dxf_extractor", "scene_analyzer", "style_generator", "video_stitcher",
    "project_api", "child_process", "app_paths", "server", "main",
    # uvicorn resolves its implementation classes by string name at runtime.
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
]
for package in PIPELINE_PACKAGES:
    hiddenimports += collect_submodules(package, on_error="ignore")

a = Analysis(
    [os.path.join(SPECPATH, "backend_main.py")],
    pathex=[ROOT, MODULES],
    binaries=[],
    datas=[
        # Read at runtime by main.py and the Blender generator.
        (os.path.join(ROOT, "config.json"), "."),
        # blender_generator.py is not imported — it is passed to Blender's own
        # interpreter as a script path, so it must exist as a real file.
        (MODULES, "modules"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here renders a GUI from Python; the shell is Tauri. Excluding the
    # toolkits keeps tens of megabytes of Tk/Qt out of the bundle.
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="archx3d-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console subsystem binary: the Tauri shell captures stdout/stderr for its
    # log panel, and a windowed build would give it nothing to read. The shell
    # spawns it with CREATE_NO_WINDOW so no terminal appears.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="archx3d-backend",
)
