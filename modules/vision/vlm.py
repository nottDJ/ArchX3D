"""
ArchX3D — Vision-language model client
======================================
Thin, swappable wrapper around a multimodal model, with caching and retries.

Model choice
------------
Gemini 2.5 is the default because it is already a project dependency, it is
natively multimodal, it does open-vocabulary recognition (so new furniture
types need no retraining), it returns 2D grounding boxes, and it can reason
about *relationships* and *materials* — none of which a closed-vocabulary
detector like YOLO can do. See ``docs/VISION_PIPELINE.md`` for the full
evaluation against segmentation / depth / open-vocab detector alternatives.

SDK note
--------
``google-generativeai`` is deprecated upstream ("all support has ended"). This
module prefers the successor ``google-genai`` when it is installed and falls
back to the legacy SDK otherwise, so the migration is a dependency bump rather
than a code change.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .cache import ResponseCache

#: Long-edge cap applied before upload. Beyond roughly this size Gemini gains
#: no accuracy on room-scale scenes, but latency and token cost keep climbing.
#: Resizing also makes the cache key stable across source resolutions.
MAX_IMAGE_EDGE = 1536

DEFAULT_MODEL = "gemini-2.5-pro"
FALLBACK_MODEL = "gemini-2.5-flash"


class VLMError(RuntimeError):
    """Raised when the model cannot be reached or returns unusable output."""


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class VisionBackend(Protocol):
    """Minimal surface a backend must provide, so tests can substitute a fake."""

    name: str

    def generate_json(self, prompt: str, image_bytes: bytes, mime_type: str) -> str:
        """Return the model's raw text response for one image + prompt."""
        ...


@dataclass
class VLMResult:
    payload: Dict[str, Any]
    cached: bool
    model: str
    latency_s: float
    #: Populated when a retry or model fallback was needed.
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------


