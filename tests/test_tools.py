"""The agent tools: real commands, real side effects, and what happens when they fail.

Everything here runs `unconfined` against a temporary workspace with harmless
commands (`echo`, `cat`, `sleep`). Whether bubblewrap works is a property of the
host, not of this code, so confinement is pinned in `conftest` and the bwrap
backend gets asserted against separately by `tools/check_sandbox.py`, which is the
only place that can honestly claim "sandboxed" means something.

The provider is scripted per turn (`tool`, `tools_turn`, `envelope`, `says`), so a
test says what the model asks for and then asserts on what actually happened on
disk and in the database. Nothing reaches the network.

Two assertions recur and both matter:

- **The command's output is threaded back.** A tool that runs but whose result the
  model never sees is worse than no tool at all, so the tests check the *next*
  prompt, not just the row.
- **Nothing ran that should not have.** For a denied or refused call it is not
  enough that the status says `denied`; the file must not exist.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import settings
from app.db import db
from app.deps import engine
from app.orchestrator import agentloop as agentloop_mod
from app.orchestrator import sandbox as sandbox_mod
from app.orchestrator import tools as tools_mod
from app.orchestrator.sandbox import Completed, DirectSandbox, SandboxStatus
from app.orchestrator.tools import ToolInvocation, classify
from tests.conftest import (
    ONE_PHASE,
    FakeProvider,
    envelope,
    event_kinds,
    phase_rows,
    says,
    tool,
    tools_turn,
    tune,
    wait_for_approval,
    wait_for_job,
    wait_until,
)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(plan=ONE_PHASE)


async def tool_rows(job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select * from tool_calls where job_id=? order by created_at,rowid", (job_id,)
    )
    return [dict(row) for row in rows]


async def commands(job_id: str) -> list[str]:
    return [json.loads(row["args"]).get("command", "") for row in await tool_rows(job_id)]


async def _first_row_once_terminal(job_id: str) -> dict[str, Any] | None:
    """The first tool row once it has stopped being in flight, or None to keep waiting."""
    rows = await tool_rows(job_id)
    if rows and rows[0]["status"] not in {"pending", "running"}:
        return rows[0]
    return None


async def workspace_of(job_id: str) -> Path:
    return Path(await db.fetch_value("select workspace from jobs where id=?", (job_id,)))


async def pgrep(pattern: str) -> list[str]:
    """PIDs whose command line matches, so an orphan claim can be checked rather than assumed."""
    process = await asyncio.create_subprocess_exec(
        "pgrep",
        "-f",
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    return out.decode().split()


# ------------------------------------------------------------------- the happy path


async def test_a_command_runs_and_its_output_reaches_the_next_turn(
    job, provider: FakeProvider
) -> None:
    provider.tool_script = [
        tool("run", command="echo hello-from-the-shell"),
        says("I ran it and the shell printed hello-from-the-shell."),
    ]

    job_id = await job("Say hello from a real shell")
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert len(rows) == 1, f"expected one command, got {[r['tool'] for r in rows]}"
    row = rows[0]
    assert (row["tool"], row["status"], row["exit_code"]) == ("run", "ok", 0)
    assert "hello-from-the-shell" in row["stdout"]
    assert row["sandbox"] == "unconfined"
    assert row["turn"] == 1 and row["agent"] == "coder"
    assert row["started_at"] and row["finished_at"] >= row["started_at"]

    # The point of the loop: the model's next turn sees what the command printed.
    assert provider.prompts_containing("hello-from-the-shell"), provider.prompts
    assert any(offered for offered in provider.tools_seen), "the schemas were never offered"

    output = await db.fetch_value("select output from phases where job_id=? and seq=1", (job_id,))
    assert output == "I ran it and the shell printed hello-from-the-shell."


async def test_the_schemas_offered_are_the_four_tools(job, provider: FakeProvider) -> None:
    provider.tool_script = [says("Nothing needs running here.")]

    job_id = await job()
    await wait_for_job(job_id, "complete")

    offered = [seen for seen in provider.tools_seen if seen]
    assert offered, "a work phase must be offered tools"
    assert {schema["function"]["name"] for schema in offered[0]} == set(tools_mod.TOOL_NAMES)
    # Planning and synthesis stay one-shot: nothing to run, so nothing offered.
    assert provider.tools_seen[0] is None and provider.tools_seen[-1] is None


async def test_the_system_prompt_tells_the_agent_where_it_is(job, provider: FakeProvider) -> None:
    provider.tool_script = [says("Understood.")]

    job_id = await job()
    await wait_for_job(job_id, "complete")

    workspace = str(await workspace_of(job_id))
    work_systems = [
        system for system, offered in zip(provider.systems, provider.tools_seen) if offered
    ]
    assert work_systems, "no phase was run with tools"
    preamble = work_systems[0]
    assert "You have a working shell" in preamble
    assert workspace in preamble, "the agent must be told its working directory"
    assert "running unconfined" in preamble, "the confinement in force must be stated"


async def test_calls_in_one_turn_run_in_the_order_they_were_asked_for(
    job, provider: FakeProvider
) -> None:
    """Sequential execution is not a detail: `cat` only works after the `write_file`."""
    provider.tool_script = [
        tools_turn(
            ("write_file", {"path": "notes.txt", "content": "first line\n"}),
            ("run", {"command": "cat notes.txt"}),
            text="Writing the file, then reading it back to check.",
        ),
        says("notes.txt contains 'first line'."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["tool"] for row in rows] == ["write_file", "run"]
    assert all(row["status"] == "ok" for row in rows), rows
    assert "first line" in rows[1]["stdout"], "the second call did not see the first call's write"
    assert (await workspace_of(job_id) / "notes.txt").read_text() == "first line\n"

    # Prose alongside a tool call is still worth showing the operator.
    partials = [
        payload for payload in await event_kinds(job_id, "message") if payload.get("partial")
    ]
    assert any("reading it back" in payload["content"] for payload in partials)


async def test_the_text_envelope_works_when_function_calling_does_not(
    job, provider: FakeProvider
) -> None:
    """The live provider is a proxy whose tool support is unverified, so this path ships."""
    provider.tool_script = [
        envelope("run", command="echo envelope-path-works"),
        says("The envelope call ran."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["status"] for row in rows] == ["ok"]
    assert "envelope-path-works" in rows[0]["stdout"]
    # No assistant tool_call to hang a tool message on, so the result comes back as user input.
    assert provider.prompts_containing("Result of run:")


async def test_a_nonzero_exit_is_a_result_the_loop_carries_on_from(
    job, provider: FakeProvider
) -> None:
    provider.tool_script = [
        tool("run", command="echo trouble >&2; exit 3"),
        tool("run", command="echo recovered"),
        says("The first command failed with exit 3; the second worked."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [(row["status"], row["exit_code"]) for row in rows] == [("error", 3), ("ok", 0)]
    assert "trouble" in rows[0]["stderr"]
    assert provider.prompts_containing("exit=3"), "the failure must be readable by the model"
    phase = next(row for row in await phase_rows(job_id) if row["seq"] == 1)
    assert phase["status"] == "complete", "a failed command must not fail the phase"


# ----------------------------------------------------------------------- the budgets


async def test_running_out_of_turns_still_completes_the_phase(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    tune(monkeypatch, tool_max_turns=2)
    provider.tool_script = [
        tool("run", command="echo one"),
        tool("run", command="echo two"),
        tool("run", command="echo three"),  # never reached
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert await commands(job_id) == ["echo one", "echo two"]
    assert len(provider.tool_script) == 1, "the loop kept going past its turn budget"
    assert agentloop_mod.BUDGET_PROMPT in provider.prompts, "no closing summary was asked for"

    notices = [payload["message"] for payload in await event_kinds(job_id, "notice")]
    assert any("tool budget" in message for message in notices), notices

    phase = next(row for row in await phase_rows(job_id) if row["seq"] == 1)
    assert phase["status"] == "complete" and phase["output"]


async def test_a_command_that_overruns_is_killed_and_the_loop_continues(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    tune(monkeypatch, tool_timeout=1)
    provider.tool_script = [
        tool("run", command="sleep 30"),
        tool("run", command="echo still-here"),
        says("The sleep was killed after a second; the next command ran."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["status"] for row in rows] == ["timeout", "ok"]
    assert rows[0]["exit_code"] == -1
    assert rows[0]["duration_ms"] < 10_000, "the timeout did not actually cut the command short"
    # Not the exact rounded second: the point is that the model is told it was killed.
    assert provider.prompts_containing("killed after the")
    assert "still-here" in rows[1]["stdout"]
    assert not await pgrep("sleep 30"), "the timed-out command was left running"


async def test_output_past_the_cap_is_sliced_on_the_row_and_whole_on_disk(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    tune(monkeypatch, tool_output_limit=500)
    provider.tool_script = [
        tool("run", command='for i in $(seq 1 400); do echo "line $i of noise"; done'),
        says("That produced a lot of output."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    row = (await tool_rows(job_id))[0]
    assert row["status"] == "ok"
    assert row["truncated"] == 1
    assert "earlier bytes omitted" in row["stdout"]
    # The *tail* is kept: a failing command's useful line is its last one.
    assert "line 400 of noise" in row["stdout"]
    assert "line 1 of noise" not in row["stdout"]

    full = await workspace_of(job_id) / ".agent-hub" / f"tool-{row['id']}.out"
    assert full.exists(), "the full log must survive in the workspace"
    assert len(full.read_text().splitlines()) == 400


# -------------------------------------------------------------------- the guardrails


def test_classify_says_why_a_call_was_stopped(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def risk(name: str, **args: Any):
        return classify(ToolInvocation(id="x", tool=name, args=args), workspace=workspace)

    # Ordinary work must not gate, or the operator learns to click through everything.
    assert risk("run", command="ls -la") is None
    assert risk("run", command="pytest -q 2>&1 | tail -40") is None
    assert risk("run", command="git commit -am 'wip' && git log --oneline -3") is None

    assert risk("run", command="sudo systemctl restart agent-hub").reason == "privilege escalation"
    assert risk("run", command="git push origin main").reason == "pushes to a remote repository"
    assert risk("run", command="cat ~/.codex/config.toml").reason == "reads credentials"
    assert risk("run", command="apt-get install -y cowsay").level == "high"
    assert risk("run", command="pip install requests").level == "medium"

    assert risk("write_file", path="notes/plan.md") is None
    assert "outside the job workspace" in risk("write_file", path="../escape.txt").reason
    assert risk("write_file", path="/etc/hosts").level == "high"
    assert risk("read_file", path="/etc/passwd").level == "medium"

    assert risk("fetch", url="https://example.com/status") is None
    assert risk("fetch", url="https://example.com/things", method="POST").level == "high"


def test_refuse_closes_the_holes_the_sandbox_cannot_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `run` goes through the sandbox, so the other three need this layer.

    The distinction under test is *refused* versus *gated*. A gate is a question for
    the operator about one command; a refusal is the backend's own answer, and asking
    an operator to approve reading the provider key out of a job they deliberately
    sandboxed would turn a security setting into a dialog to click through.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def refusal(name: str, *, kind: str = "sandboxed", **args: Any):
        return tools_mod.refuse(
            ToolInvocation(id="x", tool=name, args=args), workspace=workspace, sandbox_kind=kind
        )

    # Sandboxed: what a command cannot see, the file tools must not see either.
    assert "masked" in refusal("read_file", path=str(Path.home() / ".ssh" / "id_rsa"))
    assert "masked" in refusal("read_file", path=str(settings.db_path))
    assert "masked" in refusal("write_file", path=str(settings.db_path))
    # ...and the host being read-only is not a decision to delegate either.
    assert "read-only" in refusal("write_file", path="/etc/hosts")
    assert "read-only" in refusal("write_file", path="../escape.txt")

    # Ordinary work in the workspace is untouched, including under the data directory
    # the mask covers — `wrap()` binds the job's own directory back afterwards.
    assert refusal("read_file", path="notes.md") is None
    assert refusal("write_file", path="out/report.md") is None
    assert refusal("read_file", path=str(workspace / "deep" / "file.txt")) is None
    # A read of the wider host filesystem is allowed, because a command can do it too.
    assert refusal("read_file", path="/etc/hosts") is None
    # And a *command* is the sandbox's problem, not this layer's: second-guessing it
    # here would mean two places deciding the same thing, differently.
    assert refusal("run", command=f"cat {settings.codex_config}") is None

    # Unconfined: the operator chose a backend with no boundary, so these go back to
    # being gates. Refusing here would be this layer inventing a policy nobody set.
    assert refusal("read_file", path=str(settings.codex_config), kind="unconfined") is None
    assert refusal("write_file", path="/etc/hosts", kind="unconfined") is None

    # The provider key is masked wherever it is configured. Pointed somewhere of its
    # own here so the assertion is about that rule and not about the data directory
    # happening to contain it.
    elsewhere = tmp_path / "elsewhere" / "config.toml"
    tune(monkeypatch, codex_config=elsewhere)
    assert "masked" in refusal("read_file", path=str(elsewhere))

    # Network off is the one rule that binds both backends: `unconfined` cannot take
    # the network from `run`, but a `fetch` that ignored the setting would be the
    # service contradicting the operator rather than a limit of the backend.
    assert refusal("fetch", url="https://example.com") is None
    tune(monkeypatch, tool_network=False)
    for kind in ("sandboxed", "unconfined"):
        message = refusal("fetch", url="https://example.com", kind=kind)
        assert "network access is turned off" in message, kind
    assert refusal("read_file", path="notes.md") is None, "only fetch is affected"


def test_no_file_mask_sits_inside_a_masked_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlapping entry is not redundant — it un-masks the file it names.

    `--tmpfs <dir>` hides a directory's whole listing, but a later
    `--ro-bind-try /dev/null <dir>/f` makes bwrap *create* that mount point inside the
    tmpfs, so `f` comes back in `ls` as an empty file. It reads as nothing either way,
    which is why this cost a `check_sandbox` run to notice: the name was visible while
    the contents never were. Re-adding a covered path "for good measure" is the easy
    mistake, so the non-overlap is asserted rather than left to review.
    """
    dirs, files = sandbox_mod._masked_paths()
    overlapping = [
        (path, entry) for path in files for entry in dirs if sandbox_mod._under(path, entry)
    ]
    assert overlapping == [], f"{overlapping[0][0]} would reappear inside {overlapping[0][1]}"

    # Dropping the entry must not narrow what counts as masked: the directory answers
    # for it. This is the pair that actually overlaps on a default host.
    assert sandbox_mod.is_masked(settings.db_path)
    assert settings.db_path.parent in dirs

    # And a path that moves *out* of a masked directory is still covered, which is why
    # the entries stay in the list and are filtered per call rather than deleted.
    elsewhere = Path("/srv/keys/config.toml")
    tune(monkeypatch, codex_config=elsewhere)
    assert elsewhere in sandbox_mod._masked_paths()[1]
    assert sandbox_mod.is_masked(elsewhere)


