"""
Guards on desktop/build.ps1's Visual Studio bootstrap.

The build script puts MSVC on PATH before invoking cargo, because Rust links
with link.exe and the Windows SDK supplies kernel32.lib. It used to do that
through ``Launch-VsDevShell.ps1 -Arch amd64 -HostArch amd64``.

Those two parameters only exist in the VS 2022 copy of that wrapper. On VS 2019
Build Tools — which docs/DESKTOP.md lists as a prerequisite without naming a
version — the call raises a parameter-binding error, and since the script runs
under ``$ErrorActionPreference = "Stop"`` that error aborted the entire build
before stage 1. The one documented build command simply did not work on a
supported toolchain.

``VsDevCmd.bat`` has accepted ``-arch``/``-host_arch`` since VS 2017, so it
covers every version the project supports.

These are static assertions rather than an execution test on purpose: actually
exercising the regression needs a machine with VS 2019 and no VS 2022, which
no test runner can be assumed to have. What can be pinned cheaply is the shape
of the call, which is where the bug lived.
"""

from __future__ import annotations

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_SCRIPT = os.path.join(REPO_ROOT, "desktop", "build.ps1")


@pytest.fixture(scope="module")
def script() -> str:
    """The script's full text, comments included."""
    if not os.path.exists(BUILD_SCRIPT):
        pytest.skip("desktop/build.ps1 is not present in this checkout")
    with open(BUILD_SCRIPT, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def code(script) -> str:
    """The script with whole-line comments removed.

    The fix's own comment explains the trap by naming ``-Arch``/``-HostArch``,
    so a search over the raw text matches the very prose warning against them.
    What matters is whether the script *passes* those parameters, so the
    assertions run against executable lines only.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


class TestVisualStudioBootstrapIsVersionPortable:
    @pytest.mark.parametrize("parameter", ["-Arch", "-HostArch", "-DevCmdArguments"])
    def test_no_vs2022_only_parameters(self, code, parameter):
        """None of these exist on the VS 2019 wrapper, and passing one is fatal."""
        assert parameter not in code, (
            f"{parameter} is a VS 2022-only parameter of Launch-VsDevShell.ps1; "
            f"passing it aborts build.ps1 on VS 2019 Build Tools"
        )

    def test_uses_vsdevcmd_batch_file(self, code):
        """The portable entry point, supported since VS 2017."""
        assert "VsDevCmd.bat" in code

    def test_requests_an_x64_toolchain(self, code):
        """A 32-bit host toolchain cannot link the x64 shell."""
        assert "-arch=x64" in code

    def test_bootstrap_failure_is_not_fatal(self, code):
        """rustc can often find link.exe unaided.

        The original call was fatal by accident rather than by intent; failing
        the build over a best-effort PATH setup is worse than letting the link
        step report the real problem.
        """
        assert "try {" in code and "Write-Warning" in code
