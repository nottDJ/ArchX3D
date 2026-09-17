"""
ArchX3D — DXF Extractor (v4 — the reconstruction's projection)
===============================================================
Reads an architectural DXF and writes ``geometry.json``.

What changed in v4
------------------
v3 delegated to :mod:`cad.reader`, a DXF parser of its own with its own unit
detection and origin. The pipeline had meanwhile moved to
:mod:`modules.recon`, so a project could hold a ``geometry.json`` from one
reader and a ``building.json`` from another — and the two disagreed about the
drawing's units on the very plan that motivated the rewrite.

v4 has no parser. It runs the reconstruction and writes the projection
:mod:`modules.recon.compat` derives from the validated model, so there is one
reading of every drawing no matter which entry point produced it. The CAD
document the semantic tier reads is still embedded under ``cad``, projected by
:mod:`modules.recon.evidence` from the same reader.

Usage::

    python modules/dxf_extractor.py <input.dxf> <output.json> [layers] [scale] [arcs]

``layers`` and ``arcs`` are accepted for compatibility and have no effect: wall
layers are classified by the reconstruction, and curves are flattened to a
tolerance rather than a segment count. ``scale`` overrides unit resolution.
"""

from __future__ import annotations

import json
import os
import sys

# Allow both `python modules/dxf_extractor.py` and `python -m modules.dxf_extractor`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recon import compat  # noqa: E402
from recon.ir import ReconstructionError  # noqa: E402
from recon.pipeline import reconstruct  # noqa: E402


def extract_walls(
    dxf_path,
    output_path,
    layer_names=None,
    scale_factor=1.0,
    arc_segments=16,
    auto_detect=True,
    deduplicate=True,
    min_segment_length=0.05,
    normalize=True,
    filter_borders=True,
    log=print,
    wall_height=2.7,
    building_path=None,
):
    """Reconstruct a DXF and save its ``geometry.json`` projection.

    The signature is retained so existing callers keep working; every
    parameter except ``scale_factor`` is now decided by the reconstruction and
    is accepted without effect. Exits with status 1 when the drawing cannot be
    reconstructed, after logging why.

    Returns:
        The ``geometry.json`` document, as a dict.
    """
    if layer_names and {n.strip().upper() for n in layer_names if n} - {"AUTO", "WALLS"}:
        log("[LAYER] Layer restriction %r is ignored: walls are classified by "
            "the reconstruction engine" % list(layer_names))
    try:
        model = reconstruct(
            dxf_path,
            wall_height=wall_height,
            user_scale=scale_factor if scale_factor and scale_factor != 1.0 else None,
        )
    except ReconstructionError as exc:
        log(f"[ERROR] Could not reconstruct {dxf_path} ({exc.stage}): {exc}")
        for failure in exc.failures[:5]:
            log(f"  - {failure}")
        sys.exit(1)

    geometry = compat.to_geometry_json(model)
    _report(model, geometry, log)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(geometry, fh, indent=2)
        log(f"[OK] Geometry saved to {output_path}")
        if building_path:
            model.to_json(building_path)
            log(f"[OK] Model saved to {building_path}")
    except IOError as exc:
        log(f"[ERROR] Failed to save JSON: {exc}")
        sys.exit(1)

    return geometry


def _report(model, geometry, log) -> None:
    """Print what the drawing turned out to contain."""
    s = model.summary()
    cad = geometry.get("cad") or {}
    log("")
    log("[INFO] === Drawing contents ==================================")
    log(f"  Buildings     : {s['buildings']} ({s['levels']} storey(s), "
        f"{s.get('level_structure')})")
    log(f"  Walls         : {s['walls']} ({s['total_wall_length_m']:.1f} m)")
    log(f"  Rooms         : {s['rooms']} ({s['floor_area_m2']:.1f} m2)")
    log(f"  Openings      : {s['doors']} doors, {s['windows']} windows")
    log(f"  Blocks        : {len(cad.get('blocks') or [])}")
    log(f"  Text entities : {len(cad.get('texts') or [])}")
    log(f"  Dimensions    : {len(cad.get('dimensions') or [])}")
    log(f"  Units         : {s.get('units')} (x{s.get('scale_to_m')})")
    named = [r.label for r in model.rooms if r.label]
    if named:
        log("  Rooms named in the drawing: " + ", ".join(named[:12]))
    for item in model.review[:5]:
        log(f"  Review: {item.get('message')}")
    log("[INFO] =======================================================")
    log("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python dxf_extractor.py <input_dxf> <output_json> "
              "[layers] [scale] [arc_segments]")
        print("  scale:  metres per drawing unit (1.0 = resolve from the drawing)")
        sys.exit(1)

    input_dxf = sys.argv[1]
    output_json = sys.argv[2]
    layers = sys.argv[3].split(",") if len(sys.argv) > 3 else None
    scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    arcs = int(sys.argv[5]) if len(sys.argv) > 5 else 16

    extract_walls(input_dxf, output_json, layers, scale, arcs)
