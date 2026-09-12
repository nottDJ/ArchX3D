"""
Tests that video stitching reads and writes under the *data* root.

Step 4 is the one pipeline stage that takes no path arguments: main.py spawns
``video_stitcher.py`` bare and the module decides for itself where the frames
are. It used to decide from ``__file__``, which is correct only while the code
root and the data root are the same directory.

They are not the same in the desktop build. The code is unpacked into
PyInstaller's temporary directory while the pipeline renders into
``%LOCALAPPDATA%\\ArchX3D``, so the stitcher looked for frames inside the
bundle, found none, and exited 1 — and because main.py spawns step 4 with
``critical=False``, the run still reported success. The symptom was a finished
generation with 120 rendered frames and no walkthrough.mp4, which is exactly
what an installed copy produced.

The regression is pinned at the module's resolved paths rather than by running
a full stitch: the bug was entirely in *where* it looked, and asserting on the
paths keeps the test free of OpenCV codec behaviour that varies by platform.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules")
if MODULES not in sys.path:
    sys.path.insert(0, MODULES)


@pytest.fixture
def stitcher(tmp_path, monkeypatch):
    """The stitcher re-imported against a data root that is not the code root.

    Re-imported per test because the module resolves its paths at import time —
    which is the behaviour under test, so monkeypatching afterwards would test
    nothing.
    """
    monkeypatch.setenv("ARCHX3D_DATA_ROOT", str(tmp_path))

    for name in ("app_paths", "video_stitcher"):
        sys.modules.pop(name, None)

    module = importlib.import_module("video_stitcher")
    yield module, tmp_path

    for name in ("app_paths", "video_stitcher"):
        sys.modules.pop(name, None)


class TestStitcherResolvesAgainstTheDataRoot:
    def test_frames_are_read_from_the_data_root(self, stitcher):
        module, data_root = stitcher
        assert module.FRAMES_DIR == str(data_root / "output" / "frames")

    def test_video_is_written_to_the_data_root(self, stitcher):
        module, data_root = stitcher
        assert module.VIDEO_OUTPUT_PATH == str(data_root / "output" / "walkthrough.mp4")

    def test_paths_do_not_fall_back_to_the_code_root(self, stitcher):
        """The specific regression: a bundle's code directory is not writable.

        Asserting only "the path is correct" would still pass if the module
        resolved to the code root *and* the code root happened to equal the
        data root, which is the case in every source checkout — so the test
        states the distinction explicitly.
        """
        module, _ = stitcher
        code_root = os.path.dirname(MODULES)
        assert not module.FRAMES_DIR.startswith(code_root)
        assert not module.VIDEO_OUTPUT_PATH.startswith(code_root)

    def test_the_frames_a_render_just_wrote_are_the_ones_found(self, stitcher):
        """End of the real failure: frames written by the pipeline are seen.

        The render stage writes into the data root; before the fix the stitcher
        looked somewhere else entirely and reported "Frames directory not
        found" against a directory the pipeline had never been asked to use.
        """
        module, data_root = stitcher
        frames = data_root / "output" / "frames"
        frames.mkdir(parents=True)
        for n in range(1, 4):
            (frames / f"frame_{n:04d}.png").write_bytes(b"")

        assert os.path.isdir(module.FRAMES_DIR)
        assert sorted(os.listdir(module.FRAMES_DIR)) == [
            "frame_0001.png", "frame_0002.png", "frame_0003.png",
        ]
