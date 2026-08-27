"""The agent loop: one phase, many turns, real side effects.

``engine.py`` runs a phase as a single provider call, which is right for planning and
synthesis — they only need to think. Work phases need to *act*, so this replaces that
one call with a bounded loop: ask, run what the model asked for, feed the result back,
repeat until it stops calling tools or a budget runs out.

Three things here are deliberate:

**The loop is never retried as a unit.** ``providers.complete()`` retries transient
HTTP failures, which is safe because a provider call has no side effects. The loop
does have side effects, so a failure inside it unwinds to the phase, and the phase
re-runs from turn 1 with a fresh conversation and an explicit notice about anything
that was interrupted mid-flight.

**A tool failure is a result; an interruption is not.** A non-zero exit is exactly the
information the model needs, so it comes back as a tool message and the loop continues.
A command killed from *outside* is the opposite: a ``systemctl restart`` SIGTERMs the
whole cgroup, so the child dies while this process is still alive, and nothing then says
whether its side effects landed. Treating that as a result would complete the phase on
an unknown, so it ends the phase instead — the row is committed ``interrupted`` and the
job is parked for a re-run that is told which command was in flight. Non-zero exits
continue; a termination signal, a cancellation, or a broken provider stop the phase.

**Hitting a budget still completes the phase.** When turns or wall-clock run out the
loop makes one final text-only call asking for a summary of what was done and what
remains, and that becomes the phase output. A phase that ran twelve useful commands
should not fail because it wanted a thirteenth.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.db import Database
from app.events import EventStore
from app.logging_setup import get_logger
from app.models import TERMINAL_JOB_STATUSES
from app.orchestrator import approvals, messages as messages_mod, questions, tools
from app.orchestrator.providers import (
    Message,
    Provider,
    ProviderError,
    ToolCallRequest,
    complete_with_retry,
    parse_json_response,
)
from app.orchestrator.sandbox import _Sandbox
from app.orchestrator.tools import Risk

log = get_logger("agent_hub.agentloop")

#: Tool results older than this many turns are stubbed out when composing the next
#: request. Twelve turns of full output would otherwise be a six-figure prompt.
FULL_RESULT_WINDOW = 6
STUB_LIMIT = 400

#: Headroom for the slice stored on a ``tool_calls`` row. Every outcome is already
#: bounded to ``tool_output_limit`` *plus* a short "earlier bytes omitted" notice, so
#: re-clipping to the limit itself would shave the end off the tail — the most recent
#: lines, which are the ones worth reading. The margin covers the notice, nothing more.
ROW_SLICE_MARGIN = 512


TOOL_PREAMBLE = """
You have a working shell and can act, not just describe. Available tools:

- run(command, timeout?) — a bash command in your workspace. Pipes, redirects and && work.
- read_file(path, start_line?, max_lines?) — read a file, or list a directory.
- write_file(path, content, append?) — write a real file.
- fetch(url, method?, body?, headers?) — an HTTP request.
- ask_operator(question, detail?, options?) — ask the human and wait for their answer.

Working agreement:

- Your working directory is {workspace}. Relative paths resolve there, and it is the
  one place you can reliably write. {sandbox_note}
- Verify by running. If you claim tests pass, run them and quote the output. An
  assertion you did not check is worth less than saying you could not check it.
- Nothing is interactive. A command that waits for input will hang until it is killed,
  so pass flags like -y or --no-input instead.
- Every command is recorded and shown to the operator. Commands that look risky —
  sudo, package installs, pushing to a remote, reading credentials — pause for a human
  to approve before they run, so expect an occasional wait rather than a refusal.
- Ask rather than assume, but only about things you cannot find out. If the task is
  ambiguous in a way that changes what you build, or needs a fact that is not in the
  workspace, call ask_operator with concrete options. If you could settle it by
  reading a file or running a command, do that instead — and never use ask_operator to
  request permission, which is handled for you.
- Stop calling tools when the phase is done, and reply with your findings. Your last
  message with no tool call is what the next specialist and the operator will read, so
  make it stand on its own: what you did, what the output showed, what remains.

