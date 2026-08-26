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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.replace(",", " ").split() if part.strip())


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

    # -- agent tools ---------------------------------------------------------
    # Off turns every phase back into a single text-only call, which is the
    # pre-tools behaviour and the escape hatch if a provider misbehaves.
    tools_enabled: bool = field(default_factory=lambda: _env_bool("AGENT_HUB_TOOLS", True))
    #: Backend used by jobs that do not name one. Ships confined.
    sandbox_default: str = field(default_factory=lambda: _env_str("AGENT_HUB_SANDBOX", "sandboxed"))
    #: Provider round-trips per phase before the loop asks for a closing summary.
    tool_max_turns: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_MAX_TURNS", 12))
    #: Seconds a single command may run.
    tool_timeout: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_TIMEOUT", 120))
    #: Seconds a whole phase may spend in its tool loop, provider time included.
    tool_wall_clock: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_WALL_CLOCK", 1800))
    #: Bytes of each stream fed back to the model and stored on the row. The full
    #: stream is always on disk in the workspace.
    tool_output_limit: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_OUTPUT", 12000))
    #: Hard ceiling before a command is killed for flooding, bytes.
    tool_output_max: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_OUTPUT_MAX", 32 * 1024 * 1024))
    #: Bytes a single read_file/write_file call may move.
    tool_file_limit: int = field(default_factory=lambda: _env_int("AGENT_HUB_TOOL_FILE_LIMIT", 256 * 1024))
    #: Commands share the host network by default — installing a dependency or
    #: calling an API is most of what a CLI session is for.
    tool_network: bool = field(default_factory=lambda: _env_bool("AGENT_HUB_TOOL_NETWORK", True))
    #: Hosts `fetch` may reach. Empty means any, which is the default because the
    #: shell already has the network; an allowlist here would be theatre.
    fetch_allow_hosts: tuple[str, ...] = field(default_factory=lambda: _env_tuple("AGENT_HUB_FETCH_HOSTS", ()))
    #: Seconds a single fetch may take.
    fetch_timeout: int = field(default_factory=lambda: _env_int("AGENT_HUB_FETCH_TIMEOUT", 30))

    # -- asking the operator -------------------------------------------------
    #: Seconds a question waits before the agent is told to proceed on its own
    #: judgement. Unlike an approval, which waits forever because the operator
    #: explicitly gated that action, a question is the agent's own initiative — and a
    #: job hung overnight because nobody was watching is worse than one that carried
    #: on and said which assumption it made. Set to 0 to wait indefinitely.
    question_timeout: int = field(default_factory=lambda: _env_int("AGENT_HUB_QUESTION_TIMEOUT", 1800))
    #: Default cap on total tokens for a job, across every round. 0 means no cap. A
    #: job may still be created with its own budget, which wins.
    token_budget_default: int = field(
        default_factory=lambda: _env_int("AGENT_HUB_TOKEN_BUDGET", 0)
    )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
