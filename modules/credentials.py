"""
ArchX3D — API credentials
=========================
Stores the Gemini API key for the desktop app, where there is no shell to
export an environment variable from.

Why this exists
---------------
Everything that talks to Gemini reads ``GEMINI_API_KEY`` from the environment
(``modules/vision/vlm.py``). That is exactly right for a developer running the
CLI and useless for someone who installed a ``.exe``: they would have to open
System Properties and add an environment variable before the AI features did
anything, and until they did, the pipeline would quietly produce unfurnished
shells with no indication why.

So the key can also be saved to a file, and this module is the single place
that knows about both sources.

Precedence, and why
-------------------
``GEMINI_API_KEY`` in the environment **wins** over the stored file. A developer
who exports a key for one run expects that run to use it, and a CI job setting
the variable must not be silently overridden by whatever a desktop user saved
months earlier. The file is the fallback, not the authority.

What is not done
----------------
The key is stored in plain text, readable by the user account that saved it.
This is deliberate and worth stating plainly rather than implying more: it sits
beside the projects in the per-user app-data directory, protected by the same
OS account boundary as everything else there. Encrypting it would need a key to
decrypt it, which would live next to it — security theatre, not security. On a
shared machine, use the environment variable instead.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict, Optional

from app_paths import data_path

ENV_VAR = "GEMINI_API_KEY"

#: Whether the *environment* supplied a key, captured once at import — before
#: this module has had any chance to write to ``os.environ`` itself.
#:
#: This distinction is load-bearing. ``apply_to_environment`` deliberately puts
#: the effective key into ``os.environ`` so subprocesses inherit it, which means
#: that after saving a key through the UI the variable is set either way.
#: Reading ``os.environ`` at that point to decide "did the user configure this
#: externally?" answers yes for a key the UI just saved — so the UI would then
#: refuse to edit or delete its own setting. Only the value present at startup
#: is evidence of an external key.
_ENV_KEY_AT_STARTUP: Optional[str] = os.environ.get(ENV_VAR) or None

#: Longest key we will store. Real keys are well under this; the cap exists so
#: a paste of the wrong thing (a whole file, a JSON blob) is rejected at the
#: door rather than written to disk.
MAX_KEY_LENGTH = 400


def _path() -> str:
    return data_path("credentials.json")


def _read() -> Dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Missing or corrupt: treat as "no key stored". A malformed credentials
        # file must not stop the server starting.
        return {}


def normalise(key: str) -> str:
    """Validate and clean a key on its way in.

    Raises ``ValueError`` with a message intended for the user.
    """
    if not isinstance(key, str):
        raise ValueError("the API key must be text")

    # Copy-pasting from a web page or a terminal routinely brings whitespace,
    # newlines or zero-width characters with it, none of which belong in a
    # credential and all of which produce a baffling 401 later.
    cleaned = "".join(ch for ch in key.strip() if ch.isprintable() and not ch.isspace())

    if not cleaned:
        raise ValueError("the API key is empty")
    if len(cleaned) > MAX_KEY_LENGTH:
        raise ValueError(
            f"that does not look like an API key — it is {len(cleaned)} "
            f"characters, and the limit is {MAX_KEY_LENGTH}"
        )
    return cleaned


def stored_key() -> Optional[str]:
    """The key saved to disk, if any. Ignores the environment."""
    value = _read().get("gemini_api_key")
    return value if isinstance(value, str) and value else None


def resolve_key() -> Optional[str]:
    """The key that should actually be used — an external one wins."""
    return _ENV_KEY_AT_STARTUP or stored_key()


def source() -> Optional[str]:
    """Where the effective key came from: ``environment``, ``saved`` or None."""
    if _ENV_KEY_AT_STARTUP:
        return "environment"
    if stored_key():
        return "saved"
    return None


def externally_set() -> bool:
    """True when the environment supplied a key this process cannot manage."""
    return _ENV_KEY_AT_STARTUP is not None


def save_key(key: str) -> None:
    """Persist a key, replacing any previous one."""
    cleaned = normalise(key)
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"gemini_api_key": cleaned}, fh)

    # Owner-only where the platform honours it. Windows ignores this, which is
    # why the module docstring does not claim the file is protected.
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    apply_to_environment()


def clear_key() -> None:
    """Forget the saved key. The environment variable is never touched."""
    try:
        os.remove(_path())
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f"could not remove the saved key: {exc}") from exc

    # Drop the copy we exported for subprocesses. An externally supplied key is
    # not ours to remove — it belongs to whoever started the process.
    if not _ENV_KEY_AT_STARTUP:
        os.environ.pop(ENV_VAR, None)


def apply_to_environment() -> bool:
    """Put the effective key into ``os.environ`` for this process and its children.

    The pipeline stages run as subprocesses and read the environment, so this
    is what makes a key saved through the UI reach the code that uses it —
    without threading a credential through six subprocess invocations.

    Returns whether a key is now present.
    """
    key = resolve_key()
    if key:
        os.environ[ENV_VAR] = key
        return True
    return False


def masked() -> Optional[str]:
    """A hint that identifies the key without disclosing it, e.g. ``AIza…7f3D``.

    Enough for a user to tell which key is saved; useless to anyone reading it.
    """
    key = resolve_key()
    if not key:
        return None
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def status() -> Dict[str, Any]:
    """What the UI needs to render the credential section. Never the key."""
    return {
        "configured": resolve_key() is not None,
        "source": source(),
        "hint": masked(),
        # An environment-supplied key cannot be edited from the UI, and saying
        # so is better than offering a Save button that appears to do nothing.
        "editable": not externally_set(),
    }
