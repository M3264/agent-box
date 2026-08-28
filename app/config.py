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

    # -- surviving a provider outage ----------------------------------------
    # A provider call already retries a few-second hiccup on its own (see
    # MAX_ATTEMPTS in providers.py). This is the ring outside that: when a call
    # still fails with a *transient* error — a 5xx, a rate limit, a dropped
    # connection, a CDN/WAF error page, a timeout — the whole call is waited on
    # and tried again, instead of failing the phase and taking the job down with
    # it. A call is stateless, so re-trying it replays nothing already done.
    # Errors that a wait cannot fix (a bad request, a missing key, an unusable
    # profile) still fail at once. Off restores the old behaviour: one exhausted
    # call ends the job.
    provider_retry_enabled: bool = field(default_factory=lambda: _env_bool("AGENT_HUB_PROVIDER_RETRY", True))
    #: Total attempts per call, including the first, before the phase is allowed
    #: to fail. 0 keeps trying until the provider recovers or the job is stopped.
    provider_retry_attempts: int = field(default_factory=lambda: _env_int("AGENT_HUB_PROVIDER_RETRY_ATTEMPTS", 20))
    #: First wait after a failure, seconds; doubles each attempt up to the cap.
    provider_retry_base_delay: int = field(default_factory=lambda: _env_int("AGENT_HUB_PROVIDER_RETRY_BASE", 10))
    #: Longest wait between attempts, seconds.
    provider_retry_max_delay: int = field(default_factory=lambda: _env_int("AGENT_HUB_PROVIDER_RETRY_MAX_DELAY", 60))

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

    # -- away-from-browser notifications (Web Push) --------------------------
    #: Off makes every push a no-op and skips key generation entirely, so the feature
    #: can be switched off without touching the frontend. On is the default: a job that
    #: parks on a question at 3am with every tab closed is invisible otherwise.
    push_enabled: bool = field(default_factory=lambda: _env_bool("AGENT_HUB_PUSH", True))
    #: The VAPID ``sub`` claim sent to the browser's push service — a contact for
    #: whoever runs this instance, as ``mailto:`` or ``https:``. The default is a
    #: placeholder; some services (Apple's especially) prefer a real address.
    vapid_subject: str = field(
        default_factory=lambda: _env_str("AGENT_HUB_VAPID_SUBJECT", "mailto:agent-hub@localhost")
    )
    #: The VAPID keypair. Left blank, it is generated once and persisted to
    #: ``vapid_file`` (below) at mode 0600. Set both to pin a keypair from the
    #: environment instead — the private key is a secret and, like every other secret
    #: in this system, never lands in the database or an API response.
    vapid_public_key: str = field(
        default_factory=lambda: _env_str("AGENT_HUB_VAPID_PUBLIC_KEY", "")
    )
    vapid_private_key: str = field(
        default_factory=lambda: _env_str("AGENT_HUB_VAPID_PRIVATE_KEY", "")
    )

    @property
    def vapid_file(self) -> Path:
        """Where a generated keypair is persisted, beside the database. A test that
        repoints the DB gets an isolated keyfile for free, so a push test never writes
        into the real ``data/``."""
        return self.db_path.parent / "vapid.json"

    @property
    def provider_secrets_file(self) -> Path:
        """Where keys pasted in the Settings form are stored, beside the database at
        mode 0600. Like ``vapid_file``, a test that repoints the DB gets an isolated
        store for free, so it never writes into the real ``data/``."""
        return self.db_path.parent / "provider_secrets.json"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
