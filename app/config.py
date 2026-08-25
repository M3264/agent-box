"""Environment-driven settings.

Every value has a working default so the service starts with no configuration.
Secrets are never stored here — they are resolved per provider profile at call
time by ``app.orchestrator.providers``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    db_path: Path = field(default_factory=lambda: _env_path("AGENT_HUB_DB", ROOT / "data" / "agent-hub.db"))
    workspace_root: Path = field(default_factory=lambda: _env_path("AGENT_HUB_WORKSPACES", ROOT / "data" / "workspaces"))
    static_dir: Path = field(default_factory=lambda: ROOT / "static")

    host: str = field(default_factory=lambda: _env_str("AGENT_HUB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("AGENT_HUB_PORT", 8090))

    db_pool_size: int = field(default_factory=lambda: _env_int("AGENT_HUB_DB_POOL", 5))
    db_busy_timeout_ms: int = field(default_factory=lambda: _env_int("AGENT_HUB_DB_BUSY_TIMEOUT_MS", 5000))

    # Per-provider-call timeouts, seconds.
    provider_timeout: int = field(default_factory=lambda: _env_int("AGENT_HUB_PROVIDER_TIMEOUT", 180))
    provider_connect_timeout: int = field(default_factory=lambda: _env_int("AGENT_HUB_PROVIDER_CONNECT_TIMEOUT", 20))

    # Fallback location for provider secrets, used only when the profile's
    # secret_ref is not present in the environment.
    codex_config: Path = field(default_factory=lambda: _env_path("AGENT_HUB_CODEX_CONFIG", Path.home() / ".codex" / "config.toml"))

    log_level: str = field(default_factory=lambda: _env_str("AGENT_HUB_LOG_LEVEL", "INFO").upper())
    log_format: str = field(default_factory=lambda: _env_str("AGENT_HUB_LOG_FORMAT", "json").lower())

    # Grace period for in-flight jobs to unwind on shutdown, seconds. Kept well
    # under systemd's default TimeoutStopSec so `systemctl stop` never needs to
    # escalate to SIGKILL.
    shutdown_grace: int = field(default_factory=lambda: _env_int("AGENT_HUB_SHUTDOWN_GRACE", 10))

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