class GeminiBackend:
    """Gemini implementation, preferring the modern ``google-genai`` SDK."""

    def __init__(self, model: str, api_key: Optional[str] = None, temperature: float = 0.1):
        self.model = model
        self.name = f"gemini:{model}"
        # Low but non-zero temperature: fully greedy decoding makes the model
        # more prone to repeating a malformed structure on retry.
        self.temperature = temperature

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise VLMError(
                "GEMINI_API_KEY is not set. Export it, or run the pipeline with "
                "--skip-vision to build an unfurnished scene."
            )

        self._mode, self._client = self._connect(key)

    @staticmethod
    def _connect(key: str):
        """Bind to whichever Gemini SDK is available."""
        try:
            from google import genai  # type: ignore

            return "genai", genai.Client(api_key=key)
        except ImportError:
            pass

        try:
            import google.generativeai as legacy  # type: ignore

            legacy.configure(api_key=key)
            return "legacy", legacy
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VLMError(
                "No Gemini SDK found. Install 'google-genai' (preferred) or "
                "'google-generativeai'."
            ) from exc

    def generate_json(self, prompt: str, image_bytes: bytes, mime_type: str) -> str:
        if self._mode == "genai":
            from google.genai import types  # type: ignore

            response = self._client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=self.temperature,
                ),
            )
            return response.text or ""

        model = self._client.GenerativeModel(self.model)
        response = model.generate_content(
            [{"mime_type": mime_type, "data": image_bytes}, prompt],
            generation_config={
                "response_mime_type": "application/json",
                "temperature": self.temperature,
            },
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class VisionClient:
    """Caching, retrying front-end over a `VisionBackend`.

    Safe to call from several threads: the pipeline analyses images in
    parallel, so the counters are guarded and the backends are stateless per
    call.
    """

    def __init__(
        self,
        backend: VisionBackend,
        cache: Optional[ResponseCache] = None,
        max_attempts: int = 3,
        fallback_backend: Optional[VisionBackend] = None,
    ) -> None:
        self.backend = backend
        self.fallback_backend = fallback_backend
        self.cache = cache
        self.max_attempts = max_attempts
        self.calls_made = 0
        self.total_latency_s = 0.0
        self._lock = threading.Lock()

    def analyse_image(self, image_path: str, prompt: str) -> VLMResult:
        """Run one image through the model, returning parsed JSON.

        Raises `VLMError` only after every attempt (and any fallback model)
        has failed; callers treat that as "this image contributed nothing"
        rather than a fatal pipeline error.
        """
        image_bytes, mime_type = prepare_image(image_path)

        cache_key = None
        if self.cache is not None:
            cache_key = ResponseCache.build_key(
                _sha256_bytes(image_bytes), prompt, self.backend.name
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                return VLMResult(
                    payload=cached, cached=True, model=self.backend.name, latency_s=0.0
                )

        notes: List[str] = []
        started = time.time()
        payload = self._call_with_retries(prompt, image_bytes, mime_type, notes)
        latency = time.time() - started

        with self._lock:
            self.calls_made += 1
            self.total_latency_s += latency

        if self.cache is not None and cache_key is not None:
            self.cache.put(
                cache_key,
                payload,
                meta={
                    "image": os.path.basename(image_path),
                    "model": self.backend.name,
                    "latency_s": round(latency, 2),
                },
            )

        return VLMResult(
            payload=payload,
            cached=False,
            model=self.backend.name,
            latency_s=latency,
            notes=notes,
        )

    def _call_with_retries(
        self, prompt: str, image_bytes: bytes, mime_type: str, notes: List[str]
    ) -> Dict[str, Any]:
        backends = [self.backend]
        if self.fallback_backend is not None:
            backends.append(self.fallback_backend)

        last_error: Optional[Exception] = None

        for backend in backends:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    raw = backend.generate_json(prompt, image_bytes, mime_type)
                    parsed = extract_json_object(raw)
                    if parsed is None:
                        raise VLMError("response contained no parsable JSON object")
                    if backend is not self.backend:
                        notes.append(f"served by fallback model {backend.name}")
                    if attempt > 1:
                        notes.append(f"succeeded on attempt {attempt}")
                    return parsed
                except Exception as exc:  # noqa: BLE001 - surfaced via VLMError
                    last_error = exc
                    if attempt < self.max_attempts:
                        # Exponential backoff; most failures here are transient
                        # rate limits or truncated streams.
                        time.sleep(min(2 ** (attempt - 1), 8))
            notes.append(f"{backend.name} exhausted {self.max_attempts} attempts")

        raise VLMError(f"vision model failed: {last_error}") from last_error

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "model": self.backend.name,
            "live_calls": self.calls_made,
            "total_latency_s": round(self.total_latency_s, 2),
        }
        if self.cache is not None:
            out["cache"] = self.cache.stats()
        return out


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------


def prepare_image(path: str) -> tuple[bytes, str]:
    """Load an image, downscale it if oversized, and return bytes + MIME type.

    Pillow is optional: without it the original file is uploaded unchanged,
    which still works but costs more tokens on large photographs.
    """
    if not os.path.exists(path):
        raise VLMError(f"reference image not found: {path}")

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        with open(path, "rb") as fh:
            return fh.read(), _guess_mime(path)

    with Image.open(path) as img:
        img = img.convert("RGB")
        long_edge = max(img.size)
        if long_edge > MAX_IMAGE_EDGE:
            ratio = MAX_IMAGE_EDGE / float(long_edge)
            new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
            img = img.resize(new_size, Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=92)
        return buffer.getvalue(), "image/jpeg"


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from a model response.

    Even with ``response_mime_type=application/json`` set, responses
    occasionally arrive fenced or with a leading sentence, and long ones can be
    truncated mid-structure. This tries, in order: direct parse, fence
    stripping, outermost-brace extraction, then a brace-balance repair for
    truncated output.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    for candidate in (text, _FENCE_RE.sub("", text).strip()):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        return None

    snippet = text[start:]
    end = snippet.rfind("}")
    if end != -1:
        try:
            parsed = json.loads(snippet[: end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Truncated output: close whatever brackets are still open. Recovering a
    # partial object keeps the objects the model already emitted rather than
    # throwing away the entire (expensive) response.
    repaired = _balance_brackets(snippet)
    if repaired is not None:
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None

    return None


def _balance_brackets(text: str) -> Optional[str]:
    """Close unbalanced braces/brackets, trimming any dangling partial value.

    The truncation point and the bracket stack must be captured *together*:
    closing with the stack as it stands at the end of the string emits the
    wrong closers for a string that was cut back to an earlier point.
    """
    stack: List[str] = []
    in_string = False
    escaped = False

    # Last index at which a complete element ended, plus the stack at that point.
    last_safe = -1
    safe_stack: List[str] = []

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack:
                stack.pop()
            # A closed container is a safe place to truncate.
            last_safe, safe_stack = index, list(stack)

    if not stack:
        return text

    if last_safe < 0:
        # Nothing complete was emitted; close what is open and hope for a
        # usable skeleton.
        return text.rstrip().rstrip(",") + "".join(
            "}" if ch == "{" else "]" for ch in reversed(stack)
        )

    cut = text[: last_safe + 1].rstrip().rstrip(",")
    closing = "".join("}" if ch == "{" else "]" for ch in reversed(safe_stack))
    return cut + closing
