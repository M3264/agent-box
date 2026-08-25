"""Command execution backends.

Giving the agents a shell turns "what can a command reach?" into a product
question, so it is a setting rather than an implementation detail. Two backends:

- ``unconfined`` — a plain subprocess. Everything the service user can do, which
  on this host includes passwordless sudo and the provider key in
  ``~/.codex/config.toml``.
- ``sandboxed`` — bubblewrap. The host filesystem is read-only, the job's own
  workspace is the only writable path, credential directories are masked, and the
  user namespace means setuid binaries (``sudo``) cannot escalate.

Two rules hold in both:

**No silent downgrade.** A backend that cannot run is *unavailable*, never quietly
replaced by a weaker one. Running unconfined because bwrap was missing, while the
UI still said "sandboxed", is worse than refusing to run at all — so
``build_sandbox`` raises instead.

**The environment is built by allowlist, not scrubbed by denylist.** A command
gets a fixed PATH and nothing else it was not explicitly given, so no
``AGENT_HUB_*`` variable and no resolved provider secret can leak into a
subprocess by being forgotten. In ``unconfined`` mode a command can still *read*
the key file; that is the documented consequence of choosing it, and the reason
``sandboxed`` is the default.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.logging_setup import get_logger

log = get_logger("agent_hub.sandbox")

SANDBOXED = "sandboxed"
UNCONFINED = "unconfined"
#: Ordered for display: the safe one first, so it reads as the default it is.
SANDBOX_KINDS: tuple[str, ...] = (SANDBOXED, UNCONFINED)

#: Fixed rather than inherited, so a command's PATH does not depend on how the
#: service happened to be started.
BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
#: The only variables copied from the service environment, and only if set.
ENV_PASSTHROUGH = ("LANG", "LC_ALL", "TZ")

#: Poll interval for the output watchdog.
_WATCH_INTERVAL = 0.5

#: Signals that mean something *outside* the command asked its process tree to stop:
#: a `systemctl restart` (systemd SIGTERMs the whole cgroup, so the child dies before
#: this process is told to shut down), an operator's `pkill`, a console Ctrl-C.
#:
#: Deliberately not SIGSEGV, SIGABRT, SIGFPE or SIGILL — a fault is a *result*, and one
#: the agent should read and act on rather than retry blindly. Deliberately not SIGKILL
#: either: if systemd escalated to KILL, this process died too and the row is swept on
#: the next start, so a SIGKILL observed by a *surviving* service is far more often the
#: OOM killer — and "your command was killed for using too much memory" is also a result.
_TERMINATION_SIGNALS = frozenset(
    {signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM}
)


class SandboxError(RuntimeError):
    """A command could not be run at all."""


class SandboxUnavailable(SandboxError):
    """The requested backend does not work on this host.

    Deliberately fatal. The caller's only correct response is to tell the operator,
    not to pick a different backend.
    """


@dataclass(frozen=True, slots=True)
class Completed:
    """What a finished command produced.

    ``stdout``/``stderr`` are bounded slices; the full streams are always on disk at
    the paths the caller supplied. ``truncated`` says the slice is short, which is
    different from ``output_capped`` — the latter means the command was killed for
    writing more than the hard ceiling.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    output_capped: bool = False
    truncated: bool = False
    #: The signal that killed the command, when it was not this process that asked for
    #: it. ``None`` on a normal exit, and on our own timeout and output-cap kills —
    #: those are already described exactly by the flags above.
    signalled: int | None = None

    @property
    def interrupted(self) -> bool:
        """Was this command stopped from outside before it could finish?

        The distinction from a non-zero exit is not cosmetic. A command that exits
        non-zero produced a *result*: the agent can read the error and decide what to
        do. A command that was terminated produced *no information at all* — its side
        effects may be complete, partial or absent, and nothing in the exit status says
        which. Reporting the second as the first hands a model a fact nobody
        established, which is how a phase ends up completing on the strength of a
        `git push` that may or may not have happened.
        """
        return self.signalled in _TERMINATION_SIGNALS

    @property
    def signal_name(self) -> str:
        """``SIGTERM`` rather than ``-15``, for a line a human or a model will read."""
        if self.signalled is None:
            return ""
        with contextlib.suppress(ValueError):
            return signal.Signals(self.signalled).name
        return f"signal {self.signalled}"


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    id: str
    label: str
    available: bool
    reason: str


