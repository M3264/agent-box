"""The tools an agent can call, and the guardrails in front of them.

Four tools, chosen to cover what a CLI session actually is: ``run`` a shell command,
``read_file``, ``write_file``, and ``fetch`` a URL. ``run`` goes through ``bash -c``
rather than an argv list, because pipes, redirects and ``&&`` are most of what makes
a shell useful and re-implementing them would be worse than using the one that
exists.

Three layers, and they must not be confused for one another:

1. **The sandbox is the security boundary** (see ``sandbox.py``). It decides what a
   command *can* touch.
2. **Guardrails are advisory pattern-matching.** ``classify()`` recognises commands
   that are outward-facing or hard to reverse and asks a human first. A determined
   model can evade a regex, so this is not containment — it is a human in the loop
   on the operations where being wrong is expensive. The matched reason is shown to
   the operator, so the gate says *why*.
3. **``refuse()`` closes the gap the other two leave.** Only ``run`` actually goes
   through the sandbox; ``read_file``, ``write_file`` and ``fetch`` run in the service
   process, so no mount flag or ``--unshare-net`` applies to them. Without a check,
   the three tools that skip the boundary would be the easy way around it — a job
   running ``sandboxed`` could read the provider key by asking for ``read_file``
   instead of ``cat``. Those calls are **refused, not gated**: an operator approving a
   command-level gate is deciding about that one command, not silently revoking the
   confinement they chose for the job.

So the order in the loop is refuse → classify → execute, and the three answers are
different in kind: *cannot* (the backend forbids it), *ask first* (a human should
see it), *go*.

Within what the backend does allow, out-of-workspace paths are guardrail hits rather
than errors, because in ``unconfined`` mode a human deciding is more useful than a
refusal the agent will just route around with ``run``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.logging_setup import get_logger
from app.orchestrator.sandbox import SANDBOXED, Completed, SandboxError, _Sandbox, is_masked

log = get_logger("agent_hub.tools")

TOOL_NAMES = ("run", "read_file", "write_file", "fetch", "ask_operator")

#: Where full command output is kept, relative to the workspace. Inside the
#: workspace on purpose: a 40MB build log the agent can grep is more useful than one
#: only the operator can see, and the truncated slice on the row stays authoritative
#: for the UI either way.
LOG_DIR = ".agent-hub"

#: How many redirects ``fetch`` follows, re-validating the target at every hop.
MAX_REDIRECTS = 3


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": (
                "Run a shell command in the job workspace and get back its exit code, "
                "stdout and stderr. Runs through bash, so pipes, redirects, && and "
                "environment assignments all work. Not interactive: a command that "
                "waits for input will hang until it times out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command, e.g. 'pytest -q 2>&1 | tail -40'.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds to allow. Defaults to the server limit.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file. Relative paths resolve against the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to read."},
                    "start_line": {"type": "integer", "description": "1-based first line to return."},
                    "max_lines": {"type": "integer", "description": "How many lines to return."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a text file, creating parent directories. Overwrites. Use this "
                "for real deliverables instead of pasting file contents into your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to write."},
                    "content": {"type": "string", "description": "Full new contents."},
                    "append": {"type": "boolean", "description": "Append instead of overwriting."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": (
                "Fetch a URL over HTTP(S) and return the status and body. For anything "
                "beyond a simple GET, or for authenticated requests, use curl via run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL."},
                    "method": {"type": "string", "description": "GET (default), POST, PUT, PATCH, DELETE, HEAD."},
                    "body": {"type": "string", "description": "Request body, for methods that take one."},
                    "headers": {"type": "object", "description": "Extra request headers."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_operator",
            # Written at the model rather than at a reader, because the failure mode
            # this tool exists to prevent is a model that would rather guess than
            # look indecisive — and the opposite failure, a model that asks instead
            # of reading a file, is just as bad. Both halves are spelled out.
            "description": (
                "Ask the operator a question and wait for their answer. Use this when "
                "you are blocked on something only they can decide: which of two "
                "acceptable directions they want, a target or credential that is not "
                "in the workspace, or which reading of an ambiguous requirement was "
                "meant. Prefer offering concrete options over an open question. Do NOT "
                "use it for anything you could answer yourself by reading a file or "
                "running a command, and do not use it to ask permission — risky actions "
                "are gated automatically. The job pauses while this waits, so ask once, "
                "with everything you need, rather than in instalments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question, in one or two plain sentences.",
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "Why you are asking and what turns on the answer. The "
                            "operator may have no context on what you are doing."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "The choices you would accept, most likely first. Two to "
                            "four is usually right."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "The choice, short enough to read on a button.",
                                },
                                "detail": {
                                    "type": "string",
                                    "description": "What picking this one means.",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "allow_free_text": {
                        "type": "boolean",
                        "description": (
                            "Whether an answer outside your options is acceptable. "
                            "Defaults to true."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
]


@dataclass(slots=True)
class ToolInvocation:
    """One requested tool call, with the identity it will be recorded under."""

    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    turn: int = 0

    @property
    def display(self) -> str:
        """A one-line rendering for the timeline, the gate, and the Commands view."""
        if self.tool == "run":
            return str(self.args.get("command") or "").strip() or "(empty command)"
        if self.tool in {"read_file", "write_file"}:
            verb = "read" if self.tool == "read_file" else "write"
            return f"{verb} {self.args.get('path') or '(no path)'}"
        if self.tool == "fetch":
            method = str(self.args.get("method") or "GET").upper()
            return f"{method} {self.args.get('url') or '(no url)'}"
        if self.tool == "ask_operator":
            return str(self.args.get("question") or "").strip() or "(empty question)"
        return f"{self.tool} {json.dumps(self.args)[:200]}"


@dataclass(slots=True)
class ToolOutcome:
    """What happened, in both machine and model-readable form."""

    #: ok | error | denied | timeout | interrupted | capped
    status: str
    #: The text handed back to the model as the tool result.
    content: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class Risk:
    level: str  # high | medium
    reason: str


# ------------------------------------------------------------- what is not allowed


def refuse(invocation: ToolInvocation, *, workspace: Path, sandbox_kind: str) -> str | None:
    """What the chosen backend forbids. Returns the reason, or None to continue.

    Checked *before* ``classify``, because these are not decisions an operator should
    be asked to make. Three of the four tools run in the service process rather than
    the sandbox, so for them confinement is a promise this function keeps or nobody
    does:

    - a masked path (the provider key, the data directory, ``~/.ssh``) is invisible to
      a sandboxed command, so ``read_file`` must not be the way to see it;
    - the host filesystem is read-only under bwrap, so an out-of-workspace
      ``write_file`` cannot be honoured there — offering it as a gate would mean
      approving a write the picker said was impossible;
    - ``fetch`` is the only tool whose network access the service can actually
      withhold, so when commands have no network it must not have one either.

    The network rule applies in both backends deliberately. ``unconfined`` cannot take
    the network away from ``run`` at all, so the setting is weaker there — but a
    ``fetch`` that ignores it would be the service itself contradicting the operator,
    which is a different thing from a limitation of the backend.

    The returned string is read by the model, so it says what happened and why rather
    than just "no": a tool that refuses without explaining gets retried.
    """
    if invocation.tool == "fetch" and not settings.tool_network:
        return (
            "Refused: network access is turned off for this deployment "
            "(AGENT_HUB_TOOL_NETWORK=0). fetch runs outside the sandbox, so it is "
            "refused rather than left as a way around that setting. Nothing was "
            "requested. Work from what is already in the workspace."
        )

    if sandbox_kind != SANDBOXED or invocation.tool not in {"read_file", "write_file"}:
        return None

    target = _resolve(str(invocation.args.get("path") or ""), workspace)
    verb = "read" if invocation.tool == "read_file" else "write"
    if is_masked(target, workspace=workspace):
        return (
            f"Refused: {target} is masked in the sandboxed backend — a command cannot "
            f"see it, so this tool will not {verb} it either. It holds credentials or "
            "service state, not anything this job needs."
        )
    if invocation.tool == "write_file" and not _within(target, workspace):
        return (
            f"Refused: {target} is outside the job workspace, and this job is "
            "sandboxed — the host filesystem is read-only, so the write cannot "
            f"succeed. Write under {workspace} instead."
        )
    return None


# --------------------------------------------------------------------- guardrails

#: Ordered most-specific first; the first match wins so the reason shown is the
#: sharpest one that applies.
_COMMAND_GUARDRAILS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]{2}[a-zA-Z]*\s+/(\s|$)"), "high",
     "recursive delete of the filesystem root"),
    (re.compile(r"\brm\s+.*-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+.*-[a-zA-Z]*f[a-zA-Z]*r"), "high",
     "recursive force delete"),
    (re.compile(r"\b(sudo|doas|pkexec)\b|\bsu\s+-"), "high", "privilege escalation"),
    (re.compile(r"\bgit\s+push\b"), "high", "pushes to a remote repository"),
    (re.compile(r"\bgh\s+(pr|release|repo|issue|api)\b"), "high", "acts on GitHub"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*[dfx]|checkout\s+--\s)"), "high",
     "discards uncommitted work irreversibly"),
    (re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba)?sh\b"), "high",
     "pipes a downloaded script straight into a shell"),
    (re.compile(r"\b(apt|apt-get|dpkg|snap|yum|dnf|pacman|brew)\b\s+\S*\b(install|remove|purge|upgrade)\b"),
     "high", "installs or removes system packages"),
    (re.compile(r"\bpip3?\s+(install|uninstall)\b|\bnpm\s+(install|i)\b[^|;]*\s-g\b|\bnpm\s+publish\b"),
     "medium", "installs packages outside the workspace"),
    (re.compile(r"\b(systemctl|service|initctl)\b|\b(reboot|shutdown|halt|poweroff)\b"), "high",
     "controls system services or power state"),
    (re.compile(r"\b(mkfs|fdisk|parted|dd)\b|\b(mount|umount)\b"), "high", "touches devices or filesystems"),
    (re.compile(r"\b(chown|chgrp)\b|\bchmod\s+(-[a-zA-Z]+\s+)*[0-7]*777\b|\bchmod\s+.*\+s\b"), "medium",
     "changes ownership or grants wide permissions"),
    (re.compile(r"\b(ssh|scp|sftp|rsync)\b\s+[^|;]*@|\brsync\b[^|;]*::"), "high", "reaches another host"),
    (re.compile(r"\b(docker|podman|kubectl|helm|terraform|aws|gcloud|az)\b"), "high",
     "drives external infrastructure"),
    (re.compile(r"\bcrontab\b|\bat\s+now\b|systemd-run"), "high", "schedules work outside this job"),
    (re.compile(r"\b(pkill|killall)\b|\bkill\s+-9\b"), "medium", "kills processes it did not start"),
    (re.compile(r"\.ssh/|\.codex/config\.toml|\.aws/credentials|\.git-credentials|"
                r"/etc/agent-hub\.env|\.config/gh|id_rsa|id_ed25519|\bnetrc\b"),
     "high", "reads credentials"),
    (re.compile(r"\bhistory\b\s*-c|\bshred\b|\btruncate\b\s+-s\s*0"), "medium", "destroys evidence or data"),
)


def classify(invocation: ToolInvocation, *, workspace: Path) -> Risk | None:
    """Does this call need a human first? Returns the matched risk, or None.

    Pattern-matching a shell string is inherently approximate — ``$(echo c)url`` gets
    through, and a long pipeline can bury a match. It is the right amount of
    machinery anyway, because its job is to catch the *ordinary* dangerous command
    (the one a well-behaved model reaches for and an operator wants to see), not to
    contain an adversarial one. Containment is the sandbox's job.
    """
    if invocation.tool == "run":
        command = str(invocation.args.get("command") or "")
        for pattern, level, reason in _COMMAND_GUARDRAILS:
            if pattern.search(command):
                return Risk(level=level, reason=reason)
        return None

    if invocation.tool in {"read_file", "write_file"}:
        raw = str(invocation.args.get("path") or "")
        target = _resolve(raw, workspace)
        if not _within(target, workspace):
            verb = "read" if invocation.tool == "read_file" else "write"
            return Risk(
                level="high" if invocation.tool == "write_file" else "medium",
                reason=f"would {verb} {target}, outside the job workspace",
            )
        return None

    if invocation.tool == "fetch":
        method = str(invocation.args.get("method") or "GET").upper()
        if method not in {"GET", "HEAD"}:
            return Risk(level="high", reason=f"{method} request changes state on a remote service")
        return None

    return None


# ----------------------------------------------------------------------- helpers


def _resolve(raw: str, workspace: Path) -> Path:
    """Resolve a tool-supplied path. Relative paths are workspace-relative."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    # No strict=True: the path may not exist yet, and `..` still has to collapse.
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _within(path: Path, workspace: Path) -> bool:
    try:
        return path.is_relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n… [{len(text) - limit} more characters omitted]", True


