"""The pasted-key store: written 0600, and resolved with the right precedence.

A key pasted in the Settings form is the one secret this app accepts inbound and persists
itself. These pin the guarantees that make that safe: the value lands only in a mode-0600
file, a whitespace-only paste reads as absent, and ``resolve_secret`` still lets an
ops-provisioned environment variable win over a stored key. The masking guarantee (the
value never leaves via the API) is pinned in ``tests/test_config_api.py``.
"""

from __future__ import annotations

import os
import stat

import pytest

from app import secret_store
from app.orchestrator.providers import ProviderConfigError, resolve_secret

SECRET_VALUE = "sk-live-do-not-leak-this"


async def test_round_trip_and_clear() -> None:
    assert secret_store.get_secret("p") is None
    secret_store.set_secret("p", SECRET_VALUE)
    assert secret_store.get_secret("p") == SECRET_VALUE
    assert secret_store.clear_secret("p") is True
    assert secret_store.get_secret("p") is None
    assert secret_store.clear_secret("p") is False, "clearing an absent key is False, not an error"


async def test_value_is_stripped_and_blank_reads_absent() -> None:
    secret_store.set_secret("p", "  spaced  ")
    assert secret_store.get_secret("p") == "spaced"
    # A whitespace-only value stores empty, and `_load` drops empties defensively, so it
    # can never hand a job a blank credential — it simply reads back as absent.
    secret_store.set_secret("p", "   ")
    assert secret_store.get_secret("p") is None


async def test_the_file_is_owner_only() -> None:
    secret_store.set_secret("p", SECRET_VALUE)
    path = secret_store._path()
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"the secrets file must be owner-only, got {oct(mode)}"
    # And the value really is in that file (so the 0600 mode is what protects it) — the
    # one place it is allowed to be, and nowhere the API or DB can reach.
    assert SECRET_VALUE in path.read_text()


async def test_providers_are_independent() -> None:
    secret_store.set_secret("a", "key-a")
    secret_store.set_secret("b", "key-b")
    assert secret_store.get_secret("a") == "key-a"
    assert secret_store.get_secret("b") == "key-b"
    secret_store.clear_secret("a")
    assert secret_store.get_secret("a") is None
    assert secret_store.get_secret("b") == "key-b", "clearing one leaves the others"


# ------------------------------------------------------------ resolve precedence


async def test_resolve_prefers_env_over_saved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "from-env")
    secret_store.set_secret("prov", "from-file")
    assert resolve_secret("MY_KEY", "prov") == "from-env", "an ops-provisioned var wins"


async def test_resolve_falls_through_to_the_saved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    secret_store.set_secret("prov", "from-file")
    # A secret_ref is named but the environment has nothing, so the pasted key authenticates.
    assert resolve_secret("MY_KEY", "prov") == "from-file"


async def test_resolve_authenticates_a_ref_less_provider() -> None:
    """A provider added on the website may name no env var at all — the pasted key is it."""
    secret_store.set_secret("web-added", "pasted")
    assert resolve_secret(None, "web-added") == "pasted"


async def test_resolve_is_none_when_nothing_is_named_or_saved() -> None:
    assert resolve_secret(None, "bare") is None


async def test_resolve_raises_when_a_named_ref_resolves_nowhere() -> None:
    # conftest points the codex fallback at a missing file, so a named ref with no env and
    # no saved key resolves nowhere — which is a configuration error, not a silent None.
    with pytest.raises(ProviderConfigError):
        resolve_secret("NO_SUCH_ENV_VAR", "bare")