def test_only_an_outside_termination_counts_as_interrupted() -> None:
    """Which signals mean "nobody knows" and which mean "here is your answer".

    `interrupted` claims something specific — that the command's side effects may be
    complete, partial or absent — so it has to be reserved for deaths that really are
    unknowable. A fault is not one: a segfaulting build is a *result*, and re-running the
    phase for it would loop on the same crash instead of letting the agent read it.

    Our own timeout and cap kills are excluded in `run()`, which records no signal for
    them; that ordering is covered by the timeout and cap tests above, which would report
    `interrupted` instead of `timeout`/`capped` if it broke.
    """

    def completed(signalled: int) -> Completed:
        return Completed(
            exit_code=-signalled, stdout="", stderr="", duration_ms=1, signalled=signalled
        )

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        assert completed(sig).interrupted, sig
    assert completed(signal.SIGTERM).signal_name == "SIGTERM"

    # A fault is a result the agent should read and act on, not an unknown to retry.
    for sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE, signal.SIGILL):
        assert not completed(sig).interrupted, sig
    # SIGKILL is a result too: if systemd had escalated to KILL this process would be
    # gone as well and the row swept on restart, so a SIGKILL a *surviving* service got
    # to observe is far more often the OOM killer — "too much memory" is an answer.
    assert not completed(signal.SIGKILL).interrupted

    ordinary = Completed(exit_code=1, stdout="", stderr="", duration_ms=1)
    assert not ordinary.interrupted and ordinary.signal_name == ""


