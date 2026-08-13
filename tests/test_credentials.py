"""
Tests for the stored Gemini API key.

The interesting cases are all about the *distinction between two sources*: a
key exported into the environment by whoever started the process, and one saved
through the UI. Conflating them is not hypothetical — the first implementation
did, and the symptom was that a key saved from Settings immediately reported
itself as environment-supplied, which made the UI refuse to edit or delete the
very setting it had just written.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules")
if MODULES not in sys.path:
    sys.path.insert(0, MODULES)


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    """A freshly imported module rooted at a temporary data directory.

    Re-imported per test because the module captures the environment at import
    time — which is the behaviour under test, so it cannot be monkeypatched
    afterwards.
    """
    monkeypatch.setenv("ARCHX3D_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    for name in ("app_paths", "credentials"):
        sys.modules.pop(name, None)

    module = importlib.import_module("credentials")
    yield module

    for name in ("app_paths", "credentials"):
        sys.modules.pop(name, None)


@pytest.fixture
def credentials_with_env(tmp_path, monkeypatch):
    """As above, but with a key already in the environment at import."""
    monkeypatch.setenv("ARCHX3D_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "env-supplied-key-0123456789")

    for name in ("app_paths", "credentials"):
        sys.modules.pop(name, None)

    module = importlib.import_module("credentials")
    yield module

    for name in ("app_paths", "credentials"):
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_empty_keys_are_rejected(credentials, value):
    with pytest.raises(ValueError, match="empty"):
        credentials.normalise(value)


def test_an_oversized_paste_is_rejected(credentials):
    with pytest.raises(ValueError, match="does not look like an API key"):
        credentials.normalise("x" * (credentials.MAX_KEY_LENGTH + 1))


def test_surrounding_whitespace_is_stripped(credentials):
    # Pasting from a terminal or a web page routinely brings a newline along,
    # and the resulting 401 gives no hint that this is why.
    assert credentials.normalise("  AIzaSyExample123  \n") == "AIzaSyExample123"


def test_a_non_string_is_rejected(credentials):
    with pytest.raises(ValueError):
        credentials.normalise(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_no_key_configured_initially(credentials):
    status = credentials.status()
    assert status == {
        "configured": False,
        "source": None,
        "hint": None,
        "editable": True,
    }


def test_saving_then_reading_back(credentials):
    credentials.save_key("AIzaSyExampleKey0123456789abcdef")

    status = credentials.status()
    assert status["configured"] is True
    assert status["source"] == "saved"
    assert status["editable"] is True


def test_a_saved_key_stays_editable(credentials):
    """The regression this module was rewritten for.

    ``save_key`` exports the key to ``os.environ`` so subprocesses inherit it.
    Deciding "was this set externally?" by reading ``os.environ`` therefore
    answers yes for a key the UI just saved, and the UI locks itself out.
    """
    credentials.save_key("AIzaSyExampleKey0123456789abcdef")

    assert os.environ["GEMINI_API_KEY"] == "AIzaSyExampleKey0123456789abcdef"
    assert credentials.externally_set() is False
    assert credentials.status()["source"] == "saved"
    assert credentials.status()["editable"] is True


def test_clearing_removes_the_key_completely(credentials):
    credentials.save_key("AIzaSyExampleKey0123456789abcdef")
    credentials.clear_key()

    assert credentials.status()["configured"] is False
    assert credentials.stored_key() is None
    # The copy exported for subprocesses must go too, or the next pipeline run
    # keeps using a key the user just deleted.
    assert "GEMINI_API_KEY" not in os.environ


def test_clearing_when_nothing_is_saved_is_not_an_error(credentials):
    credentials.clear_key()
    assert credentials.status()["configured"] is False


def test_saving_replaces_the_previous_key(credentials):
    credentials.save_key("AIzaSyFirstKey00000000000000000")
    credentials.save_key("AIzaSySecondKey1111111111111111")
    assert credentials.stored_key() == "AIzaSySecondKey1111111111111111"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_the_environment_outranks_a_saved_key(credentials_with_env):
    credentials_with_env.save_key("AIzaSySavedKey000000000000000000")

    assert credentials_with_env.resolve_key() == "env-supplied-key-0123456789"
    assert credentials_with_env.source() == "environment"
    assert credentials_with_env.externally_set() is True
    assert credentials_with_env.status()["editable"] is False


def test_an_external_key_is_not_removed_by_clearing(credentials_with_env):
    credentials_with_env.clear_key()
    # Not ours to delete: it belongs to whoever started the process.
    assert os.environ["GEMINI_API_KEY"] == "env-supplied-key-0123456789"
    assert credentials_with_env.status()["configured"] is True


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_the_status_payload_never_contains_the_key(credentials):
    secret = "AIzaSyVerySecretValue99887766554433"
    credentials.save_key(secret)

    serialised = json.dumps(credentials.status())
    assert secret not in serialised
    # The hint identifies which key is in use without disclosing it.
    assert credentials.status()["hint"] == "AIza…4433"


def test_a_short_key_is_fully_masked(credentials):
    credentials.save_key("shortkey")
    assert credentials.masked() == "•" * len("shortkey")


def test_a_corrupt_credentials_file_reads_as_no_key(credentials, tmp_path):
    (tmp_path / "credentials.json").write_text("{not json", encoding="utf-8")
    # Must not raise: a damaged file cannot be allowed to stop the server.
    assert credentials.stored_key() is None
