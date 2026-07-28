"""
ArchX3D — Vision response cache
===============================
Content-addressed disk cache for VLM responses.

The brief asks for accuracy without a big time penalty and for redundant AI
calls to be avoided. Scene analysis is the only expensive step in the pipeline
(one multimodal call per image, seconds each), and it is *pure*: the same
image, prompt and model always warrant the same answer. Caching on a hash of
exactly those three inputs means re-running the pipeline after a Blender or
placement tweak costs nothing, which is the common case during iteration.

The key deliberately includes the prompt and model id, so editing the prompt or
switching models invalidates cleanly instead of serving stale structure.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

#: Bump when the cached payload's meaning changes in a backwards-incompatible
#: way, to invalidate every existing entry at once.
CACHE_FORMAT_VERSION = "2"


class ResponseCache:
    """A tiny JSON file cache, safe for the parallel analysis workers.

    Entries are written via a temp file plus atomic replace, so concurrent
    writers cannot produce a torn file; the lock only guards the hit/miss
    counters, which would otherwise under-count under concurrency.
    """

    def __init__(self, directory: str, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        if self.enabled:
            os.makedirs(self.directory, exist_ok=True)

    # -- key construction ---------------------------------------------------

    @staticmethod
    def hash_file(path: str) -> str:
        """SHA-256 of a file's bytes, read in chunks for large images."""
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def build_key(*parts: str) -> str:
        """Combine arbitrary string parts into one stable key."""
        digest = hashlib.sha256()
        digest.update(CACHE_FORMAT_VERSION.encode("utf-8"))
        for part in parts:
            digest.update(b"\x00")
            digest.update(part.encode("utf-8"))
        return digest.hexdigest()

    def _path_for(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.json")

    # -- access -------------------------------------------------------------

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        path = self._path_for(key)
        if not os.path.exists(path):
            with self._lock:
                self.misses += 1
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # A truncated or corrupt entry should behave exactly like a miss.
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return entry.get("payload")

    def put(self, key: str, payload: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> None:
        if not self.enabled:
            return
        entry = {
            "cached_at": time.time(),
            "format_version": CACHE_FORMAT_VERSION,
            "meta": meta or {},
            "payload": payload,
        }
        path = self._path_for(key)
        # Write via a temp file + replace so a crash mid-write cannot leave a
        # partially-written entry that later reads as a corrupt hit.
        try:
            fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            # Caching is an optimisation; never let it break the run.
            pass

    # -- reporting ----------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
