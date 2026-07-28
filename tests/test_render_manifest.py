"""
Tests for the preview manifest.

The manifest is the interface between this pipeline and ``vision.similarity``:
it is how the next phase learns which PNG belongs to which reference
photograph. Two properties carry that weight — records survive a round trip
intact, and a partial run does not narrow the picture — and both are pinned
here.
"""

from __future__ import annotations

import json
import os

from render import manifest as manifest_mod
from render.manifest import Manifest, RenderRecord


def record(viewpoint_id="img_a1", room="room_a", number=1, status=manifest_mod.STATUS_RENDERED):
    return RenderRecord(
        viewpoint_id=viewpoint_id,
        room=room,
        image=f"preview/{room}/viewpoint_{number:02d}.png",
        source_image=f"{viewpoint_id}.jpg",
        camera_hash="cam",
        scene_hash="scene",
        room_hash="room",
        width=640,
        height=360,
        timestamp="2026-01-01T00:00:00Z",
        render_ms=180,
        status=status,
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_a_record_round_trips(tmp_path):
    original = record()
    assert RenderRecord.from_dict(original.to_dict()) == original


def test_the_manifest_carries_what_similarity_needs():
    """The pairing and the provenance, in the shape the next phase reads."""
    payload = record().to_dict()
    for key in ("viewpoint_id", "room", "image", "source_image",
                "camera_hash", "scene_hash", "timestamp", "render_ms"):
        assert key in payload


def test_a_failed_record_carries_its_error_and_is_not_ok():
    failed = record(status=manifest_mod.STATUS_FAILED)
    failed.error = "camera could not be rebuilt"
    assert not failed.ok
    assert failed.to_dict()["error"] == "camera could not be rebuilt"


def test_a_cached_record_still_counts_as_ok():
    assert record(status=manifest_mod.STATUS_CACHED).ok


# ---------------------------------------------------------------------------
# Collection behaviour
# ---------------------------------------------------------------------------


def test_upsert_replaces_rather_than_duplicates():
    manifest = Manifest()
    manifest.upsert(record())
    manifest.upsert(record(status=manifest_mod.STATUS_CACHED))

    assert len(manifest.records) == 1
    assert manifest.records[0].status == manifest_mod.STATUS_CACHED


def test_records_are_addressable_by_viewpoint_and_room():
    manifest = Manifest()
    manifest.upsert(record("img_a1", "room_a", 1))
    manifest.upsert(record("img_a2", "room_a", 2))
    manifest.upsert(record("img_b1", "room_b", 1))

    assert manifest.record_for("img_a2").image.endswith("viewpoint_02.png")
    assert len(manifest.for_room("room_a")) == 2
    assert manifest.rooms() == ["room_a", "room_b"]


def test_prune_drops_records_for_deleted_viewpoints():
    """A deleted camera must not leave a preview the similarity pass scores."""
    manifest = Manifest()
    manifest.upsert(record("img_a1"))
    manifest.upsert(record("img_gone"))

    dropped = manifest.prune(["img_a1"])
    assert [d.viewpoint_id for d in dropped] == ["img_gone"]
    assert manifest.record_for("img_gone") is None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_saving_and_loading_preserves_every_record(tmp_path):
    path = str(tmp_path / "preview" / "manifest.json")
    manifest = Manifest()
    manifest.upsert(record("img_a1", "room_a", 1))
    manifest.upsert(record("img_b1", "room_b", 1))
    manifest.save(path)

    reloaded = Manifest.load(path)
    assert len(reloaded.records) == 2
    assert reloaded.record_for("img_b1").room == "room_b"


def test_records_are_written_in_a_stable_order(tmp_path):
    """Two runs of an unchanged scene should differ only in the timestamp."""
    path = str(tmp_path / "manifest.json")
    manifest = Manifest()
    for viewpoint_id, room, number in (("img_b1", "room_b", 1),
                                       ("img_a2", "room_a", 2),
                                       ("img_a1", "room_a", 1)):
        manifest.upsert(record(viewpoint_id, room, number))
    manifest.save(path)

    payload = json.loads(open(path, encoding="utf-8").read())
    assert [r["viewpoint_id"] for r in payload["renders"]] == ["img_a1", "img_a2", "img_b1"]


def test_a_missing_manifest_loads_as_empty(tmp_path):
    manifest = Manifest.load(str(tmp_path / "nothing.json"))
    assert manifest.records == []
    assert manifest.root == str(tmp_path)


def test_a_corrupt_manifest_loads_as_empty(tmp_path):
    """The cache decides what to re-render; a bad manifest is recoverable."""
    path = tmp_path / "manifest.json"
    path.write_text("}{", encoding="utf-8")
    assert Manifest.load(str(path)).records == []


def test_counts_are_derived_not_stored(tmp_path):
    manifest = Manifest()
    manifest.upsert(record("img_a1"))
    manifest.upsert(record("img_b1", "room_b", status=manifest_mod.STATUS_FAILED))

    counts = manifest.to_dict()["counts"]
    assert counts == {"total": 2, "ok": 1, "failed": 1, "rooms": 2}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_image_paths_are_relative_and_forward_slashed(tmp_path):
    root = str(tmp_path / "preview")
    image = os.path.join(root, "room_a", "viewpoint_01.png")
    assert manifest_mod.relative_image(root, image) == "room_a/viewpoint_01.png"


def test_resolve_returns_an_absolute_path(tmp_path):
    manifest = Manifest(root=str(tmp_path))
    resolved = manifest.resolve(record())
    assert os.path.isabs(resolved)
    assert resolved.endswith(os.path.join("preview", "room_a", "viewpoint_01.png"))