If function calling is unavailable to you, you may instead reply with exactly this
JSON and nothing else: {{"tool": "run", "args": {{"command": "ls -la"}}}}
""".strip()

SANDBOX_NOTES = {
    "sandboxed": (
        "The rest of the filesystem is readable but read-only, and credential "
        "directories are masked, so do not plan around writing outside the workspace. "
        "read_file and write_file are held to the same limits as a command, so "
        "reaching a masked path with a different tool is not a route around them."
    ),
    "unconfined": (
        "You are running unconfined as the service user, so a mistake outside the "
        "workspace is real. Stay inside it unless the task genuinely requires otherwise."
    ),
}

#: Added when the operator turned the network off. Saying so up front is cheaper than
#: letting the agent spend three turns discovering it one refusal at a time.
NO_NETWORK_NOTE = (
    "This deployment has no network access: fetch is refused and commands cannot reach "
    "the internet, so do not plan around downloading anything."
)

BUDGET_PROMPT = (
    "You have used your tool budget for this phase, so no more tools are available. "
    "Summarise now: what you did, what the commands showed, what is verified, and what "
    "remains for the next specialist. Do not request further tools."
)


@dataclass(slots=True)
class LoopResult:
    text: str
    turns: int
    calls: int
    exhausted: bool


def build_system_prompt(role_prompt: str, *, workspace: Path, sandbox_kind: str) -> str:
    note = SANDBOX_NOTES.get(sandbox_kind, "")
    if not settings.tool_network:
        note = f"{note} {NO_NETWORK_NOTE}".strip()
    return role_prompt + "\n\n" + TOOL_PREAMBLE.format(workspace=workspace, sandbox_note=note)


def envelope_calls(text: str) -> list[ToolCallRequest]:
    """Read a text-envelope tool call, for endpoints without function calling.

    The configured provider is a proxy in front of several upstreams and its
    tool-calling support is not guaranteed, so the preamble documents a JSON shape as
    a fallback. Accepted only when the parsed object actually names a known tool —
    otherwise ordinary prose that happens to contain braces would be mistaken for a
    command.
    """
    if not text or "tool" not in text:
        return []
    try:
        parsed = parse_json_response(text)
    except ProviderError:
        return []
    if not isinstance(parsed, dict):
        return []
    name = parsed.get("tool")
    if name not in tools.TOOL_NAMES:
        return []
    args = parsed.get("args")
    if not isinstance(args, dict):
        args = {key: value for key, value in parsed.items() if key != "tool"}
    return [ToolCallRequest(id=f"env_{uuid.uuid4().hex[:8]}", name=str(name), arguments=json.dumps(args))]


def _trim(messages: list[Message]) -> list[Message]:
    """Shrink stale tool output so a long phase does not grow an unbounded prompt.

    Recent output is what the model is reasoning about; the tenth-most-recent
    ``ls`` is context it has already used. The stub says explicitly that content was
    dropped, so the model does not read a truncated result as an empty one.
    """
    tool_positions = [index for index, message in enumerate(messages) if message.role == "tool"]
    keep = set(tool_positions[-FULL_RESULT_WINDOW:])
    trimmed: list[Message] = []
    for index, message in enumerate(messages):
        if message.role == "tool" and index not in keep and len(message.content) > STUB_LIMIT:
            trimmed.append(
                Message(
                    role=message.role,
                    content=message.content[:STUB_LIMIT] + "\n… [earlier result trimmed from context]",
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                )
            )
        else:
            trimmed.append(message)
    return trimmed


class ToolLoop:
    """Runs one phase's conversation. One instance per phase, not reused."""

    def __init__(
        self,
        *,
        db: Database,
        store: EventStore,
        provider: Provider,
        sandbox: _Sandbox,
        workspace: Path,
        job_id: str,
        phase_id: int,
        phase_name: str,
        agent: str,
        cancel: asyncio.Event,
    ) -> None:
        self.db = db
        self.store = store
        self.provider = provider
        self.sandbox = sandbox
        self.workspace = workspace
        self.job_id = job_id
        self.phase_id = phase_id
        self.phase_name = phase_name
        self.agent = agent
        self.cancel = cancel
        self.calls = 0

    async def run(self, *, system: str, prompt: str) -> LoopResult:
        messages: list[Message] = [Message(role="user", content=prompt)]
        deadline = time.monotonic() + settings.tool_wall_clock
        max_turns = max(1, settings.tool_max_turns)
        turn = 0

        while turn < max_turns:
            if self.cancel.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                log.info(
                    "phase hit its tool wall clock",
                    extra={"job_id": self.job_id, "phase_id": self.phase_id, "turn": turn},
                )
                break

            turn += 1
            interjections = await messages_mod.drain(self.db, self.job_id, immediate_only=True)
            if interjections:
                # In as a user turn, before the provider call, so the model reads it as
                # the operator speaking rather than as tool output or as part of its own
                # reasoning. Announced on the stream too: an instruction that changed
                # what an agent did mid-phase should be visible at the point it landed,
                # not inferred later from a change of direction.
                messages_text = messages_mod.as_prompt(interjections)
                messages.append(
                    Message(
                        role="user",
                        content=(
                            "The operator has just sent this while you were working. Take "
                            f"it into account from here on:\n\n{messages_text}"
                        ),
                    )
                )
                await self.store.record(
                    self.job_id,
                    "guidance",
                    {
                        "messages": interjections,
                        "phase_id": self.phase_id,
                        "immediate": True,
                        "turn": turn,
                    },
                    source="operator",
                )
            completion = await complete_with_retry(
                self.provider,
                system=system,
                messages=_trim(messages),
                tools=tools.TOOL_SCHEMAS,
                cancel=self.cancel,
                on_wait=self._provider_wait_notice,
            )

            native = bool(completion.tool_calls)
            calls = completion.tool_calls or envelope_calls(completion.text)
            if not calls:
                return LoopResult(text=completion.text, turns=turn, calls=self.calls, exhausted=False)

            # The assistant turn goes in before the results, so the conversation stays
            # well-formed even if a tool raises: a tool message with no preceding
            # request is rejected outright by strict endpoints.
            if native:
                messages.append(completion.as_message())
            elif completion.text.strip():
                messages.append(Message(role="assistant", content=completion.text))

            if completion.text.strip():
                await self.store.record(
                    self.job_id,
                    "message",
                    {
                        "content": completion.text,
                        "phase_id": self.phase_id,
                        "phase": self.phase_name,
                        "partial": True,
                        "turn": turn,
                    },
                    source=self.agent,
                )

            for call in calls:
                outcome = await self._invoke(call, turn=turn)
                if native:
                    messages.append(
                        Message(
                            role="tool",
                            content=outcome.content,
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                else:
                    # No assistant tool_call to attach a tool message to, so the result
                    # comes back as user input instead.
                    messages.append(
                        Message(role="user", content=f"Result of {call.name}:\n{outcome.content}")
                    )

        return await self._wrap_up(system, messages, turn)

    async def _wrap_up(self, system: str, messages: list[Message], turn: int) -> LoopResult:
        """One text-only call so an exhausted phase still produces its output."""
        messages = _trim(messages) + [Message(role="user", content=BUDGET_PROMPT)]
        await self.store.record(
            self.job_id,
            "notice",
            {
                "message": (
                    f"'{self.phase_name}' used its tool budget after {self.calls} command(s); "
                    "asked the agent to summarise."
                ),
                "phase_id": self.phase_id,
            },
            source="system",
        )
        completion = await complete_with_retry(
            self.provider,
            system=system,
            messages=messages,
            cancel=self.cancel,
            on_wait=self._provider_wait_notice,
        )
        return LoopResult(text=completion.text, turns=turn, calls=self.calls, exhausted=True)

    # ------------------------------------------------------------------ one call

    async def _invoke(self, call: ToolCallRequest, *, turn: int) -> tools.ToolOutcome:
        invocation = tools.ToolInvocation(
            id=uuid.uuid4().hex[:12],
            tool=call.name,
            args=call.args(),
            call_id=call.id,
            turn=turn,
        )
        self.calls += 1

        if call.name not in tools.TOOL_NAMES:
            outcome = tools.ToolOutcome(
                status="error",
                content=f"Unknown tool '{call.name}'. Available: {', '.join(tools.TOOL_NAMES)}.",
            )
            await self._record_row(invocation, outcome, approval_id=None)
            return outcome

        await self.db.execute(
            "insert into tool_calls(id,job_id,phase_id,turn,agent,tool,args,status,sandbox,created_at)"
            " values(?,?,?,?,?,?,?,'pending',?,unixepoch('subsec'))",
            (
                invocation.id,
                self.job_id,
                self.phase_id,
                turn,
                self.agent,
                invocation.tool,
                json.dumps(invocation.args)[:20000],
                self.sandbox.kind,
            ),
        )

        approval_id: str | None = None
        # Refuse before gating. `read_file`, `write_file` and `fetch` do not go through
        # the sandbox, so this is the only thing standing between a sandboxed job and
        # the provider key — and it is not a question for the operator: approving a
        # command gate should never be a way to revoke the job's own confinement.
        refusal = tools.refuse(
            invocation, workspace=self.workspace, sandbox_kind=self.sandbox.kind
        )
        if refusal is not None:
            outcome = tools.ToolOutcome(status="refused", content=refusal)
            await self._finalise_row(invocation, outcome, None)
            await self._emit(
                invocation,
                outcome,
                risk=Risk(level="high", reason=f"refused by the {self.sandbox.kind} backend"),
            )
            return outcome

        risk = tools.classify(invocation, workspace=self.workspace)
        if risk is not None:
            approval_id, allowed = await self._gate(invocation, risk)
            if not allowed:
                outcome = tools.ToolOutcome(
                    status="denied",
                    content=(
                        f"The operator declined to run this ({risk.reason}). Do not retry it. "
                        "Either find another way to make progress or explain what you need."
                    ),
                )
                await self._finalise_row(invocation, outcome, approval_id)
                await self._emit(invocation, outcome, risk=risk)
                return outcome

        # Committed as 'running' *before* the process exists. A shell command's side
        # effects live in the filesystem, where the engine cannot roll them back, so
        # the one guarantee worth having is that a crash always leaves evidence a
        # command was in flight. recover() turns these into 'interrupted', and the
        # phase's next attempt is told so explicitly.
        await self.db.execute(
            "update tool_calls set status='running',started_at=unixepoch('subsec') where id=?",
            (invocation.id,),
        )
        asking = invocation.tool == "ask_operator"
        await self._set_action(
            f"Asking you: {invocation.display[:110]}" if asking else f"$ {invocation.display[:120]}"
        )

        try:
            outcome = (
                await self._ask_question(invocation)
                if asking
                else await tools.execute(
                    invocation, sandbox=self.sandbox, workspace=self.workspace
                )
            )
        except asyncio.CancelledError:
            # Leave the row as 'running'. It genuinely was, and recovery is the only
            # thing that should get to decide what became of it.
            raise

        await self._finalise_row(invocation, outcome, approval_id)
        # No `tool_call` event for a question. `questions.request` and `questions.answer`
        # already narrate it into the stream with the options and the answer attached,
        # and a second event saying `$ Which of these did you mean?` would render the
        # same moment twice, worse. The `tool_calls` row is still written, so the audit
        # trail and the Commands view are unaffected.
        if not asking:
            await self._emit(invocation, outcome, risk=risk)
        if outcome.status == "interrupted":
            # The row is committed; now stop. Something outside killed this command, so
            # nobody knows whether it did its work — and a loop that carried on would
            # complete the phase on exactly that unknown. Cancelling hands the job to
            # `_park_for_recovery`, which resets this phase to pending so the next start
            # re-runs it with `interrupted_notice()` naming the command. That is the
            # same route a hard crash takes; this is the graceful-restart case, where
            # systemd SIGTERMs the child before the service is told to shut down, so
            # `sweep_interrupted()` never sees a 'running' row to fix.
            raise asyncio.CancelledError()
        await self._set_action(self.phase_name)
        return outcome

    @asynccontextmanager
    async def _blocked(self) -> AsyncIterator[None]:
        """Hold the job at ``blocked`` for as long as it is waiting on the operator.

        Extracted because two different waits need identical status handling and the
        subtleties below were learned once each, expensively.

        Blocked *before* the thing being waited on is visible. Creating the gate or the
        question first leaves a window — short, but real — where the operator's inbox
        shows something pending on a job that still reads 'running', so the first thing
        anyone sees is already wrong. The status has to lead, not trail.
        """
        previous = await self.db.fetch_value("select status from jobs where id=?", (self.job_id,))
        # Guarded, because a stop may already have made the job terminal: the read above
        # is the last await before this write, and a cancellation requested in between is
        # delivered only afterwards. Blocking a stopped job would leave it reading
        # 'blocked' with no task, which nothing later corrects.
        placeholders = ",".join("?" * len(TERMINAL_JOB_STATUSES))
        blocked = await self.db.execute(
            "update jobs set status='blocked',updated_at=unixepoch('subsec')"
            f" where id=? and status not in ({placeholders})",
            (self.job_id, *sorted(TERMINAL_JOB_STATUSES)),
        )
        if not blocked:
            raise asyncio.CancelledError()
        await self.store.record(self.job_id, "status", {"status": "blocked"}, source="system")
        try:
            yield
        finally:
            # Back to whatever the job was, so a resolved wait does not leave the job
            # reading 'blocked' while it works. Runs even if raising the gate failed,
            # which is why that is inside the same block.
            #
            # `and status='blocked'` makes this undo *only its own write*, and makes it
            # atomic against whoever else touched the row. Without it a stop is silently
            # reversed: `stop_job` writes 'stopped', then cancels the task, and the
            # cancellation lands in the wait — so this `finally` runs afterwards and
            # wrote 'running' back over the terminal status. The job then read 'running'
            # forever with no task behind it, because the phases were already 'skipped'
            # and the startup sweep deliberately skips nothing else. Restoring a status
            # is only ever valid while the status is still the one we replaced.
            restore = previous if previous not in {"blocked", None} else "running"
            restored = await self.db.execute(
                "update jobs set status=?,updated_at=unixepoch('subsec')"
                " where id=? and status='blocked'",
                (restore, self.job_id),
            )
            # No event when the guard bit. The row did not change, and announcing
            # 'running' after 'stopped' would tell every live stream the job came back.
            if restored:
                await self.store.record(
                    self.job_id, "status", {"status": restore}, source="system"
                )

    async def _gate(self, invocation: tools.ToolInvocation, risk: Risk) -> tuple[str, bool]:
        """Raise a command-level approval gate and block on it.

        Unlike a phase gate this does not auto-approve in yolo mode. Yolo says "do not
        review my plan"; it was never a decision about ``sudo`` or ``git push``, and a
        guardrail hit is precisely the case where a human wants to look.
        """
        async with self._blocked():
            approval_id = await approvals.request(
                self.db,
                self.store,
                job_id=self.job_id,
                phase_id=self.phase_id,
                action=invocation.display[:400],
                detail=f"{risk.reason} — requested by {self.agent} during '{self.phase_name}'",
                agent=self.agent,
                risk=risk.level,
                kind="tool",
                # Linked in the same commit as the gate, so nothing can observe a
                # pending command gate whose row does not point back at it.
                tool_call_id=invocation.id,
            )
            await self._set_action(f"Awaiting approval: {invocation.display[:100]}")
            decision = await approvals.wait_for(self.db, approval_id, cancelled=self.cancel)
        if self.cancel.is_set():
            raise asyncio.CancelledError()
        return approval_id, decision.approved

    async def _ask_question(self, invocation: tools.ToolInvocation) -> tools.ToolOutcome:
        """Put a question to the operator and block until it resolves.

        Every resolution is a *result*, never an exception: an unanswered question is
        information the model can act on, and an agent that crashed its phase because
        nobody was at the keyboard would be a worse tool than one that never asked. The
        only thing that propagates is cancellation, because a stopped job must not look
        like a question that came back.
        """
        question = str(invocation.args.get("question") or "").strip()
        if not question:
            return tools.ToolOutcome(
                status="error",
                content=(
                    "No question was supplied, so nothing was asked. Call ask_operator "
                    "again with a `question` string."
                ),
            )

        # 0 means wait indefinitely, which `wait_for` expresses as None.
        timeout = float(settings.question_timeout) or None
        async with self._blocked():
            question_id, options = await questions.request(
                self.db,
                self.store,
                job_id=self.job_id,
                phase_id=self.phase_id,
                agent=self.agent,
                question=question,
                detail=str(invocation.args.get("detail") or "").strip() or None,
                options=invocation.args.get("options"),
                allow_free_text=bool(invocation.args.get("allow_free_text", True)),
            )
            await self._set_action(f"Awaiting an answer: {question[:100]}")
            answer = await questions.wait_for(
                self.db,
                self.store,
                question_id,
                job_id=self.job_id,
                cancelled=self.cancel,
                timeout=timeout,
            )
        if self.cancel.is_set():
            raise asyncio.CancelledError()

        if answer.answered:
            # The chosen option is named as well as quoted. A model that offered
            # "Rewrite it" and "Patch it" reasons better about its own label than about
            # a sentence, and the label alone is ambiguous once the operator has also
            # typed something.
            picked = next(
                (entry["label"] for entry in options if entry["value"] == answer.chosen), None
            )
            body = f'The operator answered: "{answer.text}"'
            if picked is not None and picked != answer.text:
                body = f'The operator chose "{picked}" and added: "{answer.text}"'
            return tools.ToolOutcome(
                status="ok",
                content=f"{body}\n\nAct on that answer now; do not ask again.",
                stdout=answer.text,
            )

        return tools.ToolOutcome(
            status="timeout" if answer.status == "timeout" else "cancelled",
            content=answer.text,
            stderr=answer.text,
        )

    # ------------------------------------------------------------------ recording

    async def _record_row(
        self,
        invocation: tools.ToolInvocation,
        outcome: tools.ToolOutcome,
        *,
        approval_id: str | None,
    ) -> None:
        """Insert an already-resolved row, for calls that never reached execution."""
        await self.db.execute(
            "insert into tool_calls(id,job_id,phase_id,turn,agent,tool,args,status,stderr,"
            "sandbox,approval_id,created_at,finished_at)"
            " values(?,?,?,?,?,?,?,?,?,?,?,unixepoch('subsec'),unixepoch('subsec'))",
            (
                invocation.id,
                self.job_id,
                self.phase_id,
                invocation.turn,
                self.agent,
                invocation.tool,
                json.dumps(invocation.args)[:20000],
                outcome.status,
                outcome.content[: settings.tool_output_limit],
                self.sandbox.kind,
                approval_id,
            ),
        )

    async def _finalise_row(
        self, invocation: tools.ToolInvocation, outcome: tools.ToolOutcome, approval_id: str | None
    ) -> None:
        await self.db.execute(
            "update tool_calls set status=?,exit_code=?,stdout=?,stderr=?,truncated=?,"
            "duration_ms=?,approval_id=coalesce(?,approval_id),finished_at=unixepoch('subsec')"
            " where id=?",
            (
                outcome.status,
                outcome.exit_code,
                outcome.stdout[: settings.tool_output_limit + ROW_SLICE_MARGIN],
                outcome.stderr[: settings.tool_output_limit + ROW_SLICE_MARGIN] or (
                    outcome.content[: settings.tool_output_limit] if not outcome.ok else ""
                ),
                int(outcome.truncated),
                outcome.duration_ms,
                approval_id,
                invocation.id,
            ),
        )

    async def _emit(
        self, invocation: tools.ToolInvocation, outcome: tools.ToolOutcome, *, risk: Risk | None
    ) -> None:
        await self.store.record(
            self.job_id,
            "tool_call",
            {
                "tool_call_id": invocation.id,
                "phase_id": self.phase_id,
                "phase": self.phase_name,
                "turn": invocation.turn,
                "tool": invocation.tool,
                "display": invocation.display,
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "duration_ms": outcome.duration_ms,
                "sandbox": self.sandbox.kind,
                "risk": risk.reason if risk else None,
            },
            source=self.agent,
        )

    async def _set_action(self, action: str) -> None:
        """Keep the Agents view showing what is happening right now.

        A three-minute ``pytest`` is otherwise indistinguishable from a wedged phase.
        """
        await self.db.execute(
            "update job_agents set current_action=?,updated_at=unixepoch('subsec')"
            " where job_id=? and agent=?",
            (action, self.job_id, self.agent),
        )
        await self.store.record(
            self.job_id, "agent_state", {"status": "active", "current_action": action}, source=self.agent
        )

    async def _provider_wait_notice(self, attempt: int, delay: float, exc: ProviderError) -> None:
        """Tell the operator a call is waiting out a provider blip, not failing.

        Fired by ``complete_with_retry`` before each wait. The timeline notice is the
        durable record; the action line keeps the Agents view honest so a wait reads
        as a wait rather than a hang.
        """
        await self.store.record(
            self.job_id,
            "notice",
            {
                "message": (
                    f"Provider unavailable ({exc}); waiting {delay:.0f}s before retry "
                    f"{attempt + 1} of '{self.phase_name}'."
                ),
                "phase_id": self.phase_id,
                "provider_retry": attempt,
            },
            source="system",
        )
        await self._set_action(f"Provider unavailable — retrying in {delay:.0f}s")


async def interrupted_notice(database: Database, phase_id: int) -> str:
    """Tell a re-running phase the truth about what its last attempt was doing.

    This is the one place the engine's "resume, don't replay" rule cannot be made
    perfect: a command's effects are in the filesystem, not the database. So instead
    of pretending the phase starts clean, name what was in flight and let the model
    check before repeating it. Silently re-running ``git push`` is the failure this
    exists to prevent.
    """
    rows = await database.fetch_all(
        "select tool,args,status from tool_calls"
        " where phase_id=? and status in ('interrupted','cancelled') order by id",
        (phase_id,),
    )
    if not rows:
        return ""

    lines = []
    for row in rows:
        try:
            args = json.loads(row["args"])
        except (json.JSONDecodeError, TypeError):
            args = {}
        display = tools.ToolInvocation(id="", tool=row["tool"], args=args).display
        if row["tool"] == "ask_operator":
            # A question has no side effects, so the command wording below would be a
            # lie in the one direction that matters: it would tell the model the
            # workspace might already reflect something that never touched it. What it
            # does need to know is that asking again is allowed.
            lines.append(
                f"- you asked the operator `{display}` and the question was closed "
                "unanswered; ask again if you still need it"
            )
            continue
        state = (
            "was already running when the service restarted, so the workspace may "
            "already reflect it"
            if row["status"] == "interrupted"
            else "was waiting for approval and never ran"
        )
        lines.append(f"- `{display}` — {state}")

    return (
        "\n\nIMPORTANT — a previous attempt at this phase was interrupted. These calls "
        "did not complete:\n" + "\n".join(lines) + "\nCheck the current state before "
        "repeating any of them, especially anything that is not safe to run twice.\n"
    )


async def sweep_interrupted(database: Database) -> int:
    """Resolve rows left mid-flight by a crash. Called once at startup.

    A ``running`` row means the process died with a child alive; the child is gone with
    it, so the row becomes ``interrupted`` — not ``error``, because nobody knows how far
    it got. A ``pending`` row never ran at all, and its gate (if any) is meaningless now
    that the phase will re-run from turn 1, so it is cancelled rather than left to
    confuse the operator's inbox.

    This is the *hard* path, where the service died before it could record anything. A
    graceful ``systemctl restart`` goes the other way round: systemd SIGTERMs the whole
    cgroup, so the child dies first and the still-live service sees the signal and
    commits ``interrupted`` itself (see ``Completed.interrupted``). A clean restart
    therefore usually leaves nothing here to sweep — which is exactly why this is not the
    only place that has to get the distinction right.
    """
    running = await database.execute(
        "update tool_calls set status='interrupted',finished_at=unixepoch('subsec')"
        " where status='running'"
        " and job_id in (select id from jobs where status not in ('complete','error','stopped'))"
    )
    pending = await database.execute(
        "update tool_calls set status='cancelled',finished_at=unixepoch('subsec')"
        " where status='pending'"
        " and job_id in (select id from jobs where status not in ('complete','error','stopped'))"
    )
    # A gate for a call that will never run must not sit in the inbox.
    await database.execute(
        "update approvals set status='rejected',decided_at=unixepoch('subsec'),"
        "decision_note='cancelled by restart; the phase will re-run'"
        " where kind='tool' and status='pending'"
        " and job_id in (select id from jobs where status not in ('complete','error','stopped'))"
    )
    # And neither must a question. This is the one place a question is *less* durable
    # than the row that records it: the answer would be delivered into a tool-loop
    # conversation that died with the process, so there is nothing left to unblock.
    # The phase re-runs from turn 1 and the agent asks again if it still needs to —
    # which is honest, where an answered question nobody consumed would not be.
    await database.execute(
        "update questions set status='cancelled',"
        "answer='The service restarted before this was answered; the phase re-ran.',"
        "answered_at=unixepoch('subsec')"
        " where status='pending'"
        " and job_id in (select id from jobs where status not in ('complete','error','stopped'))"
    )
    if running or pending:
        log.info("resolved interrupted tool calls", extra={"running": running, "pending": pending})
    return int(running) + int(pending)