def _masked_paths() -> tuple[list[Path], list[Path]]:
    """Directories and files a sandboxed command must not see.

    Everything here is either a credential or the service's own state. The
    database is included because an agent that can rewrite ``jobs`` or read other
    jobs' rows is not sandboxed in any useful sense.
    """
    home = Path.home()
    dirs = [
        home / ".ssh",
        home / ".codex",
        home / ".aws",
        home / ".gnupg",
        home / ".docker",
        home / ".config",
        home / ".claude",
        home / ".kube",
        Path("/root"),
    ]

    # The service's data directory as a whole, not just agent-hub.db. Enumerating
    # suffixes is a game you lose quietly: `data/agent-hub.db.bak-*` is a full copy of
    # the same job data, and a `-wal` that appears after this list is built would not
    # be covered by a per-file mask either. Masking the directory is categorical, and
    # costs nothing because this job's workspace is bound back afterwards.
    #
    # Skipped when AGENT_HUB_DB points somewhere broad (the repo root, home, `/`),
    # since hiding the tree the agent is meant to work in would be a far bigger
    # surprise than a narrow mask. The per-file entries below still cover the database
    # itself in that case.
    data_dir = settings.db_path.parent
    if data_dir != settings.root and data_dir not in settings.root.parents:
        dirs.append(data_dir)

    files = [
        home / ".netrc",
        home / ".git-credentials",
        Path("/etc/agent-hub.env"),
        settings.db_path,
        settings.db_path.with_name(settings.db_path.name + "-wal"),
        settings.db_path.with_name(settings.db_path.name + "-shm"),
        # Wherever the provider key actually is, not only the default under ~/.codex —
        # AGENT_HUB_CODEX_CONFIG can point anywhere, and a mask list that covers the
        # default location protects the default host and nobody else.
        settings.codex_config,
    ]

    # A file mask *inside* a masked directory is worse than redundant. `--tmpfs <dir>`
    # hides the whole listing, but a later `--ro-bind-try /dev/null <dir>/f` makes bwrap
    # create the mount point, so `f` comes back in `ls` as an empty file. That is exactly
    # how agent-hub.db, -wal and -shm stayed visible after the directory mask was added,
    # while agent-hub.db.bak-* — named nowhere — was correctly gone: the three names
    # someone thought of were the three that reappeared. Absent beats present-but-empty.
    #
    # Dropped here rather than deleted from the list above, because the entries still
    # earn their place: the day AGENT_HUB_DB or AGENT_HUB_CODEX_CONFIG points outside a
    # masked directory is the day the per-file mask is the only one covering it. The
    # filter decides which apply, not whoever edits the list.
    return dirs, [path for path in files if not any(_under(path, entry) for entry in dirs)]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _under(path: Path, directory: Path) -> bool:
    """Is ``path`` inside ``directory``? False rather than raising on odd input."""
    with contextlib.suppress(OSError, ValueError):
        return _resolved(path).is_relative_to(_resolved(directory))
    return False


def is_masked(path: Path, *, workspace: Path | None = None) -> bool:
    """Would a sandboxed command find this path masked?

    Exists for the tools that do *not* go through a sandbox. ``read_file``,
    ``write_file`` and ``fetch`` run in the service process, so no mount flag applies
    to them, and a tool that can read what a command cannot is a hole in the boundary
    rather than a convenience. Asking here keeps one definition of "masked": callers
    cannot drift from the list, and adding to the list closes the hole everywhere at
    once.

    ``workspace`` is the job's own directory, which ``wrap()`` binds back writable
    *after* the masks are applied — so a path inside it is reachable even though the
    data directory containing it is not. Passing it is what makes this answer match
    what a command would actually see.
    """
    resolved = _resolved(path)
    if workspace is not None and _under(resolved, workspace):
        return False

    dirs, files = _masked_paths()
    if any(_under(resolved, directory) for directory in dirs):
        return True
    return any(resolved == _resolved(file) for file in files)


