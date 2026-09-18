"""
Offline-first: the geometry needs no network, no key and no model.

AI is optional enrichment on top of a deterministic CPU reconstruction. These
tests hold that line: the reconstruction runs with every network connection
refused, nothing in the engine imports a network or AI client, and the build
log states what the build ran on.
"""

import ast
import os
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RECON = os.path.join(ROOT, "modules", "recon")
NETWORK_MODULES = {"socket", "requests", "httpx", "urllib", "http", "aiohttp",
                   "google", "genai", "openai", "anthropic"}


@pytest.fixture
def no_network(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network refused in an offline test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return attempts


@pytest.mark.parametrize("fixture", [
    "tests/fixtures/real/final_plan_19th_may.dxf",
    "tests/fixtures/plans/residential_us.dxf",
    "tests/corpus/multistorey/bsc_duplex_A.dxf",
])
def test_reconstruction_runs_with_the_network_refused(no_network, tmp_path, fixture):
    from modules.recon import compat
    from modules.recon.pipeline import reconstruct
    from modules.blender_build import check_buildable
    d = reconstruct(os.path.join(ROOT, fixture), diagnostics_dir=str(tmp_path / "diag"))
    d.to_json(str(tmp_path / "building.json"))
    compat.write_geometry_json(d, str(tmp_path / "geometry.json"))
    ok, why = check_buildable(d.as_dict())
    assert ok, why
    assert not no_network, "the reconstruction tried to open a connection"


def test_the_engine_imports_no_network_or_ai_client():
    offenders = []
    for name in sorted(os.listdir(RECON)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(RECON, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m.split(".")[0] in NETWORK_MODULES:
                    offenders.append((name, m))
    assert not offenders, offenders


def test_the_build_log_states_what_the_build_runs_on():
    import main
    offline = main.engine_statement(offline=True, vision=False, styling=False)
    assert "Engine:   deterministic CPU reconstruction" in offline
    assert "AI:       disabled" in offline
    assert "Network:  not required" in offline

    plain = main.engine_statement(offline=False, vision=False, styling=False)
    assert "AI:       disabled" in plain and "Network:  not required" in plain

    enriched = main.engine_statement(offline=False, vision=True, styling=False)
    assert any(l.startswith("AI:       enabled") for l in enriched)
    assert any("geometry does not use it" in l for l in enriched)
    assert enriched[0] == "Engine:   deterministic CPU reconstruction"


def test_offline_mode_withholds_the_key_from_every_later_step(monkeypatch, tmp_path):
    import main
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(sys, "argv", ["main.py", str(tmp_path / "missing.dxf"), "--offline"])
    with pytest.raises(SystemExit):
        main.main()          # stops at the missing input, after the offline switch
    assert "GEMINI_API_KEY" not in os.environ
    assert os.environ.get("ARCHX3D_OFFLINE") == "1"
    monkeypatch.delenv("ARCHX3D_OFFLINE", raising=False)