async def test_a_risky_command_waits_for_approval_and_then_runs(
    job, provider: FakeProvider, client: httpx.AsyncClient
) -> None:
    # Trips the `git push` guardrail while doing nothing but writing a local file: the
    # gate is a regex over the command string, and that is exactly what is under test.
    command = "echo 'about to git push' > gated.txt"
    provider.tool_script = [tool("run", command=command), says("It was approved and ran.")]

    job_id = await job()
    approval = await wait_for_approval(job_id)

    assert approval["kind"] == "tool"
    assert approval["risk"] == "high"
    assert "pushes to a remote repository" in approval["detail"]
    assert "coder" in approval["detail"] and "Do the work" in approval["detail"]
    assert approval["action"] == command
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "blocked"

    row = (await tool_rows(job_id))[0]
    assert row["status"] == "pending", "the row must exist before the decision, not after"
    assert row["approval_id"] == approval["id"]
    workspace = await workspace_of(job_id)
    assert not (workspace / "gated.txt").exists(), "it ran before anyone approved it"

    response = await client.post(
        f"/api/jobs/{job_id}/approvals/{approval['id']}", json={"decision": "approved"}
    )
    assert response.status_code == 200, response.text
    assert await wait_for_job(job_id, "complete") == "complete"

    row = (await tool_rows(job_id))[0]
    assert row["status"] == "ok"
    assert (workspace / "gated.txt").read_text().strip() == "about to git push"


