"""
Geometric quality on the corpus, held to measured floors.

Every drawing in ``tests/corpus`` with a truth document is reconstructed and
measured (:mod:`modules.recon.metrics`), and the aggregates are compared with
floors set a little below what the engine measured when they were written
(see ``docs/CORPUS_REPORT.md``). A change that costs accuracy fails here with
the number that moved; a change that improves it should raise the floor.

The corpus and where each drawing comes from are described in
``tests/corpus/README.md``.
"""

import glob
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "corpus_evaluate", os.path.join(ROOT, "tools", "corpus", "evaluate.py"))
EV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EV)

CORPUS = sorted(p for p in glob.glob(os.path.join(ROOT, "tests", "corpus", "**", "*.dxf"),
                                     recursive=True)
                if os.path.exists(p[:-4] + ".truth.json"))

#: group -> metric -> floor (or exact value for accuracies that are all-or-nothing)
FLOORS = {
    "ResPlan": {
        "door_precision": 0.99, "door_recall": 0.85,
        "window_precision": 0.99, "window_recall": 0.94,
        "room_recall": 0.83, "room_count_accuracy": 0.78,
        "footprint_iou_median": 0.97, "unit_accuracy": 1.0,
        "structure_accuracy": 0.94,
    },
    "buildingSMART Community Sample Test Files": {
        "door_precision": 0.96, "door_recall": 0.87,
        "window_precision": 0.97, "window_recall": 0.72,
        "room_recall": 0.88, "room_count_accuracy": 0.90,
        "footprint_iou_median": 0.90, "unit_accuracy": 1.0,
        "structure_accuracy": 1.0,
    },
    "ResPlan + OpenStreetMap (composed)": {
        "door_precision": 0.98, "door_recall": 0.83,
        "window_precision": 0.99, "window_recall": 0.93,
        "room_recall": 0.72, "room_count_accuracy": 0.83,
        "footprint_iou_median": 0.80, "unit_accuracy": 1.0,
        "structure_accuracy": 0.89,
    },
}


@pytest.fixture(scope="module")
def rows():
    if not CORPUS:
        pytest.skip("no corpus drawings")
    return [EV.run_one(p, None) for p in CORPUS]


def test_the_corpus_is_what_the_report_describes():
    names = [os.path.relpath(p, ROOT).replace("\\", "/") for p in CORPUS]
    assert sum(1 for n in names if "/residential/" in n) >= 20
    assert sum(1 for n in names if "/apartment/" in n) >= 10
    assert sum(1 for n in names if "/multistorey/" in n) >= 5
    assert sum(1 for n in names if "/multibuilding/" in n) >= 5
    assert sum(1 for n in names if "/siteplan/" in n) >= 5


def test_every_site_plan_separates_its_buildings(rows):
    wrong = [(r["file"], r["metrics"]["structure"]) for r in rows
             if "site_plan" in r.get("tags", [])
             and r["metrics"]["structure"]["buildings_found"]
             != r["metrics"]["structure"]["buildings_expected"]]
    assert not wrong, wrong


def test_nothing_in_the_corpus_is_refused_or_invalid(rows):
    bad = [(r["file"], r["status"], r.get("failures")) for r in rows
           if r["status"] in ("REFUSED", "INVALID")]
    assert not bad, bad


@pytest.mark.parametrize("dataset", sorted(FLOORS))
def test_quality_floors(rows, dataset):
    group = EV.aggregate([r for r in rows if r["dataset"] == dataset])
    assert group["drawings"] > 0, dataset
    short = {k: (group[k], floor) for k, floor in FLOORS[dataset].items()
             if group[k] is None or group[k] < floor}
    assert not short, "%s below floor (measured, floor): %s" % (dataset, short)


def test_every_real_multi_storey_building_resolves_exactly(rows):
    wrong = []
    for r in rows:
        if "multi_storey" not in r.get("tags", []):
            continue
        s = r["metrics"]["structure"]
        if (s["buildings_found"], s["levels_found"]) != (s["buildings_expected"],
                                                         s["levels_expected"]):
            wrong.append((r["file"], s))
    assert not wrong, wrong


def test_no_real_drawing_reports_a_false_door_rate_above_twelve_percent(rows):
    # Precision first: a false door is worse than an unknown opening.
    worst = []
    for r in rows:
        d = (r.get("metrics") or {}).get("openings", {}).get("door") or {}
        if d.get("reported", 0) >= 10 and (d.get("precision") or 0) < 0.88:
            worst.append((r["file"], d.get("precision")))
    assert not worst, worst