# ----------------------------------------------------------------------- dispatch


async def execute(
    invocation: ToolInvocation,
    *,
    sandbox: _Sandbox,
    workspace: Path,
) -> ToolOutcome:
    """Run one tool call. Never raises for a *tool* failure — that is a result.

    A failed command is information the model should read and act on, so anything
    the tool itself can go wrong about comes back as ``status='error'`` with the
    reason in ``content``. Only cancellation propagates, because a stopped job must
    not look like a tool that returned.
    """
    if "__unparsed__" in invocation.args:
        return ToolOutcome(
            status="error",
            content=(
                "Your tool arguments were not valid JSON, so nothing ran. Send the "
                f"arguments again as a JSON object. Received: {invocation.args['__unparsed__'][:400]}"
            ),
        )

    try:
        if invocation.tool == "run":
            return await _run(invocation, sandbox=sandbox, workspace=workspace)
        if invocation.tool == "read_file":
            return await asyncio.to_thread(_read_file, invocation, workspace)
        if invocation.tool == "write_file":
            return await asyncio.to_thread(_write_file, invocation, workspace)
        if invocation.tool == "fetch":
            return await _fetch(invocation)
        if invocation.tool == "ask_operator":
            # Handled by the loop, which is the only layer holding the database, the
            # event store and the cancel event a blocking question needs. Named
            # explicitly so a wiring mistake reports itself instead of producing
            # "Unknown tool 'ask_operator'. Available: … ask_operator".
            return ToolOutcome(
                status="error",
                content="ask_operator is dispatched by the agent loop, not by execute().",
            )
    except asyncio.CancelledError:
        raise
    except SandboxError as exc:
        return ToolOutcome(status="error", content=f"The command could not be started: {exc}")
    except Exception as exc:  # noqa: BLE001 - a tool fault must not end the job
        log.warning("tool raised", extra={"tool": invocation.tool, "error": str(exc)}, exc_info=True)
        return ToolOutcome(status="error", content=f"{type(exc).__name__}: {exc}")

    return ToolOutcome(
        status="error",
        content=f"Unknown tool '{invocation.tool}'. Available: {', '.join(TOOL_NAMES)}.",
    )


