"""
ArchX3D — Scene hashing and the persistent render cache
=======================================================
Decides, without rendering anything, whether a stored preview is still valid.

The problem
-----------
A refinement loop re-runs the preview pass after every edit. Rendering every
viewpoint of a whole building each time is the difference between a five-second
iteration and a five-minute one, so the pipeline has to answer "did anything
that could change *this* image actually change?" — cheaply and correctly.

Answering it with a timestamp is wrong (regenerating the ``.blend`` rewrites
every byte even when nothing was edited) and answering it with the ``.blend``'s
own digest is worse (Blender embeds paths and timestamps, so the file differs
run to run for an identical scene). Both would give a cache that never hits.

So the hash is taken over the *inputs* that produce the image, not over the
artefact: the scene graph, the DXF geometry, and the render settings.

The invalidation model
----------------------
Three independent digests are combined per viewpoint::

    key = H( pipeline+settings , scene_hash , room_hash[room] , camera_hash )

``scene_hash``   Building-wide facts: schema version, geometry, graph-level
                 finishes, dominant style, and anything that could not be
                 attributed to a single room.
``room_hash``    One room's contents: its finishes, palette, lighting
                 environment, style, walls, objects, luminaires and openings.
``camera_hash``  One viewpoint's pose and framing.

The attribution is what makes invalidation surgical, and it is deliberately
conservative in one direction: anything the graph does not place in a room
(an object with no ``room_id``, an architectural element, a wall no room
claims) folds into ``scene_hash`` and therefore invalidates the whole
building. An unattributable change could be visible from anywhere, and a
re-render costs a few hundred milliseconds while a stale evaluation image
costs a wrong similarity score.

Where it under-invalidates, and why that is accepted
----------------------------------------------------
A room's hash covers that room only, so repainting the kitchen does not
re-render a living-room view that happens to see the kitchen through a
doorway. That is a real, known gap. It is accepted by default because these
are evaluation renders scored per-viewpoint against a photograph of *that*
room, and because the alternative — invalidating neighbours transitively —
degrades toward "one room invalidates the building" in an open-plan layout,
which is the exact failure this design exists to avoid.

Set ``include_neighbours=True`` to trade that back: a room's hash then folds in
the rooms it is ``connected_to``, one hop, no transitivity.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Bump when the *meaning* of a hash changes — new inputs folded in, a
#: different attribution rule — to invalidate every existing key at once.
HASH_VERSION = "1"

#: Bump when the on-disk cache layout changes incompatibly.
CACHE_FORMAT_VERSION = "1"

#: Positions are metres and rotations degrees; six decimals is far below any
#: difference a 640x360 preview could show, and rounding keeps float noise in
#: the graph's JSON round-trip from producing spurious cache misses.
_PRECISION = 6


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def _normalise(value: Any) -> Any:
    """Reduce a value to a form whose JSON encoding is stable.

    Dicts are not sorted here — ``json.dumps(sort_keys=True)`` does that — but
    floats are rounded and non-finite values are made representable, because
    ``float('nan')`` encodes as invalid JSON and would otherwise poison a key.
    """
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        rounded = round(value, _PRECISION)
        # -0.0 and 0.0 are equal but encode differently.
        return rounded + 0.0
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


def canonical(value: Any) -> str:
    """A deterministic JSON encoding, independent of dict insertion order."""
    return json.dumps(
        _normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def digest(*parts: Any) -> str:
    """SHA-256 over canonicalised parts, truncated to 32 hex characters.

    128 bits is far more than enough to make an accidental collision between
    two scenes impossible in practice, and a short hash keeps ``hash.json``
    and the manifest readable when a human is debugging an unexpected
    re-render.
    """
    hasher = hashlib.sha256()
    hasher.update(HASH_VERSION.encode("utf-8"))
    for part in parts:
        hasher.update(b"\x1f")
        hasher.update(canonical(part).encode("utf-8"))
    return hasher.hexdigest()[:32]


def hash_file(path: str) -> str:
    """Digest of a file's bytes, or ``"absent"`` when it is not there.

    Used for the DXF-derived geometry, which is an input to every render but
    is not carried inside the scene graph.
    """
    if not path or not os.path.exists(path):
        return "absent"
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError:
        return "unreadable"
    return hasher.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Attribution: which room owns which record
# ---------------------------------------------------------------------------


#: Bucket for anything the graph does not attribute to a room. Folded into the
#: building-wide hash, so it invalidates everything — see the module docstring.
UNATTRIBUTED = "__scene__"


def _rooms_for_wall(graph) -> Dict[str, List[str]]:
    """Wall id -> the rooms that claim it.

    A wall between two rooms is claimed by both, so editing it correctly
    invalidates both and only those two.
    """
    owners: Dict[str, List[str]] = {}
    for room in getattr(graph, "rooms", []) or []:
        for wall_id in getattr(room, "wall_ids", []) or []:
            owners.setdefault(wall_id, []).append(room.id)
    return owners


def _opening_rooms(opening, wall_owners: Dict[str, List[str]]) -> List[str]:
    """An opening belongs to its room, or to whichever rooms its wall serves."""
    if getattr(opening, "room_id", ""):
        return [opening.room_id]
    return list(wall_owners.get(getattr(opening, "wall_id", ""), []))


def _bucket(records: Iterable[Any], room_ids: set) -> Dict[str, List[Any]]:
    """Group records carrying a ``room_id`` by room, unknown ids unattributed."""
    grouped: Dict[str, List[Any]] = {}
    for record in records:
        room_id = getattr(record, "room_id", "") or ""
        key = room_id if room_id in room_ids else UNATTRIBUTED
        grouped.setdefault(key, []).append(record)
    return grouped


def _sorted_dicts(records: Sequence[Any]) -> List[Dict[str, Any]]:
    """Serialise records to dicts in id order, so list order cannot leak in.

    The vision pipeline analyses images concurrently, so the order objects land
    in the graph is not stable between runs. Sorting here is what stops an
    unchanged scene from hashing differently on the second run.
    """
    return [r.to_dict() for r in sorted(records, key=lambda r: getattr(r, "id", ""))]


# ---------------------------------------------------------------------------
# The hashes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SceneHashes:
    """Every digest needed to key a build's previews.

    Produced once per run by :func:`compute`, then consulted per viewpoint.
    """

    #: Building-wide inputs, plus everything unattributable to a room.
    scene: str
    #: Room id -> that room's content digest.
    rooms: Dict[str, str] = field(default_factory=dict)
    #: What went into ``scene``, kept for debugging an unexpected re-render.
    components: Dict[str, str] = field(default_factory=dict)

    def for_room(self, room_id: str) -> str:
        """A room's digest, falling back to the scene digest when unknown.

        A viewpoint whose ``room_id`` does not resolve still needs a stable
        key; tying it to the scene digest is the conservative answer.
        """
        return self.rooms.get(room_id, self.scene)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene": self.scene,
            "rooms": dict(self.rooms),
            "components": dict(self.components),
        }


def compute(graph, geometry_path: str = "", settings_fingerprint: str = "") -> SceneHashes:
    """Hash a scene graph into one building digest plus one digest per room.

    ``geometry_path`` is the DXF-derived ``geometry.json``: it defines the
    walls the graph only annotates, so a re-extraction with a different scale
    must invalidate every preview.

    ``settings_fingerprint`` folds the render settings and pipeline version in
    at the top level, so changing the sample count or the view transform
    re-renders everything — which it should, since it changes every pixel.
    """
    rooms = list(getattr(graph, "rooms", []) or [])
    room_ids = {room.id for room in rooms}
    wall_owners = _rooms_for_wall(graph)

    objects_by_room = _bucket(getattr(graph, "objects", []) or [], room_ids)
    lights_by_room = _bucket(getattr(graph, "lights", []) or [], room_ids)

    # Openings resolve through their wall when they name no room of their own.
    openings_by_room: Dict[str, List[Any]] = {}
    for opening in getattr(graph, "openings", []) or []:
        owners = [r for r in _opening_rooms(opening, wall_owners) if r in room_ids]
        for owner in owners or [UNATTRIBUTED]:
            openings_by_room.setdefault(owner, []).append(opening)

    walls_by_room: Dict[str, List[Any]] = {}
    for wall in getattr(graph, "walls", []) or []:
        owners = [r for r in wall_owners.get(wall.id, []) if r in room_ids]
        for owner in owners or [UNATTRIBUTED]:
            walls_by_room.setdefault(owner, []).append(wall)

    # -- building-wide -------------------------------------------------------
    components = {
        "settings": settings_fingerprint or "default",
        "schema": str(getattr(graph, "schema_version", "")),
        "geometry": hash_file(geometry_path),
        "surfaces": digest(
            graph.floor.to_dict() if getattr(graph, "floor", None) else {},
            graph.ceiling.to_dict() if getattr(graph, "ceiling", None) else {},
            str(getattr(graph, "ceiling_type", "")),
        ),
        # Style drives material substitution across every room, so it is
        # building-wide even though it is derived from per-room labels.
        "style": digest(_dominant_style(graph)),
        "architecture": digest(_sorted_dicts(getattr(graph, "architecture", []) or [])),
        "unattributed": digest(
            _sorted_dicts(objects_by_room.get(UNATTRIBUTED, [])),
            _sorted_dicts(lights_by_room.get(UNATTRIBUTED, [])),
            _sorted_dicts(openings_by_room.get(UNATTRIBUTED, [])),
            _sorted_dicts(walls_by_room.get(UNATTRIBUTED, [])),
        ),
        # The roster itself: adding or removing a room changes the building
        # even if every surviving room is untouched.
        "roster": digest(sorted(room_ids)),
    }
    scene = digest(components)

    # -- per room ------------------------------------------------------------
    room_hashes: Dict[str, str] = {}
    for room in rooms:
        room_hashes[room.id] = digest(
            room.to_dict(),
            _sorted_dicts(walls_by_room.get(room.id, [])),
            _sorted_dicts(objects_by_room.get(room.id, [])),
            _sorted_dicts(lights_by_room.get(room.id, [])),
            _sorted_dicts(openings_by_room.get(room.id, [])),
        )

    return SceneHashes(scene=scene, rooms=room_hashes, components=components)


def with_neighbours(hashes: SceneHashes, graph) -> SceneHashes:
    """Fold each room's direct neighbours into its digest.

    Opt-in. Repainting a room then also invalidates the rooms that can see it
    through a doorway, which is more correct for open-plan layouts and less
    surgical everywhere else. One hop only — transitive closure would make a
    connected floor plan behave as a single room.
    """
    neighbours: Dict[str, str] = {}
    for room in getattr(graph, "rooms", []) or []:
        adjacent = sorted(
            hashes.rooms[r] for r in (room.connected_to or []) if r in hashes.rooms
        )
        neighbours[room.id] = digest(hashes.rooms.get(room.id, ""), adjacent)
    return SceneHashes(scene=hashes.scene, rooms=neighbours, components=hashes.components)


def _dominant_style(graph) -> List[Any]:
    """Area-weighted style label, mirroring ``blender_generator._dominant_style``.

    Duplicated rather than imported because that function lives in a module
    that imports ``bpy``; hashing must work outside Blender.
    """
    scores: Dict[str, float] = {}
    for room in getattr(graph, "rooms", []) or []:
        if room.style and room.style != "unknown":
            weight = max(room.area, 1.0) * max(room.style_confidence, 0.2)
            scores[room.style] = scores.get(room.style, 0.0) + weight
    if not scores:
        return ["unknown", 0.0]
    best = max(sorted(scores), key=lambda key: scores[key])
    return [best, round(scores[best] / sum(scores.values()), 4)]


def camera_hash(viewpoint, width: int, height: int) -> str:
    """Digest of one viewpoint's pose and framing.

    Only the parameters that determine which rays are cast: position, heading,
    pitch, field of view and the pixel grid. The image id is identity, not
    pose, and is deliberately excluded — two viewpoints standing in the same
    place hash the same, which is true and harmless.

    ``aspect`` is included even though it is normally implied by the pixel
    dimensions, because it survives into the manifest and a similarity
    comparison reads it.
    """
    return digest(
        {
            "position": viewpoint.position.to_dict(),
            "yaw": round(viewpoint.yaw, _PRECISION),
            "pitch_deg": round(viewpoint.pitch_deg, _PRECISION),
            "vertical_fov_deg": round(viewpoint.vertical_fov_deg, _PRECISION),
            "aspect": round(viewpoint.aspect, _PRECISION),
            "width": int(width),
            "height": int(height),
        }
    )


def render_key(scene: str, room: str, camera: str, image_path: str) -> str:
    """The full cache key for one preview.

    ``image_path`` is folded in so that renumbering a room's viewpoints — which
    changes where the PNG is written without changing anything about the scene
    — is treated as a miss rather than silently leaving the old file in place.
    """
    return digest(scene, room, camera, image_path.replace("\\", "/"))


# ---------------------------------------------------------------------------
# Persistent cache
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """What a previous run recorded about one viewpoint's preview."""

    viewpoint_id: str
    key: str
    image: str
    scene_hash: str = ""
    room_hash: str = ""
    camera_hash: str = ""
    render_ms: int = 0
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewpoint_id": self.viewpoint_id,
            "key": self.key,
            "image": self.image,
            "scene_hash": self.scene_hash,
            "room_hash": self.room_hash,
            "camera_hash": self.camera_hash,
            "render_ms": self.render_ms,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CacheEntry":
        return CacheEntry(
            viewpoint_id=str(d.get("viewpoint_id", "")),
            key=str(d.get("key", "")),
            image=str(d.get("image", "")),
            scene_hash=str(d.get("scene_hash", "")),
            room_hash=str(d.get("room_hash", "")),
            camera_hash=str(d.get("camera_hash", "")),
            render_ms=int(d.get("render_ms", 0) or 0),
            timestamp=str(d.get("timestamp", "")),
        )


