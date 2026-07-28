"""
Tests for the persistent render cache.

The cache's contract is narrow and its failure modes are asymmetric: a false
miss costs a few hundred milliseconds, a false *hit* serves a stale evaluation
image and silently corrupts every similarity score taken from it. So the tests
lean on the conditions under which a hit is refused — a changed key, a deleted
image, a truncated file — rather than on the happy path.
"""

from __future__ import annotations

import json

import pytest

from render import cache


@pytest.fixture
def store(tmp_path):
    return cache.RenderCache(str(tmp_path / "cache" / "hash.json"))


def write_image(tmp_path, name="viewpoint_01.png", data=b"\x89PNG\r\n\x1a\n"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def entry(viewpoint_id="img_a1", key="k1", image="preview/room_a/viewpoint_01.png"):
    return cache.CacheEntry(viewpoint_id=viewpoint_id, key=key, image=image,
                            render_ms=120, timestamp="2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Hit and miss
# ---------------------------------------------------------------------------


def test_a_stored_entry_with_its_image_is_a_hit(store, tmp_path):
    image = write_image(tmp_path)
    store.store(entry())
    assert store.lookup("img_a1", "k1", image) is not None
    assert store.stats()["hits"] == 1


def test_an_unknown_viewpoint_is_a_miss(store, tmp_path):
    assert store.lookup("img_zz", "k1", write_image(tmp_path)) is None
    assert store.stats()["misses"] == 1


def test_a_changed_key_is_a_miss(store, tmp_path):
    image = write_image(tmp_path)
    store.store(entry())
    assert store.lookup("img_a1", "k2", image) is None


def test_a_deleted_image_is_a_miss(store, tmp_path):
    """Deleting a PNG to force a re-render is a thing people do, and must work."""
    image = write_image(tmp_path)
    store.store(entry())
    (tmp_path / "viewpoint_01.png").unlink()
    assert store.lookup("img_a1", "k1", image) is None


def test_a_zero_length_image_is_a_miss(store, tmp_path):
    """A render killed mid-write leaves an empty file; it is not a preview."""
    image = write_image(tmp_path, data=b"")
    store.store(entry())
    assert store.lookup("img_a1", "k1", image) is None


def test_a_disabled_cache_never_hits(tmp_path):
    store = cache.RenderCache(str(tmp_path / "hash.json"), enabled=False)
    store.store(entry())
    assert store.lookup("img_a1", "k1", write_image(tmp_path)) is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_entries_survive_a_round_trip(tmp_path):
    path = str(tmp_path / "hash.json")
    image = write_image(tmp_path)

    first = cache.RenderCache(path)
    first.store(entry())
    first.save()

    second = cache.RenderCache(path)
    hit = second.lookup("img_a1", "k1", image)
    assert hit is not None
    assert hit.render_ms == 120


def test_a_corrupt_cache_file_reads_as_empty(tmp_path):
    """A bad cache must cost a slow run, never a failed one."""
    path = tmp_path / "hash.json"
    path.write_text("{not json", encoding="utf-8")

    store = cache.RenderCache(str(path))
    assert store.entries() == {}
    assert store.lookup("img_a1", "k1", write_image(tmp_path)) is None


def test_a_cache_from_an_older_format_is_discarded(tmp_path):
    """Bumping the format version must invalidate wholesale, not half-load."""
    path = tmp_path / "hash.json"
    path.write_text(json.dumps({
        "format_version": "0",
        "entries": [entry().to_dict()],
    }), encoding="utf-8")

    assert cache.RenderCache(str(path)).entries() == {}


def test_saved_entries_are_sorted(tmp_path):
    """Stable ordering keeps hash.json reviewable in a diff."""
    path = tmp_path / "hash.json"
    store = cache.RenderCache(str(path))
    for viewpoint_id in ("img_c", "img_a", "img_b"):
        store.store(entry(viewpoint_id=viewpoint_id))
    store.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [e["viewpoint_id"] for e in payload["entries"]] == ["img_a", "img_b", "img_c"]


def test_saving_into_a_missing_directory_creates_it(tmp_path):
    store = cache.RenderCache(str(tmp_path / "deep" / "nested" / "hash.json"))
    store.store(entry())
    store.save()
    assert (tmp_path / "deep" / "nested" / "hash.json").exists()


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_forget_drops_one_entry(store, tmp_path):
    image = write_image(tmp_path)
    store.store(entry())
    store.forget("img_a1")
    assert store.lookup("img_a1", "k1", image) is None


def test_prune_drops_viewpoints_the_graph_no_longer_has(store):
    store.store(entry(viewpoint_id="img_a1"))
    store.store(entry(viewpoint_id="img_gone"))

    assert store.prune(["img_a1"]) == 1
    assert set(store.entries()) == {"img_a1"}


def test_stats_report_the_hit_rate(store, tmp_path):
    image = write_image(tmp_path)
    store.store(entry())
    store.lookup("img_a1", "k1", image)      # hit
    store.lookup("img_a1", "other", image)   # miss

    stats = store.stats()
    assert (stats["hits"], stats["misses"], stats["hit_rate"]) == (1, 1, 0.5)