async def _run(invocation: ToolInvocation, *, sandbox: _Sandbox, workspace: Path) -> ToolOutcome:
    command = str(invocation.args.get("command") or "").strip()
    if not command:
        return ToolOutcome(status="error", content="No command was supplied.")

    timeout = float(invocation.args.get("timeout") or settings.tool_timeout)
    timeout = max(1.0, min(timeout, float(settings.tool_timeout)))

    log_dir = workspace / LOG_DIR
    stdout_path = log_dir / f"tool-{invocation.id}.out"
    stderr_path = log_dir / f"tool-{invocation.id}.err"

    result = await sandbox.run(
        ["/bin/bash", "-c", command],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout=timeout,
        output_limit=settings.tool_output_limit,
        output_max_bytes=settings.tool_output_max,
    )

    status = "ok"
    if result.timed_out:
        status = "timeout"
    elif result.output_capped:
        status = "capped"
    elif result.interrupted:
        # Not an error, because an error is a result. Something outside this command
        # killed it — nearly always a service restart, which SIGTERMs the whole cgroup
        # — so whether it did its work is unknown, and `interrupted` is the only status
        # that says so. The loop stops here rather than carrying the phase on; see
        # agentloop._invoke.
        status = "interrupted"
    elif result.exit_code != 0:
        status = "error"

    return ToolOutcome(
        status=status,
        content=_render_run(command, result, status, log_dir),
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
    )


