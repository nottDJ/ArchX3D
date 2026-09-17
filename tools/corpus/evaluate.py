"""
Measure the reconstruction engine across the corpus and write the report.

Runs every DXF that has a ``.truth.json`` beside it — the converted corpus in
``tests/corpus`` and the generated fixtures in ``tests/fixtures`` — computes
the geometric metrics in :mod:`modules.recon.metrics`, and aggregates them by
category and by drafting convention.

Usage::

    python tools/corpus/evaluate.py [--json results.json] [--markdown report.md]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from modules.recon.ir import ReconstructionError  # noqa: E402
from modules.recon.metrics import evaluate, load_truth  # noqa: E402
from modules.recon.pipeline import reconstruct  # noqa: E402

SOURCES = [
    ("tests/corpus/**/*.dxf", None),
    ("tests/fixtures/plans/*.dxf", "generated"),
    ("tests/fixtures/multi/*.dxf", "generated-multi"),
    ("tests/fixtures/doors/*.dxf", "generated-doors"),
]


def files() -> List[tuple]:
    out = []
    for pattern, category in SOURCES:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern), recursive=True)):
            if os.path.exists(path[:-4] + ".truth.json"):
                out.append((path, category))
    return out


def run_one(path: str, category: Optional[str]) -> dict:
    truth = load_truth(path)
    src = truth.get("source") or {}
    row = {
        "file": os.path.relpath(path, ROOT).replace("\\", "/"),
        "category": category or src.get("category") or os.path.basename(os.path.dirname(path)),
        "style": src.get("style", "-"),
        "dataset": src.get("dataset", "generated"),
        "native_cad": bool(src.get("native_cad", False)),
        "tags": list(src.get("tags") or []),
    }
    t = time.perf_counter()
    try:
        d = reconstruct(path, strict=False)
    except ReconstructionError as exc:
        row.update({"status": "REFUSED", "stage": exc.stage,
                    "failures": exc.failures[:3], "seconds": round(time.perf_counter() - t, 3)})
        return row
    row["seconds"] = round(time.perf_counter() - t, 3)
    row["status"] = d.validation.get("status")
    row["confidence"] = (d.validation.get("quality") or {}).get("confidence")
    row["metrics"] = evaluate(d, truth)
    return row


def _sum(rows, *keys):
    total = 0
    for r in rows:
        v = r.get("metrics") or {}
        for k in keys:
            v = (v or {}).get(k)
        total += v or 0
    return total


def _values(rows, *keys):
    out = []
    for r in rows:
        v = r.get("metrics") or {}
        for k in keys:
            v = (v or {}).get(k) if isinstance(v, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(v)
    return out


def _ratio(n, d):
    return round(n / d, 4) if d else None


def _med(vals):
    return round(statistics.median(vals), 4) if vals else None


def _mean(vals):
    return round(statistics.fmean(vals), 4) if vals else None


def aggregate(rows: List[dict]) -> dict:
    ok = [r for r in rows if r.get("metrics")]
    units = [r["metrics"]["scale"]["unit_correct"] for r in ok
             if (r["metrics"].get("scale") or {}).get("unit_correct") is not None]
    structure = [r["metrics"]["structure"] for r in ok if r["metrics"].get("structure")]
    statuses: Dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {
        "drawings": len(rows),
        "statuses": dict(sorted(statuses.items())),
        "door_precision": _ratio(_sum(ok, "openings", "door", "true_positive"),
                                 _sum(ok, "openings", "door", "reported")),
        "door_recall": _ratio(_sum(ok, "openings", "door", "true_positive"),
                              _sum(ok, "openings", "door", "truth")),
        "window_precision": _ratio(_sum(ok, "openings", "window", "true_positive"),
                                   _sum(ok, "openings", "window", "reported")),
        "window_recall": _ratio(_sum(ok, "openings", "window", "true_positive"),
                                _sum(ok, "openings", "window", "truth")),
        "doors_truth": _sum(ok, "openings", "door", "truth"),
        "windows_truth": _sum(ok, "openings", "window", "truth"),
        "opening_position_error_m": _mean(_values(ok, "openings", "door", "mean_position_error_m")
                                          + _values(ok, "openings", "window", "mean_position_error_m")),
        "room_recall": _ratio(_sum(ok, "rooms", "matched"), _sum(ok, "rooms", "truth_enclosed")),
        "room_count_accuracy": _mean(_values(ok, "rooms", "count_accuracy")),
        "room_area_error_median": _med(_values(ok, "rooms", "median_area_error")),
        "footprint_iou_mean": _mean(_values(ok, "footprint", "iou")),
        "footprint_iou_median": _med(_values(ok, "footprint", "iou")),
        "wall_position_error_m": _mean(_values(ok, "walls", "mean_position_error_m")),
        "wall_recall": _mean(_values(ok, "walls", "recall")),
        "wall_thickness_error_m": _med(_values(ok, "walls", "thickness_error_m")),
        "wall_length_error": _med(_values(ok, "walls", "length_error")),
        "unit_accuracy": _ratio(sum(1 for u in units if u), len(units)),
        "linear_scale_error_median": _med(_values(ok, "scale", "linear_scale_error")),
        "structure_accuracy": _ratio(sum(1 for s in structure
                                         if s["buildings_found"] == s["buildings_expected"]
                                         and s["levels_found"] == s["levels_expected"]),
                                     len(structure)),
        "seconds_total": round(sum(r["seconds"] for r in rows), 2),
        "seconds_max": max((r["seconds"] for r in rows), default=None),
    }


def markdown(rows: List[dict], groups: Dict[str, dict]) -> str:
    cols = ["drawings", "statuses", "door_precision", "door_recall", "window_precision",
            "window_recall", "room_recall", "room_count_accuracy", "room_area_error_median",
            "footprint_iou_median", "wall_position_error_m", "wall_recall",
            "wall_thickness_error_m", "unit_accuracy", "linear_scale_error_median",
            "structure_accuracy", "seconds_max"]
    lines = ["# ArchX3D reconstruction — corpus metrics", "",
             "Generated by `tools/corpus/evaluate.py`. Every figure is measured "
             "against ground truth; see `tests/corpus/README.md` for where each "
             "drawing comes from and what its truth means.", "",
             "| group | " + " | ".join(cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    for name, agg in groups.items():
        cells = []
        for c in cols:
            v = agg.get(c)
            cells.append(json.dumps(v).replace("|", "/") if isinstance(v, dict) else str(v))
        lines.append("| %s | %s |" % (name, " | ".join(cells)))
    lines += ["", "## Per drawing", "",
              "| file | status | doors tp/truth/reported | windows tp/truth/reported | rooms matched/truth | footprint IoU | wall pos err m | unit ok | s |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        m = r.get("metrics") or {}
        o = m.get("openings") or {}
        d = o.get("door") or {}
        w = o.get("window") or {}
        rm = m.get("rooms") or {}
        fp = m.get("footprint") or {}
        wl = m.get("walls") or {}
        sc = m.get("scale") or {}
        lines.append("| %s | %s | %s/%s/%s | %s/%s/%s | %s/%s | %s | %s | %s | %s |" % (
            r["file"], r["status"], d.get("true_positive", "-"), d.get("truth", "-"),
            d.get("reported", "-"), w.get("true_positive", "-"), w.get("truth", "-"),
            w.get("reported", "-"), rm.get("matched", "-"), rm.get("truth_enclosed", "-"),
            fp.get("iou", "-"), wl.get("mean_position_error_m", "-"),
            sc.get("unit_correct", "-"), r["seconds"]))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(ROOT, "tests", "corpus", "results.json"))
    ap.add_argument("--markdown", default=os.path.join(ROOT, "docs", "CORPUS_REPORT.md"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    rows = [run_one(p, c) for p, c in files()]
    groups: Dict[str, dict] = {"ALL": aggregate(rows)}
    for key in sorted({r["category"] for r in rows}):
        groups["category:" + key] = aggregate([r for r in rows if r["category"] == key])
    for key in sorted({r["style"] for r in rows if r["style"] != "-"}):
        groups["style:" + key] = aggregate([r for r in rows if r["style"] == key])
    for key in sorted({r["dataset"] for r in rows}):
        groups["dataset:" + key] = aggregate([r for r in rows if r["dataset"] == key])
    for key in sorted({t for r in rows for t in r.get("tags", [])}):
        groups["tag:" + key] = aggregate([r for r in rows if key in r.get("tags", [])])
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump({"groups": groups, "rows": rows}, fh, indent=1)
    with open(args.markdown, "w", encoding="utf-8") as fh:
        fh.write(markdown(rows, groups))
    if not args.quiet:
        for name, agg in groups.items():
            print(name, json.dumps({k: v for k, v in agg.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