async def test_a_declined_command_does_not_run_and_the_phase_carries_on(
    job, provider: FakeProvider, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Covers the reject branch and the out-of-workspace path check in one run."""
    escape = tmp_path / "escaped.txt"
    provider.tool_script = [
        tool("write_file", path=str(escape), content="this must never be written"),
        tool("run", command="echo carried-on > fallback.txt"),
        says("The operator declined the write, so I left a note in the workspace instead."),
    ]

    job_id = await job()
    approval = await wait_for_approval(job_id)
    assert "outside the job workspace" in approval["detail"]

    response = await client.post(
        f"/api/jobs/{job_id}/approvals/{approval['id']}",
        json={"decision": "rejected", "note": "not outside the workspace"},
    )
    assert response.status_code == 200, response.text
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["status"] for row in rows] == ["denied", "ok"]
    assert not escape.exists(), "a denied write still happened"
    assert (await workspace_of(job_id) / "fallback.txt").exists()
    assert provider.prompts_containing("operator declined"), "the model was not told why"


async def test_a_tool_gate_is_raised_even_in_yolo_mode(job, provider: FakeProvider) -> None:
    """Yolo means "do not review my plan"; it was never a decision about `git push`."""
    provider.tool_script = [
        tool("run", command="echo 'git push --force' > note.txt"),
        says("done"),
    ]

    job_id = await job(mode="yolo")
    approval = await wait_for_approval(job_id)

    assert approval["kind"] == "tool"
    assert not approval["auto"], "a guardrail hit must not be auto-approved"
    assert (await tool_rows(job_id))[0]["status"] == "pending"


# ------------------------------------------------------------------------ recovery


async def test_a_restart_mid_command_interrupts_the_row_and_warns_the_rerun(
    job, provider: FakeProvider
) -> None:
    """The one place "resume, don't replay" cannot be perfect, handled honestly."""
    marker = "sleep 3613"
    # Nothing after the call, so the phase is parked inside the command when the
    # service goes down.
    provider.tool_script = [tool("run", command=marker)]

    job_id = await job()
    # Wait for the *process*, not the row. The row is committed 'running' before the
    # process is spawned — that ordering is the whole point of it — so a running row
    # is not yet evidence of a running command. Waiting on the process gets both,
    # since the row necessarily landed first.
    await wait_until(lambda: pgrep(marker))
    assert (await tool_rows(job_id))[0]["status"] == "running"

    # --- the restart -------------------------------------------------------
    await engine.shutdown(grace=2)
    assert not engine.is_running(job_id)
    assert not await pgrep(marker), "shutdown left the command running"
    assert (await tool_rows(job_id))[0]["status"] == "running", (
        "the row must still say 'running' until recovery decides what became of it"
    )

    provider.tool_script = [
        tool("run", command="echo checked the workspace first"),
        says("The interrupted sleep left nothing behind, so I carried on."),
    ]
    assert job_id in await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["finished_at"], "an interrupted row must still be closed out"
    assert (await commands(job_id)).count(marker) == 1, "the interrupted command ran again"

    phase = next(row for row in await phase_rows(job_id) if row["seq"] == 1)
    assert phase["attempts"] == 2 and phase["status"] == "complete"

    warned = provider.prompts_containing("previous attempt at this phase was interrupted")
    assert warned, "the re-run was not told what was in flight"
    assert marker in warned[0], "the notice must name the command"


async def test_a_command_killed_from_outside_is_unknown_rather_than_failed(
    job, provider: FakeProvider
) -> None:
    """A signal death is not an exit code, and the loop must not build on one.

    The test above never reaches this path. `engine.shutdown()` cancels the job task, so
    the command dies from *inside* — through CancelledError — and the row is left
    'running' for the sweep to resolve. A real `systemctl restart` behaves differently:
    systemd SIGTERMs the whole cgroup, so the child is already dead when
    `process.wait()` returns -15 to a service that is still perfectly alive and has not
    been told to stop. `kill -TERM $$` reproduces exactly that, from the service's point
    of view, without needing systemd.

    `tools.check_tool_restart` found this against the real unit: the command came back as
    an ordinary `error`, and the loop then carried the phase to completion on it. That is
    the failure worth naming — not the label, but telling the model a command *failed*
    when the truth is that nobody knows whether it ran. For `sleep` that is harmless; for
    `git push` or a migration it is the worst answer available.
    """
    killed = "kill -TERM $$"
    provider.tool_script = [tool("run", command=killed), says("must never be reached")]

    job_id = await job()
    row = await wait_until(
        lambda: _first_row_once_terminal(job_id), timeout=15.0
    )

    assert row["status"] == "interrupted", f"a signal death was reported as {row['status']}"
    assert row["exit_code"] < 0, "the signal should still be legible on the row"
    assert row["finished_at"], "an interrupted row must still be closed out"

    # Wait for the task, not just the row: the row is committed before the cancellation
    # unwinds, so the phase is still 'active' for a moment after it flips.
    await wait_until(lambda: not engine.is_running(job_id))

    # The loop stopped instead of carrying on. This is the half that matters: a correct
    # label on a phase that completed anyway would still be a phase built on nothing.
    assert provider.tool_script, "the loop kept going after an interrupted command"
    assert not await pgrep(killed), "left a process behind"

    phase = next(row for row in await phase_rows(job_id) if row["seq"] == 1)
    assert phase["status"] == "pending", "the phase must go back for a re-run, not complete"
    assert phase["attempts"] == 1
    assert phase["output"] is None
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) not in {
        "complete",
        "error",
        "stopped",
    }, "an unknown outcome must not settle the job either way"

    # And the re-run is told what was in flight, by the same machinery a hard crash uses.
    provider.tool_script = [
        tool("run", command="echo checked the workspace"),
        says("The killed command left nothing behind, so I carried on."),
    ]
    assert job_id in await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert (await commands(job_id)).count(killed) == 1, "the interrupted command ran again"
    warned = provider.prompts_containing("previous attempt at this phase was interrupted")
    assert warned and killed in warned[0], "the notice must name the command"


async def test_a_pending_gate_is_cancelled_by_a_restart(job, provider: FakeProvider) -> None:
    """A gate for a call that will never run must not sit in the operator's inbox."""
    command = "echo 'sudo apt-get install cowsay' > note.txt"
    provider.tool_script = [tool("run", command=command)]

    job_id = await job()
    approval = await wait_for_approval(job_id)

    await engine.shutdown(grace=2)
    provider.tool_script = [says("Nothing needed running after all.")]
    await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert (await tool_rows(job_id))[0]["status"] == "cancelled"
    gate = await db.fetch_one("select * from approvals where id=?", (approval["id"],))
    assert gate["status"] == "rejected" and "re-run" in gate["decision_note"]
    assert await commands(job_id) == [command], "the ungated command ran anyway"
    assert not (await workspace_of(job_id) / "note.txt").exists()
    warned = provider.prompts_containing("was waiting for approval and never ran")
    assert warned and command in warned[0]


async def test_stopping_a_job_at_a_command_gate_still_ends_it(
    job, provider: FakeProvider, client: httpx.AsyncClient
) -> None:
    """A stop must win against the gate's own attempt to put the status back.

    The gate writes 'blocked' on the way in and restores the previous status on the way
    out, and that restore runs during cancellation — after ``stop`` has already written
    'stopped'. Unguarded it wrote 'running' back over the terminal status, and because
    the phases were already 'skipped' and the startup sweep skips terminal jobs, nothing
    ever corrected it: the job read 'running' forever with no task behind it. Live, that
    is a job the operator cannot stop, delete or restart their way out of.
    """
    # Trips the `sudo` guardrail while doing nothing but writing a local file, so the
    # workspace itself says whether the gated command ran.
    command = "echo 'sudo apt-get install cowsay' > note.txt"
    provider.tool_script = [tool("run", command=command)]

    job_id = await job()
    await wait_for_approval(job_id)

    response = await client.post(f"/api/jobs/{job_id}/stop")
    assert response.status_code == 200, response.text
    assert await wait_for_job(job_id, "stopped") == "stopped"

    # Not just at the moment of stopping — the restore lands a beat later, so let the
    # loop finish unwinding and check the status is still terminal.
    await wait_until(lambda: not engine.is_running(job_id))
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "stopped"

    statuses = [
        json.loads(row["payload"])["status"]
        for row in await db.fetch_all(
            "select payload from events where job_id=? and kind='status' order by id", (job_id,)
        )
    ]
    assert statuses[-1] == "stopped", (
        f"a stopped job announced {statuses[-1]!r} to every open stream: {statuses}"
    )
    assert not (await workspace_of(job_id) / "note.txt").exists(), (
        "the gated command ran despite the stop"
    )
    assert await commands(job_id) == [command], "the command was recorded more than once"
    assert (await tool_rows(job_id))[0]["status"] == "cancelled"


async def test_stopping_a_job_kills_the_command_it_was_running(
    job, provider: FakeProvider, client: httpx.AsyncClient
) -> None:
    marker = "sleep 3607"
    provider.tool_script = [tool("run", command=marker)]

    job_id = await job()
    await wait_until(lambda: pgrep(marker))

    response = await client.post(f"/api/jobs/{job_id}/stop")
    assert response.status_code == 200, response.text
    assert await wait_for_job(job_id, "stopped") == "stopped"

    assert not await pgrep(marker), "the command outlived the job that started it"
    assert (await tool_rows(job_id))[0]["status"] == "interrupted", (
        "a stopped job must not keep claiming a command is running"
    )
    assert (await tool_rows(job_id))[0]["finished_at"]


# --------------------------------------------------------------------- confinement


async def test_a_job_records_the_confinement_it_actually_used(
    job, provider: FakeProvider, client: httpx.AsyncClient
) -> None:
    provider.tool_script = [tool("run", command="echo which-sandbox"), says("done")]

    job_id = await job(sandbox="unconfined")
    assert await wait_for_job(job_id, "complete") == "complete"

    assert await db.fetch_value("select sandbox from jobs where id=?", (job_id,)) == "unconfined"
    rows = await tool_rows(job_id)
    assert {row["sandbox"] for row in rows} == {"unconfined"}

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["sandbox"] == "unconfined"
    assert [call["id"] for call in snapshot["tool_calls"]] == [row["id"] for row in rows]
    assert "stdout" not in snapshot["tool_calls"][0], "the snapshot must stay metadata-only"

    listing = (await client.get(f"/api/jobs/{job_id}/tool-calls")).json()
    assert listing[0]["args"] == {"command": "echo which-sandbox"}

    detail = (await client.get(f"/api/jobs/{job_id}/tool-calls/{rows[0]['id']}")).json()
    assert "which-sandbox" in detail["stdout"]
    assert detail["stdout_path"] == f".agent-hub/tool-{rows[0]['id']}.out"
    assert (await workspace_of(job_id) / detail["stdout_path"]).exists()


async def test_a_sandboxed_job_cannot_reach_the_key_with_read_file(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole `refuse()` exists to close, exercised through the whole loop.

    `read_file` runs in the service process, so without that check a job the operator
    deliberately sandboxed could read the provider key by asking for the tool that
    skips the boundary. Nothing here spawns bwrap — a refused call never reaches
    `execute()` — so the assertion holds on hosts where the backend cannot run.
    """
    monkeypatch.setitem(
        sandbox_mod._status,
        "sandboxed",
        SandboxStatus(id="sandboxed", label="Sandboxed", available=True, reason="fine"),
    )
    key = Path.home() / ".codex" / "config.toml"
    provider.tool_script = [
        tool("read_file", path=str(key)),
        says("The key is masked, as it should be; I worked from the workspace instead."),
    ]

    job_id = await job(sandbox="sandboxed")
    assert await wait_for_job(job_id, "complete") == "complete"

    row = (await tool_rows(job_id))[0]
    assert row["status"] == "refused", f"the key was reachable via read_file: {row['status']}"
    assert row["sandbox"] == "sandboxed"
    assert row["approval_id"] is None

    # The distinction that matters: nobody was asked. A gate here would let an
    # operator click away the confinement they chose for the job.
    assert await db.fetch_value(
        "select count(*) from approvals where job_id=?", (job_id,)
    ) == 0
    assert "blocked" not in [
        payload["status"] for payload in await event_kinds(job_id, "status")
    ]

    # And the model was told enough to stop trying, rather than just "no".
    assert provider.prompts_containing("masked"), provider.prompts
    assert not provider.prompts_containing("bearer_token"), "key material reached the model"


async def test_an_unavailable_sandbox_is_refused_at_creation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sandbox_mod._status,
        "sandboxed",
        SandboxStatus(
            id="sandboxed",
            label="Sandboxed",
            available=False,
            reason="the kernel denies unprivileged user namespaces",
        ),
    )

    response = await client.post("/api/jobs", json={"task": "Do it safely", "sandbox": "sandboxed"})
    assert response.status_code == 400, response.text
    assert "not available on this host" in response.json()["detail"]
    assert "user namespaces" in response.json()["detail"], "the reason must be actionable"


async def test_a_job_errors_rather_than_running_unconfined_by_accident(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst possible failure mode is a silent downgrade, so there isn't one."""
    monkeypatch.setitem(
        sandbox_mod._status,
        "sandboxed",
        SandboxStatus(
            id="sandboxed", label="Sandboxed", available=False, reason="bwrap denied by the kernel"
        ),
    )
    tune(monkeypatch, sandbox_default="sandboxed")
    provider.tool_script = [tool("run", command="echo must-not-run"), says("done")]

    job_id = await job()
    assert await wait_for_job(job_id, "error") == "error"

    error = await db.fetch_value("select error from jobs where id=?", (job_id,))
    assert "not available on this host" in error and "bwrap denied" in error
    assert await tool_rows(job_id) == [], "a command ran despite the sandbox being unavailable"
    assert provider.prompts == [], "the job should fail before it even plans"
    assert [payload["error"] for payload in await event_kinds(job_id, "error")], (
        "the operator was never told why the job failed"
    )


async def test_disabling_tools_returns_to_one_call_per_phase(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch: `AGENT_HUB_TOOLS=0` is the pre-tools behaviour, unchanged."""
    tune(monkeypatch, tools_enabled=False)
    provider.tool_script = [tool("run", command="echo must-not-run")]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert await tool_rows(job_id) == []
    assert provider.tool_script, "the script was consumed despite tools being off"
    assert all(offered is None for offered in provider.tools_seen)
    assert await db.fetch_value("select sandbox from jobs where id=?", (job_id,)) is None
    assert all("You have a working shell" not in system for system in provider.systems), (
        "the tool preamble was sent to an agent that has no tools"
    )


# --------------------------------------------------------------------------- fetch


async def test_fetch_refuses_an_internal_address_instead_of_crashing(
    job, provider: FakeProvider
) -> None:
    """`fetch` runs in the service process, so loopback would reach around the sandbox."""
    provider.tool_script = [
        tool("fetch", url="http://127.0.0.1:8090/api/jobs"),
        tool("fetch", url="not-a-url"),
        says("Both were refused, so I did not try again."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["status"] for row in rows] == ["error", "error"]
    assert "internal address" in rows[0]["stderr"]
    assert "absolute http(s) URL" in rows[1]["stderr"]
    assert provider.prompts_containing("Refused:"), "the model must be told it was refused"


async def test_fetch_honours_an_allowlist_when_one_is_configured(
    job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    tune(monkeypatch, fetch_allow_hosts=("example.com",))
    provider.tool_script = [
        tool("fetch", url="https://elsewhere.invalid/data"),
        says("That host is not allowed."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    row = (await tool_rows(job_id))[0]
    assert row["status"] == "error"
    assert "not in the configured fetch allowlist" in row["stderr"]


# ------------------------------------------------------------------- malformed asks


async def test_an_unknown_tool_is_recorded_and_explained(job, provider: FakeProvider) -> None:
    provider.tool_script = [
        tool("nonesuch", whatever=1),
        tool("run", command="echo used the real one"),
        says("I used run instead."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [(row["tool"], row["status"]) for row in rows] == [
        ("nonesuch", "error"),
        ("run", "ok"),
    ]
    assert "Unknown tool" in rows[0]["stderr"]
    assert provider.prompts_containing("Available: run, read_file, write_file, fetch")


async def test_unparsable_arguments_come_back_as_a_result_not_an_exception(
    tmp_path: Path,
) -> None:
    outcome = await tools_mod.execute(
        ToolInvocation(id="x", tool="run", args={"__unparsed__": '{"command": '}),
        sandbox=DirectSandbox(tmp_path),
        workspace=tmp_path,
    )
    assert outcome.status == "error"
    assert "not valid JSON" in outcome.content


async def test_reading_a_missing_file_is_a_result_the_agent_can_act_on(
    job, provider: FakeProvider
) -> None:
    provider.tool_script = [
        tool("read_file", path="does-not-exist.md"),
        tool("write_file", path="does-not-exist.md", content="now it does\n"),
        tool("read_file", path="does-not-exist.md"),
        says("It was missing, so I wrote it."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await tool_rows(job_id)
    assert [row["status"] for row in rows] == ["error", "ok", "ok"]
    assert "does not exist" in rows[0]["stderr"]
    assert "now it does" in rows[2]["stdout"]
