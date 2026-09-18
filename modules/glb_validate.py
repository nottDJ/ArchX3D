"""
ArchX3D — GLB validation against the architectural model
========================================================
Opens an exported ``model.glb`` and checks it against the ``building.json`` it
was built from: that the file is well-formed glTF 2.0, and that the geometry
in it is the building — every storey present, at its elevation, over its
footprint — rather than merely a file that parses.

Why this exists
---------------
The failure the reconstruction engine was written to fix shipped a *valid GLB*
of a 0.8 m building. "The exporter reported success" and "the file loads" were
both true. So the checks here are measurements: the model's plan extent
against the model's, each storey's height band against its elevation.

Stdlib only, so it runs anywhere the pipeline does, with no glTF library.

Usage::

    python modules/glb_validate.py output/model.glb data/building.json
"""

from __future__ import annotations

import json
import math
import struct
import sys
from typing import Dict, List, Optional, Sequence, Tuple

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


class GlbError(ValueError):
    pass


def read_glb(path: str) -> Tuple[dict, bytes]:
    """The glTF JSON document and the binary chunk of a GLB file."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 20:
        raise GlbError("file too short to be a GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise GlbError("not a GLB (bad magic)")
    if version != 2:
        raise GlbError("unsupported glTF container version %d" % version)
    if length != len(data):
        raise GlbError("declared length %d does not match file size %d"
                       % (length, len(data)))
    offset = 12
    doc = None
    binary = b""
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        body = data[offset + 8: offset + 8 + chunk_len]
        if chunk_type == CHUNK_JSON:
            doc = json.loads(body.decode("utf-8"))
        elif chunk_type == CHUNK_BIN:
            binary = body
        offset += 8 + chunk_len
    if doc is None:
        raise GlbError("no JSON chunk")
    return doc, binary


def _quat_rotate(q: Sequence[float], v: Sequence[float]) -> Tuple[float, float, float]:
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w*t + cross(q.xyz, t)
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx))


def node_bounds(doc: dict) -> List[dict]:
    """World-space, Y-up bounds of every mesh node, from accessor min/max.

    Accessor bounds are the eight corners of the local box; rotated and
    scaled corners are transformed individually so a rotated node's world box
    is still conservative. Parent transforms are applied down the hierarchy.
    """
    nodes = doc.get("nodes", [])
    meshes = doc.get("meshes", [])
    accessors = doc.get("accessors", [])
    parent: Dict[int, int] = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i

    def chain(i: int) -> List[dict]:
        out = []
        while True:
            out.append(nodes[i])
            if i not in parent:
                return out
            i = parent[i]

    def apply(n: dict, p: Tuple[float, float, float]) -> Tuple[float, float, float]:
        if "matrix" in n:
            m = n["matrix"]
            return (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
                    m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
                    m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])
        s = n.get("scale", [1, 1, 1])
        p = (p[0] * s[0], p[1] * s[1], p[2] * s[2])
        if "rotation" in n:
            p = _quat_rotate(n["rotation"], p)
        t = n.get("translation", [0, 0, 0])
        return (p[0] + t[0], p[1] + t[1], p[2] + t[2])

    out = []
    for i, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        for prim in meshes[n["mesh"]].get("primitives", []):
            acc = accessors[prim["attributes"]["POSITION"]]
            if "min" not in acc or "max" not in acc:
                raise GlbError("POSITION accessor without min/max on %r" % n.get("name"))
            amin, amax = acc["min"], acc["max"]
            for cx in (amin[0], amax[0]):
                for cy in (amin[1], amax[1]):
                    for cz in (amin[2], amax[2]):
                        p = (cx, cy, cz)
                        for m in chain(i):
                            p = apply(m, p)
                        for k in range(3):
                            lo[k] = min(lo[k], p[k])
                            hi[k] = max(hi[k], p[k])
        out.append({"name": n.get("name", ""), "extras": n.get("extras", {}),
                    "min": lo, "max": hi})
    return out


def validate(glb_path: str, model: Optional[dict] = None, *,
             tolerance: float = 0.35) -> dict:
    """Check a GLB's structure and, given the model, its architecture.

    Returns ``{"ok", "errors", "warnings", "checks"}``. glTF is Y-up; the model
    is Z-up plan metres, so plan ``(x, y)`` is glTF ``(x, -z)`` and elevation
    is glTF ``y``.
    """
    errors: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, object] = {}
    try:
        doc, binary = read_glb(glb_path)
    except (OSError, GlbError, ValueError) as exc:
        return {"ok": False, "errors": ["unreadable GLB: %s" % exc],
                "warnings": [], "checks": {}}

    asset = doc.get("asset", {})
    checks["gltf_version"] = asset.get("version")
    if asset.get("version") != "2.0":
        errors.append("asset.version is %r, not 2.0" % asset.get("version"))
    for k, bv in enumerate(doc.get("bufferViews", [])):
        end = bv.get("byteOffset", 0) + bv.get("byteLength", 0)
        if bv.get("buffer", 0) == 0 and end > len(binary):
            errors.append("bufferView %d overruns the binary chunk" % k)
            break
    try:
        nodes = node_bounds(doc)
    except (GlbError, KeyError, IndexError) as exc:
        return {"ok": False, "errors": errors + ["malformed geometry: %s" % exc],
                "warnings": warnings, "checks": checks}
    checks["mesh_nodes"] = len(nodes)
    if not nodes:
        errors.append("the GLB contains no mesh")

    finite = [n for n in nodes if all(math.isfinite(v) for v in n["min"] + n["max"])]
    if len(finite) != len(nodes):
        errors.append("%d mesh node(s) have non-finite bounds" % (len(nodes) - len(finite)))

    walls = [n for n in finite if (n["extras"] or {}).get("archx3d_kind") == "wall"]
    checks["wall_nodes"] = len(walls)
    if model is None:
        return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}

    storeys = []
    if "buildings" in model:
        for b in model["buildings"]:
            for l in b.get("levels", []):
                storeys.append((b, l))
    elif "walls" in model:
        storeys.append(({"id": "b1"}, model))
    checks["model_storeys"] = len(storeys)

    per_level = {}
    for b, l in storeys:
        lid = l.get("id", "")
        mine = [n for n in walls if (n["extras"] or {}).get("archx3d_level") == lid] \
            if len(storeys) > 1 else walls
        entry = {"wall_nodes": len(mine)}
        if l.get("walls") and not mine:
            errors.append("storey %s has walls in the model but none in the GLB" % lid)
            per_level[lid] = entry
            continue
        if not mine:
            per_level[lid] = entry
            continue
        px, py = (l.get("placement") or [0.0, 0.0])[:2]
        xs = [p[0] + px for w in l.get("walls", []) for p in (w["start"], w["end"])]
        ys = [p[1] + py for w in l.get("walls", []) for p in (w["start"], w["end"])]
        gx0 = min(n["min"][0] for n in mine)
        gx1 = max(n["max"][0] for n in mine)
        # plan y = -glTF z
        gy0 = -max(n["max"][2] for n in mine)
        gy1 = -min(n["min"][2] for n in mine)
        z0 = min(n["min"][1] for n in mine)
        z1 = max(n["max"][1] for n in mine)
        elevation = float(l.get("elevation", 0.0) or 0.0)
        entry.update({
            "plan_extent_glb": [round(gx1 - gx0, 3), round(gy1 - gy0, 3)],
            "plan_extent_model": [round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)],
            "z_band": [round(z0, 3), round(z1, 3)],
            "elevation": elevation,
        })
        if abs(gx0 - min(xs)) > tolerance or abs(gx1 - max(xs)) > tolerance or \
                abs(gy0 - min(ys)) > tolerance or abs(gy1 - max(ys)) > tolerance:
            errors.append("storey %s walls in the GLB span x %.2f..%.2f, y %.2f..%.2f "
                          "but the model's span x %.2f..%.2f, y %.2f..%.2f"
                          % (lid, gx0, gx1, gy0, gy1, min(xs), max(xs), min(ys), max(ys)))
        if abs(z0 - elevation) > 0.05:
            errors.append("storey %s walls start at z=%.3f, expected its elevation %.3f"
                          % (lid, z0, elevation))
        if z1 - z0 < 2.0:
            errors.append("storey %s walls are only %.2f m tall" % (lid, z1 - z0))
        per_level[lid] = entry
    checks["levels"] = per_level
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: glb_validate.py model.glb [building.json]")
        return 2
    model = None
    if len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as fh:
            model = json.load(fh)
    report = validate(argv[0], model)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