class _Sandbox:
    """Shared spawn machinery. Subclasses decide the argv wrapping and the env."""

    kind: str = UNCONFINED

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    # -- subclass surface ----------------------------------------------------

    def wrap(self, argv: list[str]) -> list[str]:
        return argv

    def env(self) -> dict[str, str]:
        environ = {
            "PATH": BASE_PATH,
            # HOME points at the workspace so per-tool caches (pip, npm, git) land
            # inside the job rather than in the service user's home, and so a
            # command that expands ``~`` gets the workspace either way.
            "HOME": str(self.workspace),
            "TMPDIR": "/tmp",
            # Stops CLIs emitting cursor control sequences into the captured log.
            "TERM": "dumb",
            "PWD": str(self.workspace),
            # Widely honoured "don't be interactive" hints. A command that blocks
            # on a prompt would otherwise burn its whole timeout in silence.
            "DEBIAN_FRONTEND": "noninteractive",
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "NO_COLOR": "1",
        }
        for name in ENV_PASSTHROUGH:
            value = os.environ.get(name)
            if value:
                environ[name] = value
        return environ

    # -- the shared implementation -------------------------------------------

    async def run(
        self,
        argv: list[str],
        *,
        stdout_path: Path,
        stderr_path: Path,
        timeout: float,
        output_limit: int,
        output_max_bytes: int,
        stdin: bytes | None = None,
    ) -> Completed:
        """Run a command, capturing to files rather than pipes.

        Output goes straight to disk for two reasons: ``communicate()`` would hold
        an unbounded amount of a runaway command's output in memory, and a
        watchdog on file size can kill such a command before it fills the disk.
        The caller gets a bounded slice back and keeps the full log.
        """
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False
        capped = False

        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.wrap(argv),
                    stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    cwd=str(self.workspace),
                    env=self.env(),
                    # Its own process group, so a timeout can kill the whole tree
                    # rather than just the shell that spawned it.
                    start_new_session=True,
                )
            except OSError as exc:
                raise SandboxError(f"could not start command: {exc}") from exc

            if stdin:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    assert process.stdin is not None
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                    process.stdin.close()

            watchdog = asyncio.ensure_future(
                self._watch_output(process, stdout_path, stderr_path, output_max_bytes)
            )
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                await self._terminate(process)
            except asyncio.CancelledError:
                # An operator stop, or shutdown. Kill the tree before unwinding:
                # leaving a detached `npm install` running past the job that
                # started it is exactly the orphan this avoids.
                await self._terminate(process)
                raise
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
                capped = watchdog.done() and not watchdog.cancelled() and bool(watchdog.result())

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, out_truncated = _tail(stdout_path, output_limit)
        stderr, err_truncated = _tail(stderr_path, output_limit)

        # A negative return code means a signal killed it. When *we* did the killing the
        # flags above already say so precisely, and calling it a signal death too would
        # turn "killed after the 60s timeout" into "interrupted, nobody knows". Anything
        # else came from outside this process — see _TERMINATION_SIGNALS.
        #
        # Reaching here at all means `process.wait()` returned: an operator stop unwinds
        # through the CancelledError branch above and never builds a Completed, so a stop
        # can never be mistaken for an interruption.
        returncode = process.returncode if process.returncode is not None else -1
        signalled = None
        if not timed_out and not capped and returncode < 0:
            signalled = -returncode

        return Completed(
            exit_code=-1 if timed_out else returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_capped=capped,
            truncated=out_truncated or err_truncated,
            signalled=signalled,
        )

    @staticmethod
    async def _watch_output(
        process: asyncio.subprocess.Process,
        stdout_path: Path,
        stderr_path: Path,
        limit: int,
    ) -> bool:
        """Kill a command that writes more than ``limit`` bytes. Returns True if it did."""
        while True:
            await asyncio.sleep(_WATCH_INTERVAL)
            total = 0
            for path in (stdout_path, stderr_path):
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
            if total > limit:
                log.warning(
                    "command exceeded its output ceiling; killing it",
                    extra={"bytes": total, "limit": limit},
                )
                await _Sandbox._terminate(process)
                return True

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """SIGTERM the group, then SIGKILL what survives."""
        if process.returncode is not None:
            return
        for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(process.pid), sig)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), grace)
                return
        with contextlib.suppress(Exception):
            await process.wait()