def _render_run(command: str, result: Completed, status: str, log_dir: Path) -> str:
    """The tool result as the model reads it."""
    header = f"$ {command}"
    if status == "timeout":
        header += f"\n[killed after the {result.duration_ms / 1000:.0f}s timeout]"
    elif status == "capped":
        header += "\n[killed for producing too much output]"
    elif status == "interrupted":
        # Says "unknown", not "failed". This text is what the next attempt at the phase
        # is shown, so it has to be the thing that stops a model from either assuming
        # the work is done or blindly repeating it.
        header += (
            f"\n[terminated by {result.signal_name} after {result.duration_ms}ms, from "
            "outside the command — most likely a service restart. Whether it finished "
            "its work is unknown: check the workspace before repeating it]"
        )
    else:
        header += f"\nexit={result.exit_code} ({result.duration_ms}ms)"

    parts = [header]
    if result.stdout:
        parts.append(f"--- stdout ---\n{result.stdout}")
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    if not result.stdout and not result.stderr:
        parts.append("(no output)")
    if result.truncated:
        parts.append(f"[output was truncated; the full log is under {log_dir}]")
    return "\n".join(parts)


def _read_file(invocation: ToolInvocation, workspace: Path) -> ToolOutcome:
    started = time.monotonic()
    path = _resolve(str(invocation.args.get("path") or ""), workspace)
    if not path.exists():
        return ToolOutcome(status="error", content=f"{path} does not exist.")
    if path.is_dir():
        entries = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir())
        listing, truncated = _clip("\n".join(entries), settings.tool_output_limit)
        return ToolOutcome(
            status="ok",
            content=f"{path} is a directory containing:\n{listing}",
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        raw = path.read_bytes()[: settings.tool_file_limit + 1]
    except OSError as exc:
        return ToolOutcome(status="error", content=f"Could not read {path}: {exc}")

    over = len(raw) > settings.tool_file_limit
    text = raw[: settings.tool_file_limit].decode("utf-8", errors="replace")

    start_line = max(1, int(invocation.args.get("start_line") or 1))
    max_lines = invocation.args.get("max_lines")
    if start_line > 1 or max_lines:
        lines = text.splitlines()
        end = start_line - 1 + int(max_lines) if max_lines else len(lines)
        text = "\n".join(lines[start_line - 1 : end])

    body, clipped = _clip(text, settings.tool_output_limit)
    return ToolOutcome(
        status="ok",
        content=f"{path}\n{body}" + ("\n[file is larger than the read limit]" if over else ""),
        stdout=body,
        truncated=clipped or over,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _write_file(invocation: ToolInvocation, workspace: Path) -> ToolOutcome:
    started = time.monotonic()
    path = _resolve(str(invocation.args.get("path") or ""), workspace)
    content = str(invocation.args.get("content") or "")
    if len(content.encode()) > settings.tool_file_limit:
        return ToolOutcome(
            status="error",
            content=(
                f"Content is larger than the {settings.tool_file_limit} byte write limit. "
                "Write it in pieces with append, or generate it with a command."
            ),
        )

    append = bool(invocation.args.get("append"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        return ToolOutcome(status="error", content=f"Could not write {path}: {exc}")

    verb = "Appended" if append else "Wrote"
    return ToolOutcome(
        status="ok",
        content=f"{verb} {len(content.encode())} bytes to {path}.",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# -------------------------------------------------------------------------- fetch


def _host_allowed(host: str) -> tuple[bool, str]:
    """Is this host reachable by ``fetch``?

    Two checks. The allowlist, when the operator set one. And, always, a resolved-IP
    check against loopback, link-local and private ranges — ``fetch`` runs in the
    service process rather than the sandbox, so without it the tool would be a way
    around the boundary: the hub's own unauthenticated API on 127.0.0.1:8090 and
    cloud metadata on 169.254.169.254 are both one GET away. An explicitly
    allowlisted host overrides this, because naming it is a deliberate choice.
    """
    allow = settings.fetch_allow_hosts
    if allow:
        if not any(host == entry or host.endswith("." + entry.lstrip(".")) for entry in allow):
            return False, f"'{host}' is not in the configured fetch allowlist"
        return True, ""

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return False, f"could not resolve '{host}': {exc}"

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False, (
                f"'{host}' resolves to the internal address {address}; fetch is restricted to "
                "public hosts. Use run with curl if you genuinely need a local endpoint."
            )
    return True, ""


async def _fetch(invocation: ToolInvocation) -> ToolOutcome:
    started = time.monotonic()
    url = str(invocation.args.get("url") or "").strip()
    method = str(invocation.args.get("method") or "GET").upper()
    headers = invocation.args.get("headers")
    headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
    # No provider credentials, no service environment: this client shares nothing
    # with the one that talks to the model.
    headers.setdefault("User-Agent", "agent-hub/2.0")

    body = invocation.args.get("body")
    limit = settings.tool_output_limit

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.fetch_timeout),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ToolOutcome(
                    status="error",
                    content=f"'{url}' is not an absolute http(s) URL.",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            # Re-checked at every hop: a public URL that 302s to 169.254.169.254 is
            # the standard way around a one-time check.
            allowed, reason = await asyncio.to_thread(_host_allowed, parsed.hostname)
            if not allowed:
                return ToolOutcome(
                    status="error",
                    content=f"Refused: {reason}.",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            try:
                response = await client.request(
                    method, url, headers=headers, content=body if body else None
                )
            except httpx.HTTPError as exc:
                return ToolOutcome(
                    status="error",
                    content=f"{method} {url} failed: {exc}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            location = response.headers.get("location")
            if response.is_redirect and location and hop < MAX_REDIRECTS:
                url = str(response.url.join(location))
                continue
            break

    text, truncated = _clip(response.text, limit)
    return ToolOutcome(
        status="ok" if response.status_code < 400 else "error",
        content=(
            f"{method} {url}\nHTTP {response.status_code} "
            f"({response.headers.get('content-type', 'unknown type')})\n{text}"
        ),
        exit_code=response.status_code,
        stdout=text,
        truncated=truncated,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
