"""Provider API keys pasted on the website, kept in a protected file.

The gap this closes: a provider profile stores only the *name* of an environment
variable (its ``secret_ref``); the key itself had to be placed in the process
environment or ``~/.codex/config.toml`` by hand, over SSH. This lets the operator paste
a key in the Settings form instead — it reaches the server once, over the existing TLS +
basic-auth, and is written here.

Where it lives and what it never does: the value is stored in
``settings.provider_secrets_file`` (``data/provider_secrets.json``) at mode 0600, keyed
by provider id. It is **never** written to the database, **never** returned by any API
response (the Settings screen sees only the boolean ``has_saved_secret``), and **never**
logged (only the provider id and the action are). This mirrors exactly how the VAPID
private key is handled in ``app.push`` — the one other secret this app persists itself.

``resolve_secret`` in ``app.orchestrator.providers`` reads this store live, between the
environment (which still wins, so an ops-provisioned var overrides a pasted key) and the
codex-config fallback.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.config import settings
from app.logging_setup import get_logger

log = get_logger("agent_hub.secret_store")

#: Serialises the read-modify-write in set/clear against a concurrent reader, so a
#: fan-out of provider builds never sees a half-written file.
_lock = threading.Lock()


def _path() -> Path:
    """Resolved live rather than captured, so a test that repoints the DB (and with it
    ``provider_secrets_file``) is honoured without re-importing this module."""
    return settings.provider_secrets_file


def _load(path: Path) -> dict[str, str]:
    """The stored ``{provider_id: value}`` map, or empty if absent or unreadable.

    Non-string or empty values are dropped defensively, so a hand-corrupted file can
    never hand a job a bogus credential."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str) and value}


def _persist(path: Path, data: dict[str, str]) -> None:
    """Write the whole map as JSON at mode 0600, created private from the start.

    Mirrors ``app.push._persist``: opened 0600 rather than written-then-chmod'd so the
    keys are never briefly world-readable; the explicit chmod covers overwriting a
    pre-existing file whose mode we did not choose.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.chmod(path, 0o600)


def get_secret(profile_id: str) -> str | None:
    """The pasted key for a provider, or None. Read live so a just-saved key is seen."""
    with _lock:
        return _load(_path()).get(profile_id) or None


def set_secret(profile_id: str, value: str) -> None:
    """Store (or replace) a provider's key. The value never leaves this file."""
    value = value.strip()
    with _lock:
        path = _path()
        data = _load(path)
        data[profile_id] = value
        _persist(path, data)
    log.info("provider secret stored", extra={"provider": profile_id})


def clear_secret(profile_id: str) -> bool:
    """Remove a provider's stored key. True if one was there, False if not."""
    with _lock:
        path = _path()
        data = _load(path)
        if profile_id not in data:
            return False
        del data[profile_id]
        _persist(path, data)
    log.info("provider secret cleared", extra={"provider": profile_id})
    return True
