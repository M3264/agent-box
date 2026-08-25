"""The job engine: a durable phase state machine.

This replaces ``runtime.py``'s fixed relay chain, in which five hardcoded roles
each got one LLM call and the whole chain restarted from the beginning whenever
the process did. Job ``cb183d475f0a`` in the archived database shows the result:
message phases ``[1,1,2,3,1,2,3,4,5]`` appended to a single job across four
restarts.

The rule that fixes it: **a phase's output and terminal status are committed
together, before the engine advances.** Everything else follows from that.

- Resume, don't replay. On start the engine picks the first phase that is not in
  a terminal state and continues there, reading earlier phases' persisted output
  from the database instead of re-deriving it.
- Planning is itself a phase (``seq 0``, ``kind='plan'``), so a crash during
  planning resumes correctly too.
- Pause waits on an ``asyncio.Event``, not a ``sleep(0.5)`` poll. The v1 loop kept
  one job spinning for 40.6 hours and blocked graceful shutdown until SIGKILL.
- Stop cancels the task, so an in-flight provider call is actually interrupted
  rather than running to completion behind a flag nobody checks until the next
  phase boundary.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import Database
from app.events import EventStore
from app.logging_setup import get_logger
from app.models import TERMINAL_JOB_STATUSES
from app.orchestrator import agentloop, approvals, sandbox as sandbox_mod
from app.orchestrator.providers import (
    Completion,
    Message,
    Provider,
    ProviderError,
    build_provider,
    load_profile,
)
from app.orchestrator.roles import Role, Team, load_team

log = get_logger("agent_hub.engine")

#: A phase in one of these states will never run again.
TERMINAL_PHASE_STATUSES = frozenset({"complete", "failed", "skipped"})

MAX_PLANNED_PHASES = 8
#: Per-phase output is truncated when composing context so a long job cannot grow
#: an unbounded prompt.
CONTEXT_CHARS_PER_PHASE = 4000
PAUSE_RECHECK_SECONDS = 30.0


class JobFailed(RuntimeError):
    """A phase failed in a way that ends the job."""


@dataclass(slots=True)
class PhaseRow:
    id: int
    seq: int
    kind: str
    name: str
    owner: str
    acceptance: str | None
    status: str
    requires_approval: bool
    output: str | None
    attempts: int

    @classmethod
    def from_row(cls, row: Any) -> PhaseRow:
        return cls(
            id=int(row["id"]),
            seq=int(row["seq"]),
            kind=row["kind"],
            name=row["name"],
            owner=row["owner"],
            acceptance=row["acceptance"],
            status=row["status"],
            requires_approval=bool(row["requires_approval"]),
            output=row["output"],
            attempts=int(row["attempts"]),
        )


class JobEngine:
    """Owns the lifecycle of every running job in this process."""

    def __init__(self, database: Database, store: EventStore) -> None:
        self.db = database
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._resumes: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ control

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    @property
    def active_count(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done())

    def start(self, job_id: str) -> asyncio.Task[None]:
        """Launch (or re-attach to) the background task driving a job."""
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return existing

        self._cancels[job_id] = asyncio.Event()
        self._resumes.setdefault(job_id, asyncio.Event())
        task = asyncio.create_task(self._run(job_id), name=f"job:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: self._on_task_done(jid, t))
        return task

    def _on_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        self._cancels.pop(job_id, None)
        self._resumes.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("job task crashed", extra={"job_id": job_id}, exc_info=task.exception())

    async def stop(self, job_id: str) -> None:
        """Interrupt a job now, including any in-flight provider call."""
        cancel = self._cancels.get(job_id)
        if cancel is not None:
            cancel.set()
        # Wake a paused job so it observes the cancellation instead of sleeping.
        resume = self._resumes.get(job_id)
        if resume is not None:
            resume.set()
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

    def notify_resumed(self, job_id: str) -> None:
        """Wake a job blocked at the pause gate. Called by the resume endpoint."""
        resume = self._resumes.get(job_id)
        if resume is not None:
            resume.set()

    async def shutdown(self, grace: float | None = None) -> None:
        """Cancel all in-flight jobs so the process can exit promptly.

        v1 had no shutdown path: uvicorn waited on background tasks that could
        never finish, and systemd escalated to SIGKILL after 90 seconds. Jobs are
        parked back to 'queued' so ``recover()`` continues them on next start.
        """
        limit = grace if grace is not None else settings.shutdown_grace
        job_ids = [job_id for job_id, task in self._tasks.items() if not task.done()]
        if not job_ids:
            return

        log.info("cancelling in-flight jobs for shutdown", extra={"count": len(job_ids), "grace": limit})
        for job_id in job_ids:
            cancel = self._cancels.get(job_id)
            if cancel is not None:
                cancel.set()
            resume = self._resumes.get(job_id)
            if resume is not None:
                resume.set()
            task = self._tasks.get(job_id)
            if task is not None:
                task.cancel()

        tasks = [task for task in (self._tasks.get(j) for j in job_ids) if task is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=limit)

        # Park anything still mid-flight so it is picked up again on next boot.
        await self.db.execute(
            "update jobs set status='queued',updated_at=unixepoch('subsec')"
            " where status in ('running','planning','blocked')"
        )
        await self.db.execute(
            "update phases set status='pending' where status='active'"
        )

    async def recover(self) -> list[str]:
        """Re-attach to jobs that were mid-flight when the process last stopped.

        Safe because phase state is durable: each job continues at its first
        non-terminal phase rather than starting over.
        """
        # Before anything restarts, resolve the tool calls that were in flight. A
        # command's side effects are not in the database, so the only honest thing to
        # do is mark them interrupted and tell the re-running phase about them.
        await agentloop.sweep_interrupted(self.db)

        rows = await self.db.fetch_all(
            "select id from jobs where status not in ('complete','error','stopped') order by created_at"
        )
        job_ids = [row["id"] for row in rows]
        for job_id in job_ids:
            self.start(job_id)
        if job_ids:
            log.info("recovered interrupted jobs", extra={"count": len(job_ids), "job_ids": job_ids})
        return job_ids

    # -------------------------------------------------------------- persistence

    async def _set_job_status(self, job_id: str, status: str, **columns: Any) -> None:
        """Write the job's status, never resurrecting a job an operator has stopped.

        A non-terminal write carries a guard, because every one of them sits after a
        check that has already gone stale. ``_run`` reads the status at the top and
        writes 'running' several awaits later; the phase gate checks ``cancel`` and then
        writes 'running'. A ``stop`` landing in either window writes 'stopped' and
        cancels the task — but the cancellation is only delivered at the *next* await, so
        the write below can still land on top of it, leaving a job that reads 'running'
        with nothing running. Terminal writes are unguarded: 'complete', 'error' and
        'stopped' are allowed to overwrite each other, and the first one wins the phase
        loop anyway.
        """
        assignments = ["status=?", "updated_at=unixepoch('subsec')"]
        params: list[Any] = [status]
        for name, value in columns.items():
            assignments.append(f"{name}=?")
            params.append(value)
        params.append(job_id)
        guard = ""
        if status not in TERMINAL_JOB_STATUSES:
            placeholders = ",".join("?" * len(TERMINAL_JOB_STATUSES))
            guard = f" and status not in ({placeholders})"
            params.extend(sorted(TERMINAL_JOB_STATUSES))
        changed = await self.db.execute(
            f"update jobs set {','.join(assignments)} where id=?{guard}", params
        )
        # Nothing changed means the guard bit, so there is nothing to announce either:
        # a 'running' event after 'stopped' would tell every live stream the job is back.
        if not changed:
            return
        await self.store.record(job_id, "status", {"status": status}, source="system")

    async def _set_agent(
        self, job_id: str, agent: str | None, status: str, action: str | None = None
    ) -> None:
        if not agent:
            return
        await self.db.execute(
            "insert into job_agents(job_id,agent,status,current_action,updated_at)"
            " values(?,?,?,?,unixepoch('subsec'))"
            " on conflict(job_id,agent) do update set"
            "  status=excluded.status,current_action=excluded.current_action,"
            "  updated_at=excluded.updated_at",
            (job_id, agent, status, action),
        )
        await self.store.record(
            job_id, "agent_state", {"status": status, "current_action": action}, source=agent
        )

    async def _emit_phase(self, job_id: str, phase: PhaseRow, status: str) -> None:
        await self.store.record(
            job_id,
            "phase",
            {
                "phase_id": phase.id,
                "seq": phase.seq,
                "kind": phase.kind,
                "name": phase.name,
                "owner": phase.owner,
                "status": status,
                "acceptance": phase.acceptance,
            },
            source=phase.owner,
        )

    async def _next_phase(self, job_id: str) -> PhaseRow | None:
        row = await self.db.fetch_one(
            "select * from phases where job_id=? and status not in ('complete','failed','skipped')"
            " order by seq limit 1",
            (job_id,),
        )
        return PhaseRow.from_row(row) if row else None

    async def _prior_outputs(self, job_id: str, before_seq: int) -> list[tuple[str, str, str]]:
        rows = await self.db.fetch_all(
            "select name,owner,output from phases"
            " where job_id=? and seq<? and status='complete' and output is not null"
            " order by seq",
            (job_id, before_seq),
        )
        return [(row["name"], row["owner"], row["output"]) for row in rows]

    # -------------------------------------------------------------------- prompt

    async def _drain_operator_messages(self, job_id: str) -> list[str]:
        """Consume operator guidance so it can be injected into the next prompt.

        v1 stored these and never read them, so the "lead conversation" in PLAN.md
        §1 went nowhere.
        """
        rows = await self.db.fetch_all(
            "select id,content from job_messages"
            " where job_id=? and role='operator' and consumed_at is null order by id",
            (job_id,),
        )
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" * len(ids))
        await self.db.execute(
            f"update job_messages set consumed_at=unixepoch('subsec') where id in ({placeholders})",
            ids,
        )
        return [row["content"] for row in rows]

    @staticmethod
    def _context_block(outputs: list[tuple[str, str, str]]) -> str:
        if not outputs:
            return "(no prior phases)"
        parts = []
        for name, owner, text in outputs:
            body = text.strip()
            if len(body) > CONTEXT_CHARS_PER_PHASE:
                body = body[:CONTEXT_CHARS_PER_PHASE] + "\n[...truncated...]"
            parts.append(f"### {name} (by {owner})\n{body}")
        return "\n\n".join(parts)

    # --------------------------------------------------------------------- runner

    async def _run(self, job_id: str) -> None:
        cancel = self._cancels.setdefault(job_id, asyncio.Event())
        try:
            job = await self.db.fetch_one("select * from jobs where id=?", (job_id,))
            if job is None:
                log.warning("job vanished before it could run", extra={"job_id": job_id})
                return
            if job["status"] in TERMINAL_JOB_STATUSES:
                return

            team = await load_team(self.db, int(job["team_id"]))
            profile = await load_profile(self.db, job["provider_id"])
            # Resolved once, here, and deliberately before the first phase runs. If the
            # job asked for a backend this host cannot provide, it must fail with that
            # message rather than run a single command somewhere the operator did not
            # choose — build_sandbox raises instead of substituting.
            box = await self._resolve_sandbox(job)

            await self._set_job_status(job_id, "running")
            for role in team.roles:
                if not await self.db.exists(
                    "select 1 from job_agents where job_id=? and agent=?", (job_id, role.id)
                ):
                    await self._set_agent(job_id, role.id, "queued", "Queued")

            async with build_provider(profile) as provider:  # type: ignore[union-attr]
                previous_owner: str | None = None
                while True:
                    await self._await_unpaused(job_id, cancel)
                    if cancel.is_set():
                        raise asyncio.CancelledError()

                    phase = await self._next_phase(job_id)
                    if phase is None:
                        break

                    if previous_owner and previous_owner != phase.owner:
                        await self.store.record(
                            job_id,
                            "handoff",
                            {
                                "from": previous_owner,
                                "to": phase.owner,
                                "reason": f"Passing the work to {phase.owner} for '{phase.name}'.",
                            },
                            source=previous_owner,
                        )
                    await self._run_phase(job, team, provider, phase, cancel, box)
                    previous_owner = phase.owner

            await self._finish(job_id, team)

        except asyncio.CancelledError:
            await self._park_for_recovery(job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            log.error("job failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
            await self.db.execute(
                "update jobs set status='error',error=?,updated_at=unixepoch('subsec') where id=?",
                (str(exc), job_id),
            )
            await self.store.record(job_id, "error", {"error": str(exc)}, source="system")
            # Close out the phases this job will now never reach, the same way the
            # stop endpoint does. A terminal job holding non-terminal phases is the
            # exact symptom this redesign set out to remove: v1's plans sat at
            # 'queued' forever, so a dead job looked identical to a stalled one.
            # The phase that actually failed is already 'failed' by this point.
            await self.db.execute(
                "update phases set status='skipped',error=?,finished_at=unixepoch('subsec')"
                " where job_id=? and status in ('pending','active','blocked_on_approval')",
                (f"job failed: {exc}", job_id),
            )
            await self.db.execute(
                "update job_agents set status='error',current_action='Error',"
                "updated_at=unixepoch('subsec') where job_id=? and status='active'",
                (job_id,),
            )
            await self.store.record(job_id, "status", {"status": "error"}, source="system")

    async def _resolve_sandbox(self, job: Any) -> sandbox_mod._Sandbox | None:
        """Decide where this job's commands run, and record it.

        Returns None when tools are switched off, which turns every phase back into a
        single text-only call. The backend is written onto the row the first time it is
        resolved so the audit trail says what actually ran the commands, not what the
        default happened to be when someone later opened the Commands view.
        """
        if not settings.tools_enabled:
            return None

        job_id = job["id"]
        workspace = Path(job["workspace"]) if job["workspace"] else settings.workspace_root / job_id
        workspace.mkdir(parents=True, exist_ok=True)

        chosen = job["sandbox"] or sandbox_mod.default_kind()
        box = sandbox_mod.build_sandbox(chosen, workspace)
        if job["sandbox"] != chosen or job["workspace"] != str(workspace):
            await self.db.execute(
                "update jobs set sandbox=?,workspace=? where id=?",
                (chosen, str(workspace), job_id),
            )
        return box

    async def _park_for_recovery(self, job_id: str) -> None:
        """Leave an interrupted job resumable.

        Two things cancel a job: an operator stop, which sets ``status='stopped'``
        before cancelling, and a process shutdown, which does not. So a job that is
        still non-terminal here was interrupted by shutdown, and its half-finished
        phase is reset to pending so ``recover()`` re-runs exactly that phase —
        never the ones already committed.
        """
        current = await self.db.fetch_value("select status from jobs where id=?", (job_id,))
        if current in TERMINAL_JOB_STATUSES:
            return
        await self.db.execute(
            "update phases set status='pending' where job_id=? and status='active'", (job_id,)
        )

    async def _await_unpaused(self, job_id: str, cancel: asyncio.Event) -> None:
        """Block while the job is paused, without polling.

        The database flag is the durable source of truth; the event is the wake-up.
        The periodic recheck is a safety net for a missed notification (for example
        a pause toggled by another process), not the primary mechanism.
        """
        announced = False
        while True:
            paused = await self.db.fetch_value("select paused from jobs where id=?", (job_id,))
            if not paused:
                if announced:
                    await self.store.record(job_id, "status", {"status": "running"}, source="system")
                return
            if cancel.is_set():
                return
            if not announced:
                announced = True
                log.info("job paused; waiting for resume", extra={"job_id": job_id})

            resume = self._resumes.setdefault(job_id, asyncio.Event())
            resume.clear()
            waiters = [
                asyncio.ensure_future(resume.wait()),
                asyncio.ensure_future(cancel.wait()),
            ]
            try:
                await asyncio.wait(
                    waiters, timeout=PAUSE_RECHECK_SECONDS, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()

    # ---------------------------------------------------------------- phase exec

    async def _run_phase(
        self,
        job: Any,
        team: Team,
        provider: Provider,
        phase: PhaseRow,
        cancel: asyncio.Event,
        box: sandbox_mod._Sandbox | None = None,
    ) -> None:
        job_id = job["id"]
        role = team.get(phase.owner) or team.orchestrator

        if phase.requires_approval and not await self._gate(job, phase, cancel):
            return  # rejected by the operator; phase is now terminal

        await self.db.execute(
            "update phases set status='active',attempts=attempts+1,"
            "started_at=coalesce(started_at,unixepoch('subsec')) where id=?",
            (phase.id,),
        )
        await self._emit_phase(job_id, phase, "active")
        await self._set_agent(job_id, role.id, "active", phase.name)

        guidance = await self._drain_operator_messages(job_id)
        if guidance:
            await self.store.record(
                job_id, "guidance", {"messages": guidance, "phase_id": phase.id}, source="operator"
            )

        try:
            if phase.kind == "plan":
                output = await self._do_plan(job, team, provider, phase, guidance)
            elif phase.kind == "synthesis":
                output = await self._do_synthesis(job, team, provider, phase, guidance)
            else:
                output = await self._do_work(job, team, provider, phase, role, guidance, box, cancel)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            await self._fail_phase(job_id, phase, str(exc))
            raise JobFailed(f"phase '{phase.name}' failed: {exc}") from exc

        # Output and terminal status commit together: this single write is what
        # makes a restart resume instead of replay.
        await self.db.execute(
            "update phases set status='complete',output=?,finished_at=unixepoch('subsec') where id=?",
            (output, phase.id),
        )
        await self._emit_phase(job_id, phase, "complete")
        await self._set_agent(job_id, role.id, "waiting", "Waiting for next handoff")

    async def _fail_phase(self, job_id: str, phase: PhaseRow, error: str) -> None:
        await self.db.execute(
            "update phases set status='failed',error=?,finished_at=unixepoch('subsec') where id=?",
            (error, phase.id),
        )
        await self._emit_phase(job_id, phase, "failed")
        await self._set_agent(job_id, phase.owner, "error", "Error")

    async def _gate(self, job: Any, phase: PhaseRow, cancel: asyncio.Event) -> bool:
        """Block a phase on operator approval. Returns True if it may proceed.

        Re-attaches to an existing gate rather than creating a second one. That
        distinction matters across a restart: a phase left in
        ``blocked_on_approval`` must reuse its gate, and one whose gate was decided
        while the process was down must honour that decision rather than asking
        the operator to approve the same work twice.
        """
        job_id = job["id"]
        auto = job["mode"] == "yolo"

        existing = await approvals.latest_for_phase(self.db, phase.id)
        if existing is not None and existing["status"] != "pending":
            if existing["status"] == "approved":
                return True
            await self._skip_phase(job_id, phase, existing.get("decision_note"))
            return False

        # A phase already sitting in 'blocked_on_approval' with a pending gate is one
        # this process is *re-attaching* to after a restart, not one that is entering
        # the gate. Announcing it again put the same gate in the Timeline twice and
        # claimed the phase blocked a second time, which never happened — the row was
        # never rewritten and the operator was never asked twice. The job's own
        # running → blocked pair below stays: `_run` really did set the column to
        # 'running' on its way in, and the log should say so.
        reattaching = existing is not None and phase.status == "blocked_on_approval"

        # Block the phase and the job before the gate exists, in one commit.
        #
        # Written the other way round, a pending approval can be attached to a phase
        # still reading 'pending' — the inbox shows a gate on work that looks like it
        # has not been reached. Written separately, a snapshot can show a phase
        # 'blocked_on_approval' on a 'running' job, which anything waiting on the
        # gate reads as "still working". Crashing in *this* window is the harmless
        # case: the phase has not run, and `latest_for_phase` finds no gate on
        # recovery, so one is raised then.
        async with self.db.transaction() as conn:
            if not auto:
                # Guarded, and first, so a stop that has already made the job terminal
                # ends this phase instead of pulling the job back to 'blocked'. The
                # rollback takes the phase write with it, and CancelledError is simply
                # the truth: this phase is not going to run. The `cancel` check above
                # cannot cover it — `conn.execute` is an await, so a cancellation
                # requested in between is delivered only after this write lands.
                placeholders = ",".join("?" * len(TERMINAL_JOB_STATUSES))
                async with conn.execute(
                    "update jobs set status='blocked',updated_at=unixepoch('subsec')"
                    f" where id=? and status not in ({placeholders})",
                    (job_id, *sorted(TERMINAL_JOB_STATUSES)),
                ) as cursor:
                    if cursor.rowcount == 0:
                        raise asyncio.CancelledError()
            await conn.execute(
                "update phases set status='blocked_on_approval' where id=?", (phase.id,)
            )
        if not reattaching:
            await self._emit_phase(job_id, phase, "blocked_on_approval")
        if not auto:
            await self.store.record(job_id, "status", {"status": "blocked"}, source="system")
            if not reattaching:
                # The agent row already reads 'blocked' on this very phase; rewriting
                # it would emit an agent_state event that changes nothing.
                await self._set_agent(
                    job_id, phase.owner, "blocked", f"Awaiting approval: {phase.name}"
                )

        approval_id = existing["id"] if existing else await approvals.request(
            self.db,
            self.store,
            job_id=job_id,
            phase_id=phase.id,
            action=phase.name,
            detail=phase.acceptance,
            agent=phase.owner,
            auto_approve=auto,
        )

        decision = await approvals.wait_for(self.db, approval_id, cancelled=cancel)

        if cancel.is_set():
            raise asyncio.CancelledError()

        if not decision.approved:
            await self._skip_phase(job_id, phase, decision.note)
            await self._set_job_status(job_id, "running")
            return False

        if not auto:
            await self._set_job_status(job_id, "running")
        return True

    async def _skip_phase(self, job_id: str, phase: PhaseRow, note: str | None) -> None:
        await self.db.execute(
            "update phases set status='skipped',error=?,finished_at=unixepoch('subsec') where id=?",
            (f"rejected by operator: {note or 'no reason given'}", phase.id),
        )
        await self._emit_phase(job_id, phase, "skipped")
        await self._set_agent(job_id, phase.owner, "waiting", "Phase rejected")

    # ------------------------------------------------------------------ phase kinds

    async def _ask(
        self, provider: Provider, system: str, prompt: str, temperature: float = 0.2
    ) -> Completion:
        return await provider.complete(
            system=system, messages=[Message(role="user", content=prompt)], temperature=temperature
        )

    async def _do_plan(
        self, job: Any, team: Team, provider: Provider, phase: PhaseRow, guidance: list[str]
    ) -> str:
        """Manager authors the phases. Falls back to one phase per specialist."""
        job_id = job["id"]
        await self._set_job_status(job_id, "planning")

        specialists = team.specialists
        roster = "\n".join(f"- {role.id} ({role.name}): {role.instructions}" for role in specialists)
        guidance_block = (
            "\n\nOperator guidance you must incorporate:\n"
            + "\n".join(f"- {item}" for item in guidance)
            if guidance
            else ""
        )
        prompt = (
            f"Task:\n{job['task']}\n\n"
            f"Available specialists:\n{roster}{guidance_block}\n\n"
            f"Produce an ordered plan of between 1 and {MAX_PLANNED_PHASES} phases. "
            "Each phase needs exactly one owner from the specialist ids above, and "
            "acceptance criteria that can be objectively checked. Set requires_approval "
            "to true only for phases with real external consequences (publishing, "
            "sending, deleting, spending).\n\n"
            'Reply with JSON only, no prose:\n'
            '{"phases":[{"name":"...","owner":"<specialist id>",'
            '"acceptance":"...","requires_approval":false}],"notes":"..."}'
        )

        completion = await self._ask(
            provider, f"You are {team.orchestrator.name}. {team.orchestrator.instructions}", prompt
        )

        notes: str | None = None
        try:
            plan = completion.json()
            if isinstance(plan, dict):
                notes = str(plan.get("notes") or "") or None
                raw_phases = plan.get("phases")
            else:
                raw_phases = plan
            planned = self._validate_plan(raw_phases, specialists)
        except (ProviderError, ValueError, TypeError, AttributeError) as exc:
            log.warning(
                "manager plan was unusable; falling back to one phase per specialist",
                extra={"job_id": job_id, "error": str(exc)},
            )
            planned = [
                {
                    "name": f"{role.name} phase",
                    "owner": role.id,
                    "acceptance": role.instructions,
                    "requires_approval": False,
                }
                for role in specialists
            ]
            await self.store.record(
                job_id,
                "notice",
                {"message": "Manager plan could not be parsed; used the default phase per specialist."},
                source="system",
            )

        await self._insert_phases(job_id, planned, team)
        await self.store.record(
            job_id,
            "plan",
            {"phases": planned, "notes": notes},
            source=team.orchestrator.id,
        )
        # Back to 'running' now the plan exists, so the status reflects the work
        # being done rather than staying 'planning' for the rest of the job.
        await self._set_job_status(job_id, "running")
        summary = "\n".join(
            f"{index}. {item['name']} — {item['owner']} (acceptance: {item['acceptance']})"
            for index, item in enumerate(planned, start=1)
        )
        return f"Plan:\n{summary}"

    @staticmethod
    def _validate_plan(raw: Any, specialists: tuple[Role, ...]) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValueError("plan contained no phases")
        valid_ids = {role.id for role in specialists}
        default_owner = specialists[0].id
        cleaned: list[dict[str, Any]] = []
        for entry in raw[:MAX_PLANNED_PHASES]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            owner = str(entry.get("owner") or "").strip()
            cleaned.append(
                {
                    "name": name[:200],
                    "owner": owner if owner in valid_ids else default_owner,
                    "acceptance": str(entry.get("acceptance") or "").strip()[:2000] or None,
                    "requires_approval": bool(entry.get("requires_approval")),
                }
            )
        if not cleaned:
            raise ValueError("plan contained no usable phases")
        return cleaned

    async def _insert_phases(self, job_id: str, planned: list[dict[str, Any]], team: Team) -> None:
        """Write the planned phases plus a final synthesis phase.

        Synthesis is a phase like any other so that it, too, survives a restart.
        """
        async with self.db.transaction() as conn:
            async with conn.execute(
                "select coalesce(max(seq),0) from phases where job_id=?", (job_id,)
            ) as cursor:
                start = int((await cursor.fetchone())[0])
            previous: str | None = None
            for offset, item in enumerate(planned, start=1):
                await conn.execute(
                    "insert into phases(job_id,seq,kind,name,owner,acceptance,depends_on,status,"
                    "requires_approval,created_at) values(?,?,?,?,?,?,?,'pending',?,unixepoch('subsec'))",
                    (
                        job_id,
                        start + offset,
                        "work",
                        item["name"],
                        item["owner"],
                        item["acceptance"],
                        json.dumps([previous] if previous else []),
                        int(item["requires_approval"]),
                    ),
                )
                previous = item["owner"]

            await conn.execute(
                "insert into phases(job_id,seq,kind,name,owner,acceptance,depends_on,status,"
                "requires_approval,created_at) values(?,?,?,?,?,?,?,'pending',0,unixepoch('subsec'))",
                (
                    job_id,
                    start + len(planned) + 1,
                    "synthesis",
                    "Synthesise the result",
                    team.orchestrator.id,
                    "A single coherent answer covering decisions, caveats and next actions.",
                    json.dumps([previous] if previous else []),
                ),
            )

    async def _do_work(
        self,
        job: Any,
        team: Team,
        provider: Provider,
        phase: PhaseRow,
        role: Role,
        guidance: list[str],
        box: sandbox_mod._Sandbox | None = None,
        cancel: asyncio.Event | None = None,
    ) -> str:
        job_id = job["id"]
        context = self._context_block(await self._prior_outputs(job_id, phase.seq))
        guidance_block = (
            "\n\nOperator guidance (authoritative, overrides the plan where they conflict):\n"
            + "\n".join(f"- {item}" for item in guidance)
            if guidance
            else ""
        )
        # A phase that is re-running after a crash is told what its last attempt had in
        # flight. This is the seam where "resume, don't replay" stops being enough:
        # commands changed the filesystem, and the model has to know that before it
        # repeats one.
        notice = await agentloop.interrupted_notice(self.db, phase.id)
        prompt = (
            f"Overall task:\n{job['task']}\n\n"
            f"Your phase: {phase.name}\n"
            f"Acceptance criteria: {phase.acceptance or 'use your judgement'}\n\n"
            f"Work completed so far:\n{context}{guidance_block}{notice}\n\n"
            "Do your phase now. Be concrete and specific, and hand off work the next "
            "specialist can build on directly."
        )
        system = f"You are {role.name}. {role.instructions}"

        if box is None:
            completion = await self._ask(provider, system, prompt)
            text = completion.text
        else:
            loop = agentloop.ToolLoop(
                db=self.db,
                store=self.store,
                provider=provider,
                sandbox=box,
                workspace=box.workspace,
                job_id=job_id,
                phase_id=phase.id,
                phase_name=phase.name,
                agent=role.id,
                cancel=cancel if cancel is not None else asyncio.Event(),
            )
            result = await loop.run(
                system=agentloop.build_system_prompt(
                    system, workspace=box.workspace, sandbox_kind=box.kind
                ),
                prompt=prompt,
            )
            text = result.text
            log.info(
                "phase tool loop finished",
                extra={
                    "job_id": job_id,
                    "phase_id": phase.id,
                    "turns": result.turns,
                    "commands": result.calls,
                    "exhausted": result.exhausted,
                },
            )

        await self.store.record(
            job_id,
            "message",
            {"content": text, "phase_id": phase.id, "seq": phase.seq, "phase": phase.name},
            source=role.id,
        )
        return text

    async def _do_synthesis(
        self, job: Any, team: Team, provider: Provider, phase: PhaseRow, guidance: list[str]
    ) -> str:
        job_id = job["id"]
        context = self._context_block(await self._prior_outputs(job_id, phase.seq))
        guidance_block = (
            "\n\nOperator guidance:\n" + "\n".join(f"- {item}" for item in guidance)
            if guidance
            else ""
        )
        prompt = (
            f"Task:\n{job['task']}\n\n"
            f"Team output:\n{context}{guidance_block}\n\n"
            "Give the overall result, the decisions made, caveats, and next actions. "
            "Write it for someone who has not seen the intermediate work."
        )
        completion = await self._ask(
            provider, f"You are {team.orchestrator.name}. {team.orchestrator.instructions}", prompt
        )
        await self.store.record(
            job_id, "result", {"content": completion.text}, source=team.orchestrator.id
        )
        await self.db.execute(
            "update jobs set result=?,updated_at=unixepoch('subsec') where id=?",
            (json.dumps({"content": completion.text}), job_id),
        )
        await self._record_artifact(
            job_id, phase.id, team.orchestrator.id, "result.md", "text/markdown", completion.text
        )
        return completion.text

    # ------------------------------------------------------------------ artifacts

    async def _record_artifact(
        self,
        job_id: str,
        phase_id: int | None,
        agent: str,
        name: str,
        mime_type: str,
        content: str,
    ) -> None:
        """Persist an artifact and mirror it into the job workspace.

        v1 had a ``maybe_artifact`` helper that was never called, so the artifacts
        table stayed empty and the Artifacts screen had nothing to show.
        """
        await self.db.execute(
            "insert into artifacts(job_id,phase_id,agent,name,mime_type,content,created_at)"
            " values(?,?,?,?,?,?,unixepoch('subsec'))",
            (job_id, phase_id, agent, name, mime_type, content),
        )
        workspace = await self.db.fetch_value("select workspace from jobs where id=?", (job_id,))
        if workspace:
            try:
                path = Path(workspace)
                path.mkdir(parents=True, exist_ok=True)
                (path / name).write_text(content)
            except OSError as exc:
                log.warning(
                    "could not write artifact to workspace",
                    extra={"job_id": job_id, "name": name, "error": str(exc)},
                )
        await self.store.record(
            job_id, "artifact", {"name": name, "mime_type": mime_type, "phase_id": phase_id}, source=agent
        )

    # --------------------------------------------------------------------- finish

    async def _finish(self, job_id: str, team: Team) -> None:
        failed = await self.db.fetch_value(
            "select count(*) from phases where job_id=? and status='failed'", (job_id,), default=0
        )
        status = "error" if failed else "complete"
        if status == "error":
            await self.db.execute(
                "update jobs set status='error',error=?,updated_at=unixepoch('subsec') where id=?",
                ("one or more phases failed", job_id),
            )
            await self.store.record(job_id, "status", {"status": "error"}, source="system")
        else:
            await self._set_job_status(job_id, "complete")
        # Agents already in 'error' keep that state — it says which one failed.
        await self.db.execute(
            "update job_agents set status=?,current_action=?,updated_at=unixepoch('subsec')"
            " where job_id=? and status<>'error'",
            ("complete" if status == "complete" else "stopped", "Complete", job_id),
        )
        log.info("job finished", extra={"job_id": job_id, "status": status})