def _tail(path: Path, limit: int) -> tuple[str, bool]:
    """The last ``limit`` bytes of a log, and whether anything was dropped.

    The *tail* rather than the head: a failing command's useful line is its last
    one, and a head slice of a verbose build log is all banner and no error.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "", False
    if size == 0:
        return "", False
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        data = handle.read(limit)
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        return f"… [{size - limit} earlier bytes omitted; full log in the workspace]\n{text}", True
    return text, False


class DirectSandbox(_Sandbox):
    """No confinement. The command runs as the service user."""

    kind = UNCONFINED


class BwrapSandbox(_Sandbox):
    """bubblewrap confinement: read-only host, writable workspace, masked secrets."""

    kind = SANDBOXED

    def wrap(self, argv: list[str]) -> list[str]:
        workspace = str(self.workspace)
        wrapped = [
            "bwrap",
            "--ro-bind", "/", "/",
            # /tmp and /dev/shm are per-command, so nothing survives between calls
            # in a place the agent might mistake for the workspace.
            "--tmpfs", "/tmp",
            "--tmpfs", "/dev/shm",
            "--dev", "/dev",
            "--proc", "/proc",
        ]
        # Mask every job's workspace, then bind this one back writable. That both
        # grants write access and stops one job reading another's files.
        wrapped += ["--tmpfs", str(settings.workspace_root)]

        masked_dirs, masked_files = _masked_paths()
        # Only what actually exists. bwrap creates a missing mount point, and every
        # parent here sits under the read-only bind of `/`, so masking a path that is
        # not there fails the *whole command* with "Can't mkdir …: Read-only file
        # system" — which is how a stray `~/.aws` in this list made every sandboxed
        # command impossible. Nothing is lost by skipping: a path that does not exist
        # has nothing to leak, and a command cannot create it inside a read-only bind
        # either. Checked per command, so a directory created later is still masked on
        # the next one.
        for directory in masked_dirs:
            if directory.is_dir():
                wrapped += ["--tmpfs", str(directory)]
        for file in masked_files:
            # A file cannot be tmpfs'd; shadow it with an empty read-only file. The
            # `-try` suffix only forgives a missing *source*, hence the check here.
            if file.exists():
                wrapped += ["--ro-bind-try", "/dev/null", str(file)]

        wrapped += ["--bind", workspace, workspace]
        wrapped += [
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-cgroup-try",
            # Without a new session a process inside can push characters back onto
            # the controlling terminal (TIOCSTI).
            "--new-session",
            # If the service dies, so does the command — no orphans outliving the
            # engine that started them.
            "--die-with-parent",
            "--chdir", workspace,
        ]
        if not settings.tool_network:
            wrapped += ["--unshare-net"]
        wrapped += ["--"]
        return wrapped + argv


# ------------------------------------------------------------------ availability

_PROBE_TIMEOUT = 10.0
_status: dict[str, SandboxStatus] | None = None


async def probe(*, force: bool = False) -> dict[str, SandboxStatus]:
    """Work out which backends actually run here, once per process.

    ``bwrap`` being installed is not enough: this host sets
    ``kernel.apparmor_restrict_unprivileged_userns=1``, which denies unprivileged
    user namespaces unless bwrap is setuid or has an AppArmor profile granting
    ``userns``. The only reliable test is to try, so that is what this does — and
    the failure text is kept, because "sandboxed is unavailable" is not actionable
    without the reason.
    """
    global _status
    if _status is not None and not force:
        return _status

    statuses = {
        UNCONFINED: SandboxStatus(
            id=UNCONFINED,
            label="Unconfined",
            available=True,
            reason="Runs as the service user, with access to its files and sudo.",
        )
    }
    statuses[SANDBOXED] = await _probe_bwrap()
    _status = statuses
    log.info(
        "sandbox probe complete",
        extra={kind: status.available for kind, status in statuses.items()},
    )
    return statuses


async def _probe_bwrap() -> SandboxStatus:
    def unavailable(reason: str) -> SandboxStatus:
        return SandboxStatus(id=SANDBOXED, label="Sandboxed", available=False, reason=reason)

    if shutil.which("bwrap") is None:
        return unavailable("bubblewrap is not installed (apt install bubblewrap)")

    probe_dir = settings.workspace_root / ".probe"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return unavailable(f"could not create a probe directory: {exc}")

    sandbox = BwrapSandbox(probe_dir)
    try:
        result = await sandbox.run(
            ["/bin/true"],
            stdout_path=probe_dir / "probe.out",
            stderr_path=probe_dir / "probe.err",
            timeout=_PROBE_TIMEOUT,
            output_limit=4096,
            output_max_bytes=1 << 20,
        )
    except SandboxError as exc:
        return unavailable(str(exc))
    except Exception as exc:  # noqa: BLE001 - a probe must never break startup
        return unavailable(f"probe failed: {exc}")

    if result.exit_code != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"bwrap exited {result.exit_code}"
        if "namespace" in reason or "permission" in reason.lower():
            reason += " — the kernel denies unprivileged user namespaces on this host"
        return unavailable(reason)

    return SandboxStatus(
        id=SANDBOXED,
        label="Sandboxed",
        available=True,
        reason="Read-only host, writable workspace only, credentials masked, no sudo.",
    )


def status() -> dict[str, SandboxStatus]:
    """The cached probe result. Empty before startup has run ``probe()``."""
    return dict(_status) if _status else {}


def default_kind() -> str:
    """The configured default, downgraded to a *label* only — never silently used.

    If the configured default is unavailable, jobs that do not name a backend fail
    with a clear message rather than running somewhere the operator did not choose.
    """
    return settings.sandbox_default if settings.sandbox_default in SANDBOX_KINDS else SANDBOXED


def build_sandbox(kind: str | None, workspace: Path) -> _Sandbox:
    """Build a backend, or refuse. Never substitutes a different one."""
    resolved = kind or default_kind()
    if resolved not in SANDBOX_KINDS:
        raise SandboxUnavailable(f"unknown sandbox '{resolved}'")

    available = _status.get(resolved) if _status else None
    if available is not None and not available.available:
        raise SandboxUnavailable(
            f"the '{resolved}' sandbox is not available on this host: {available.reason}"
        )

    if resolved == SANDBOXED:
        return BwrapSandbox(workspace)
    return DirectSandbox(workspace)