class RenderCache:
    """``cache/hash.json`` — one entry per viewpoint, keyed by content.

    Small enough to keep in memory and rewrite atomically: a building has tens
    of viewpoints, not thousands. The atomic replace matters because the
    scheduler may finish several batches at once, and a torn file would read
    back as a total cache loss on the next run.

    A hit requires *both* a matching key and a readable image. Deleting a PNG
    to force a re-render is a thing people do, and it must work.
    """

    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self._entries: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.enabled or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A corrupt cache must behave as an empty one, never as an error:
            # the worst case is a slow run, and that is always recoverable.
            return
        if str(payload.get("format_version", "")) != CACHE_FORMAT_VERSION:
            return
        for raw in payload.get("entries", []) or []:
            entry = CacheEntry.from_dict(raw)
            if entry.viewpoint_id:
                self._entries[entry.viewpoint_id] = entry

    def save(self) -> None:
        if not self.enabled:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        payload = {
            "format_version": CACHE_FORMAT_VERSION,
            "hash_version": HASH_VERSION,
            "updated_at": timestamp(),
            "entries": [
                self._entries[k].to_dict() for k in sorted(self._entries)
            ],
        }
        try:
            os.makedirs(directory, exist_ok=True)
            handle_fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # Caching is an optimisation; never let it fail a run.
            pass

    # -- access -------------------------------------------------------------

    def lookup(self, viewpoint_id: str, key: str, image_path: str,
               also: Iterable[str] = ()) -> Optional[CacheEntry]:
        """Return the entry when this exact preview is already on disk.

        ``also`` lists the auxiliary pass images this preview is expected to
        have. They are checked because the evaluation engine reads them: a
        preview whose depth pass was deleted is not a usable evaluation
        input, however current the beauty render is.
        """
        if not self.enabled:
            with self._lock:
                self.misses += 1
            return None

        entry = self._entries.get(viewpoint_id)
        fresh = (
            entry is not None
            and entry.key == key
            and _readable(image_path)
            and all(_readable(path) for path in also)
        )
        with self._lock:
            if fresh:
                self.hits += 1
            else:
                self.misses += 1
        return entry if fresh else None

    def store(self, entry: CacheEntry) -> None:
        with self._lock:
            self._entries[entry.viewpoint_id] = entry

    def forget(self, viewpoint_id: str) -> None:
        with self._lock:
            self._entries.pop(viewpoint_id, None)

    def prune(self, keep: Iterable[str]) -> int:
        """Drop entries for viewpoints the graph no longer contains."""
        keep_set = set(keep)
        with self._lock:
            stale = [k for k in self._entries if k not in keep_set]
            for key in stale:
                del self._entries[key]
        return len(stale)

    def entries(self) -> Dict[str, CacheEntry]:
        return dict(self._entries)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "entries": len(self._entries),
        }


def _readable(path: str) -> bool:
    """A file that exists and holds something. An empty PNG is a failed write."""
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0


def timestamp() -> str:
    """UTC, seconds resolution, ISO-8601 with an explicit zone.

    Shared with the manifest so a cache entry and the record describing the
    same image carry the same clock.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
