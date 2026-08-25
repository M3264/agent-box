"""Assert that the `sandboxed` label means something, against the real backend.

`sandbox.py` claims four things about a confined command: it cannot read the
service's credentials, it cannot escalate, it can still reach the network, and the
job workspace is the only place it can write. Those claims are mount flags and
namespace options — reading them is not the same as running under them, and a
wrong flag fails silently in the safe-looking direction. So this runs commands.

Run from the repo root as a module, so the ``app`` import resolves. There is no
``python`` on PATH here; the venv interpreter is the one with the dependencies:

    .venv/bin/python -m tools.check_sandbox

Every command here is inert by construction. ``sudo -n /bin/true`` is the one that
looks alarming: it does nothing even if it succeeds, and the point is that it must
*not* succeed — so it is run only inside the sandbox, never on the host.

Exit 0 means every claim held. Exit 1 means one did not, and the line says which.
Exit 2 means the backend is unavailable on this host, so nothing was proved — a
different outcome from "proved false", and the reason is printed.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from app.config import settings
from app.orchestrator.sandbox import (
    SANDBOXED,
    UNCONFINED,
    Completed,
    SandboxError,
    build_sandbox,
    probe,
)

#: Substrings that would mean a real credential reached a command's output or
#: environment. Only ever tested for — never printed, and never the value itself.
SECRET_MARKERS = ("bearer_token", "api_key", "apikey", "sk-", "OPENAI_API_KEY")

TIMEOUT = 15.0
NETWORK_TIMEOUT = 30.0
#: Reached over plain DNS + TLS with no credentials, and stable enough to treat a
#: failure as "no network" rather than "that host is having a bad day".
NETWORK_URL = "https://example.com"


class Check:
    """One claim, its command, and what the outcome has to look like."""

    def __init__(self, workspace: Path, logs: Path) -> None:
        self.workspace = workspace
        self.logs = logs
        self.failures: list[str] = []
        self.n = 0

    async def run(self, sandbox, argv: list[str], *, timeout: float = TIMEOUT) -> Completed:
        self.n += 1
        return await sandbox.run(
            argv,
            stdout_path=self.logs / f"{self.n}.out",
            stderr_path=self.logs / f"{self.n}.err",
            timeout=timeout,
            output_limit=8192,
            output_max_bytes=4 << 20,
        )

    def ok(self, claim: str) -> None:
        print(f"  ok      {claim}")

    def fail(self, claim: str, detail: str) -> None:
        print(f"  FAILED  {claim}: {detail}")
        self.failures.append(f"{claim}: {detail}")


def leaked(text: str) -> str | None:
    """The marker a blob of output leaked, if any. Returns the marker, not the text."""
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


async def check_confinement(check: Check) -> None:
    """The four claims, plus the two that make the first one non-vacuous."""
    sandbox = build_sandbox(SANDBOXED, check.workspace)

    # --- it cannot read the provider key ------------------------------------
    #
    # Assert on the bytes, not the exit status. Which of the two masks catches this
    # file decides whether `cat` succeeds or fails: a directory `--tmpfs` makes it
    # *absent* (ENOENT, exit 1), while a `--ro-bind-try /dev/null` over the file
    # makes `cat` *succeed* and print nothing. Both are fine and the mask list is
    # free to change which one applies, so the claim is about key material coming
    # back — the one thing that must be true either way.
    key = settings.codex_config
    host_text = key.read_text(errors="replace") if key.exists() else ""
    host_marker = leaked(host_text)
    claim = f"cannot read the provider key ({key})"
    if not host_marker:
        check.fail(
            claim,
            f"nothing to prove — {key} is missing or holds no recognisable secret, so this "
            "check would pass even with the mask removed",
        )
    else:
        result = await check.run(sandbox, ["/bin/cat", str(key)])
        found = leaked(result.stdout)
        if found:
            check.fail(claim, f"the command read {found!r} out of the key file")
        else:
            check.ok(f"{claim} — {len(result.stdout)} bytes back, no key material")

    # --- nor the database ---------------------------------------------------
    claim = f"cannot read the database ({settings.db_path.name})"
    if not settings.db_path.exists():
        check.fail(claim, "nothing to prove — the database file does not exist yet")
    else:
        result = await check.run(sandbox, ["/bin/cat", str(settings.db_path)])
        if "SQLite format" in result.stdout:
            check.fail(claim, "the command read the SQLite header")
        else:
            check.ok(f"{claim} — {len(result.stdout)} bytes back, no SQLite header")

    # --- nor a stray copy of it ---------------------------------------------
    #
    # Masking agent-hub.db, -wal and -shm by name left `agent-hub.db.bak-*` readable:
    # the same job data under a name nobody enumerated. The whole data directory is
    # masked now, so the assertion is about the *listing*, which catches any future
    # sibling too rather than the three suffixes someone thought of.
    data_dir = settings.db_path.parent
    claim = f"cannot see anything else in the data directory ({data_dir})"
    host_entries = [entry.name for entry in data_dir.iterdir() if ".db" in entry.name]
    if not host_entries:
        check.fail(claim, f"nothing to prove — no database files in {data_dir} to hide")
    else:
        result = await check.run(sandbox, ["/bin/sh", "-c", f"ls -A {data_dir} 2>&1"])
        visible = [name for name in result.stdout.split() if ".db" in name]
        if visible:
            check.fail(claim, f"{len(visible)} database file(s) visible, e.g. {visible[0]}")
        else:
            check.ok(f"{claim} — {len(host_entries)} on the host, none of them visible")

    # --- it cannot escalate -------------------------------------------------
    #
    # `/bin/true` is the payload precisely because it does nothing: the assertion
    # is about sudo being unusable, and an inert argument keeps the check itself
    # from being the dangerous thing.
    claim = "cannot escalate with sudo"
    if shutil.which("sudo") is None:
        check.fail(claim, "nothing to prove — sudo is not installed on this host")
    else:
        result = await check.run(sandbox, ["/usr/bin/sudo", "-n", "/bin/true"])
        if result.exit_code == 0:
            check.fail(claim, "sudo -n /bin/true succeeded inside the sandbox")
        else:
            last = (result.stderr or result.stdout).strip().splitlines()
            check.ok(f"{claim} — exit {result.exit_code}: {last[-1] if last else 'no output'}")

    # --- it can still reach the network ------------------------------------
    claim = "can reach the network"
    fetch = (
        "import urllib.request as u;"
        f"print(u.urlopen({NETWORK_URL!r}, timeout=20).status)"
    )
    result = await check.run(
        sandbox, ["/usr/bin/python3", "-c", fetch], timeout=NETWORK_TIMEOUT
    )
    reached = result.exit_code == 0 and "200" in result.stdout
    if settings.tool_network:
        if reached:
            check.ok(f"{claim} — {NETWORK_URL} returned 200")
        else:
            detail = (result.stderr or result.stdout).strip().splitlines()
            check.fail(claim, f"exit {result.exit_code}: {detail[-1] if detail else 'no output'}")
    elif reached:
        check.fail(
            "network is off when AGENT_HUB_TOOL_NETWORK=0",
            "the command reached the internet anyway",
        )
    else:
        check.ok("network is off when AGENT_HUB_TOOL_NETWORK=0 — the request failed")

    # --- it can write its own workspace ------------------------------------
    claim = "can write the workspace"
    proof = check.workspace / "proof.txt"
    proof.unlink(missing_ok=True)
    result = await check.run(sandbox, ["/bin/sh", "-c", "echo written-inside > proof.txt"])
    if result.exit_code != 0:
        check.fail(claim, f"exit {result.exit_code}: {(result.stderr or '').strip()}")
    elif not proof.exists():
        check.fail(claim, "the command reported success but the host cannot see the file")
    elif proof.read_text().strip() != "written-inside":
        check.fail(claim, f"unexpected contents: {proof.read_text()!r}")
    else:
        check.ok(f"{claim} — {proof.name} is on the host with the right contents")

    # --- and nothing else --------------------------------------------------
    claim = "cannot write outside the workspace"
    outside = Path("/etc/agent-hub-sandbox-probe")
    result = await check.run(
        sandbox, ["/bin/sh", "-c", f"echo escaped > {outside}"]
    )
    if outside.exists():
        # Belt and braces: if the write really landed, do not leave it behind.
        outside.unlink(missing_ok=True)
        check.fail(claim, f"the command created {outside} on the host")
    elif result.exit_code == 0:
        check.fail(claim, "the write reported success (a writable overlay would hide the escape)")
    else:
        check.ok(f"{claim} — exit {result.exit_code}, /etc is read-only")

    # --- one job cannot see another's files --------------------------------
    #
    # `workspace_root` is masked with a tmpfs and only this job's directory is
    # bound back, so a neighbour's workspace should not exist at all.
    claim = "cannot see another job's workspace"
    neighbour = settings.workspace_root / ".check-sandbox-neighbour"
    neighbour.mkdir(parents=True, exist_ok=True)
    (neighbour / "secret-plan.txt").write_text("another job's file\n")
    try:
        result = await check.run(sandbox, ["/bin/cat", str(neighbour / "secret-plan.txt")])
        if "another job's file" in result.stdout:
            check.fail(claim, "the command read a file from a sibling workspace")
        else:
            check.ok(f"{claim} — exit {result.exit_code}, the sibling is not there")
    finally:
        shutil.rmtree(neighbour, ignore_errors=True)


async def check_environment(check: Check, kind: str) -> None:
    """Env scrubbing is unconditional, so it is asserted in both backends."""
    sandbox = build_sandbox(kind, check.workspace)
    claim = f"[{kind}] no service configuration or secret in the environment"
    result = await check.run(sandbox, ["/usr/bin/env"])
    if result.exit_code != 0:
        check.fail(claim, f"env exited {result.exit_code}")
        return
    offenders = [
        line.split("=", 1)[0]
        for line in result.stdout.splitlines()
        if line.startswith("AGENT_HUB_")
    ]
    marker = leaked(result.stdout)
    if offenders:
        check.fail(claim, f"leaked {offenders}")
    elif marker:
        check.fail(claim, f"leaked something matching {marker!r}")
    else:
        names = sorted(line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line)
        check.ok(f"{claim} — {len(names)} variables, all expected: {' '.join(names)}")


async def main() -> int:
    settings.ensure_dirs()
    statuses = await probe(force=True)

    workspace = settings.workspace_root / ".check-sandbox"
    logs = settings.workspace_root / ".check-sandbox-logs"
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(logs, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    check = Check(workspace, logs)
    try:
        print(f"env scrubbing (both backends), workspace {workspace}")
        await check_environment(check, UNCONFINED)

        state = statuses[SANDBOXED]
        if not state.available:
            print(f"\nsandboxed: UNAVAILABLE on this host — {state.reason}")
            print("nothing about confinement was proved; see the plan's provisioning follow-up.")
            return 2

        await check_environment(check, SANDBOXED)
        print(f"\nsandboxed confinement (network={'on' if settings.tool_network else 'off'})")
        await check_confinement(check)
    except SandboxError as exc:
        print(f"\nthe backend refused to run: {exc}")
        return 2
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(logs, ignore_errors=True)

    print()
    if check.failures:
        print(f"FAILURES ({len(check.failures)}):")
        for failure in check.failures:
            print(" -", failure)
        return 1
    print("OK: credentials masked, no escalation, network reachable, workspace writable")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
