"""
ArchX3D — Preview manifest
==========================
The record of what was rendered, from where, and out of which scene — written
to ``preview/manifest.json`` beside the images.

Why the images alone are not enough
-----------------------------------
``vision.similarity`` compares a render against the photograph its camera was
fitted to. To do that it needs the pairing (which PNG belongs to which
reference image) and it needs to know the render is current. A directory of
PNGs supplies neither: filenames are positional, and a stale image is
indistinguishable from a fresh one. The manifest carries both, so the
similarity pass can be a pure function of the manifest and the scene graph
rather than a filesystem crawl.

The hashes are in here for a second reason: debugging. When a preview
unexpectedly did or did not re-render, comparing the manifest's
``scene_hash`` / ``room_hash`` / ``camera_hash`` against a freshly computed set
localises the change immediately — building-wide, one room, or one camera.

Paths
-----
``image`` is relative to the manifest's own directory and always uses forward
slashes, so a manifest written on Windows resolves on a Linux render farm. Use
:meth:`Manifest.resolve` to get an absolute path back.

Stability
---------
Records are stored sorted by ``(room, viewpoint_id)`` and floats are rounded on
the way in, so re-running an unchanged scene rewrites a byte-identical file
apart from ``generated_at``. That keeps the manifest reviewable in a diff.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

MANIFEST_VERSION = "1.0"

#: A render that was produced this run.
STATUS_RENDERED = "rendered"
#: A render that was already valid on disk and was not repeated.
STATUS_CACHED = "cached"
#: A render that was attempted and failed; the image may not exist.
STATUS_FAILED = "failed"


@dataclass
class RenderRecord:
    """One preview image and everything needed to interpret it."""

    viewpoint_id: str = ""
    room: str = ""
    #: Relative to the manifest directory, forward slashes.
    image: str = ""
    #: The reference photograph this viewpoint was fitted to, when known. This
    #: is the other half of the pair the similarity engine scores.
    source_image: str = ""

    camera_hash: str = ""
    scene_hash: str = ""
    room_hash: str = ""

    width: int = 0
    height: int = 0

    timestamp: str = ""
    render_ms: int = 0
    status: str = STATUS_RENDERED
    #: Populated on failure; empty otherwise.
    error: str = ""
    #: ``blend`` when the camera came from the generated file, ``graph`` when
    #: it was rebuilt from the stored ViewPoint. Both reproduce the same pose;
    #: which path ran is worth knowing when a render looks wrong.
    camera_source: str = ""
    #: Auxiliary passes written beside the beauty image, ``{name: relative
    #: path}``. The evaluation engine reads these; an absent pass makes an
    #: axis unmeasured rather than zero, so the record must be honest about
    #: which ones actually exist.
    passes: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "viewpoint_id": self.viewpoint_id,
            "room": self.room,
            "image": self.image,
            "source_image": self.source_image,
            "camera_hash": self.camera_hash,
            "scene_hash": self.scene_hash,
            "room_hash": self.room_hash,
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp,
            "render_ms": self.render_ms,
            "status": self.status,
        }
        if self.passes:
            out["passes"] = dict(sorted(self.passes.items()))
        if self.camera_source:
            out["camera_source"] = self.camera_source
        if self.error:
            out["error"] = self.error
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RenderRecord":
        return RenderRecord(
            viewpoint_id=str(d.get("viewpoint_id", "")),
            room=str(d.get("room", "")),
            image=str(d.get("image", "")),
            source_image=str(d.get("source_image", "")),
            camera_hash=str(d.get("camera_hash", "")),
            scene_hash=str(d.get("scene_hash", "")),
            room_hash=str(d.get("room_hash", "")),
            width=int(d.get("width", 0) or 0),
            height=int(d.get("height", 0) or 0),
            timestamp=str(d.get("timestamp", "")),
            render_ms=int(d.get("render_ms", 0) or 0),
            status=str(d.get("status", STATUS_RENDERED)),
            error=str(d.get("error", "")),
            camera_source=str(d.get("camera_source", "")),
            passes={str(k): str(v) for k, v in (d.get("passes") or {}).items()},
        )

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_RENDERED, STATUS_CACHED)


@dataclass
class Manifest:
    """Every preview belonging to one build.

    Loaded, updated in place and rewritten by each run: a run that renders one
    room must not discard the records for the rooms it skipped, because the
    similarity pass reads the manifest as the complete picture.
    """

    version: str = MANIFEST_VERSION
    generated_at: str = ""
    #: Absolute path of the directory the manifest lives in. Not serialised —
    #: it is wherever the file was found — but needed to resolve ``image``.
    root: str = ""
    records: List[RenderRecord] = field(default_factory=list)
    #: Free-form: cache statistics, timings, the settings used.
    stats: Dict[str, Any] = field(default_factory=dict)

    # -- access -------------------------------------------------------------

    def record_for(self, viewpoint_id: str) -> Optional[RenderRecord]:
        for record in self.records:
            if record.viewpoint_id == viewpoint_id:
                return record
        return None

    def for_room(self, room_id: str) -> List[RenderRecord]:
        return [r for r in self.records if r.room == room_id]

    def rooms(self) -> List[str]:
        return sorted({r.room for r in self.records})

    def resolve(self, record: RenderRecord) -> str:
        """Absolute path of a record's image."""
        return os.path.normpath(os.path.join(self.root, record.image))

    def upsert(self, record: RenderRecord) -> None:
        """Replace the record for this viewpoint, or append it.

        Keyed on ``viewpoint_id`` alone: one viewpoint has exactly one current
        preview, and a re-render supersedes whatever was there.
        """
        for index, existing in enumerate(self.records):
            if existing.viewpoint_id == record.viewpoint_id:
                self.records[index] = record
                return
        self.records.append(record)

    def prune(self, keep: Iterable[str]) -> List[RenderRecord]:
        """Drop records for viewpoints that no longer exist, returning them.

        Called with the current graph's viewpoint ids, so a manifest cannot
        keep advertising a preview whose camera was deleted.
        """
        keep_set = set(keep)
        dropped = [r for r in self.records if r.viewpoint_id not in keep_set]
        if dropped:
            self.records = [r for r in self.records if r.viewpoint_id in keep_set]
        return dropped

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        ordered = sorted(self.records, key=lambda r: (r.room, r.viewpoint_id))
        return {
            "version": self.version,
            "generated_at": self.generated_at or _now(),
            "counts": {
                "total": len(ordered),
                "ok": sum(1 for r in ordered if r.ok),
                "failed": sum(1 for r in ordered if r.status == STATUS_FAILED),
                "rooms": len({r.room for r in ordered}),
            },
            "stats": self.stats,
            "renders": [r.to_dict() for r in ordered],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any], root: str = "") -> "Manifest":
        return Manifest(
            version=str(d.get("version", MANIFEST_VERSION)),
            generated_at=str(d.get("generated_at", "")),
            root=root,
            records=[RenderRecord.from_dict(r) for r in (d.get("renders") or [])],
            stats=dict(d.get("stats") or {}),
        )

    def save(self, path: str) -> None:
        """Write atomically, so a crash cannot leave an unreadable manifest."""
        self.generated_at = _now()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2)
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def load(path: str) -> "Manifest":
        """Read a manifest, or return an empty one rooted at its directory.

        A missing or corrupt manifest is not an error: the next run rebuilds
        it. The cache is the thing that decides what to re-render, and it is
        checked independently.
        """
        root = os.path.dirname(os.path.abspath(path))
        if not os.path.exists(path):
            return Manifest(root=root)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return Manifest.from_dict(json.load(handle), root=root)
        except (OSError, json.JSONDecodeError):
            return Manifest(root=root)

    # -- reporting ----------------------------------------------------------

    def summary(self) -> str:
        if not self.records:
            return "no previews"
        failed = sum(1 for r in self.records if r.status == STATUS_FAILED)
        parts = [f"{len(self.records)} preview(s) across {len(self.rooms())} room(s)"]
        if failed:
            parts.append(f"{failed} failed")
        return ", ".join(parts)


def relative_image(root: str, image_path: str) -> str:
    """Manifest-relative, forward-slashed form of an absolute image path."""
    rel = os.path.relpath(os.path.abspath(image_path), os.path.abspath(root))
    return rel.replace("\\", "/")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
