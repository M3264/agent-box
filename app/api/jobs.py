"""Job endpoints: creation, snapshots, sub-resources, control, and streaming."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.config import settings
from app.deps import db, engine, events, require_job
from app.logging_setup import get_logger
from app.models import (
    TERMINAL_JOB_STATUSES,
    AgentAssignment,
    BudgetUpdate,
    JobAction,
    JobContinue,
    JobCreate,
    JobPatch,
    JobRerun,
    MessageCreate,
    MessageUpdate,
    QuestionAnswer,
)
from app.orchestrator.engine import Assignment, resolve_choice
from app.orchestrator.questions import answer as answer_question
from app.orchestrator.questions import cancel_open as cancel_questions
from app.orchestrator.questions import for_job as questions_for_job
from app.orchestrator.roles import Team, load_team, resolve_team_id
from app.orchestrator.sandbox import status as sandbox_status
from app.orchestrator.usage import job_usage
from app.streams import job_event_stream

log = get_logger("agent_hub.api.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: Job ids are generated, so anything outside this shape is a client bug — reject
#: it at the edge rather than running a query with it.
JobId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

ARTIFACT_COLUMNS = (
    "id,job_id,phase_id,agent,name,mime_type,length(content) as size,created_at"
)

#: The Commands list never needs the captured output — a job with a few hundred
#: commands would otherwise ship megabytes of logs into a snapshot.
TOOL_CALL_COLUMNS = (
    "id,job_id,phase_id,turn,agent,tool,args,status,exit_code,truncated,sandbox,"
    "approval_id,duration_ms,created_at,started_at,finished_at"
)


def _json_column(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _job_dict(row: Any) -> dict[str, Any]:
    job = dict(row)
    job["paused"] = bool(job["paused"])
    job["result"] = _json_column(job.get("result"), None)
    return job


def _phase_dict(row: Any) -> dict[str, Any]:
    phase = dict(row)
    phase["depends_on"] = _json_column(phase.get("depends_on"), [])
    phase["requires_approval"] = bool(phase.get("requires_approval"))
    return phase


def _tool_call_dict(row: Any) -> dict[str, Any]:
    call = dict(row)
    call["args"] = _json_column(call.get("args"), {})
    call["truncated"] = bool(call.get("truncated"))
    return call


async def _action(job_id: str) -> JobAction:
    """Report the job's real persisted state after a control action.

    ``status`` stays the underlying lifecycle status and ``paused`` is a separate
    flag, so a client can tell what resuming will actually go back to — a job
    paused while blocked on approval is still blocked.
    """
    row = await db.fetch_one("select status,paused from jobs where id=?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobAction(id=job_id, status=row["status"], paused=bool(row["paused"]))


async def _check_provider(provider_id: str | None, *, where: str) -> None:
    if provider_id and not await db.exists(
        "select 1 from provider_profiles where id=? and enabled=1", (provider_id,)
    ):
        raise HTTPException(
            status_code=400, detail=f"provider '{provider_id}' ({where}) not found or disabled"
        )


async def _validate_assignments(
    team: Team, job_provider_id: str | None, agents: list[AgentAssignment]
) -> None:
    """Reject per-agent choices that could not work, before the job exists.

    Checked here as well as being resolved in the engine, for the same reason the
    sandbox is: the engine's refusal is the real guarantee, but a 400 at creation says
    which agent and which model, instead of handing back a job that dies partway
    through on its third phase.
    """
    known = set(team.role_ids)
    for entry in agents:
        if entry.agent not in known:
            raise HTTPException(
                status_code=400,
                detail=f"'{entry.agent}' is not a role in team '{team.name}'"
                f" (roles: {', '.join(sorted(known))})",
            )
        await _check_provider(entry.provider_id, where=f"for agent '{entry.agent}'")

        # Resolved through exactly the precedence the engine will use, so a model given
        # without a provider is checked against the provider it will actually reach.
        provider_id, model = resolve_choice(
            {"provider_id": job_provider_id},
            team.get(entry.agent),
            Assignment(provider_id=entry.provider_id, model=entry.model),
        )
        if not model:
            continue
        if provider_id is None:
            # The job named no provider, so it will run on whichever profile is the
            # server default. Checking against that default now is worth doing even
            # though it could change before the job starts: it is the common case, and
            # skipping it would leave the check firing only for operators who pin a
            # provider explicitly. The engine's own refusal remains the guarantee.
            provider_id = await db.fetch_value(
                "select id from provider_profiles where enabled=1 order by rowid limit 1"
            )
        if not provider_id:
            continue
        listed = [
            row["model"]
            for row in await db.fetch_all(
                "select model from provider_models where provider_id=? order by model",
                (provider_id,),
            )
        ]
        # An empty list means the profile predates model lists entirely; refusing every
        # model there would be worse than letting the endpoint answer.
        if listed and model not in listed:
            raise HTTPException(
                status_code=400,
                detail=f"'{model}' is not a model on provider '{provider_id}'"
                f" (available: {', '.join(listed)})",
            )


async def _write_assignments(conn: Any, job_id: str, agents: list[AgentAssignment]) -> None:
    for entry in agents:
        if entry.provider_id is None and entry.model is None:
            # Nothing pinned is the same as no row, and a row of nulls would make the
            # UI show an override that changes nothing.
            continue
        await conn.execute(
            "insert into job_agent_providers(job_id,agent,provider_id,model) values(?,?,?,?)"
            " on conflict(job_id,agent) do update set"
            "  provider_id=excluded.provider_id,model=excluded.model",
            (job_id, entry.agent, entry.provider_id, entry.model),
        )


async def _spawn_job(
    *,
    task: str,
    team_id: int | None,
    mode: str,
    provider_id: str | None,
    sandbox: str | None,
    agents: list[AgentAssignment],
    token_budget: int | None = None,
    forked_from: str | None = None,
) -> dict[str, Any]:
    """Create a job, seed its planning phase, and start it.

    Only the ``seq 0`` planning phase is seeded — the manager authors the rest. v1
    pre-inserted one phase per hardcoded role here and never updated them, which is why
    every job's plan showed five permanently-queued phases.
    """
    job_id = uuid.uuid4().hex[:12]
    resolved_team = await resolve_team_id(db, team_id)
    team = await load_team(db, resolved_team)

    await _check_provider(provider_id, where="for the job")
    await _validate_assignments(team, provider_id, agents)

    workspace = settings.workspace_root / job_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Refused here as well as in the engine. The engine check is the real guarantee
    # (nothing runs in a backend the operator did not choose), but a 400 at creation
    # tells the operator *why* instead of handing them a job that dies on its first
    # phase. `None` is not checked: it resolves to the server default when the job
    # starts, which may be different by then.
    if sandbox is not None:
        state = sandbox_status().get(sandbox)
        if state is not None and not state.available:
            raise HTTPException(
                status_code=400,
                detail=f"the '{sandbox}' sandbox is not available on this host: {state.reason}",
            )

    async with db.transaction() as conn:
        await conn.execute(
            "insert into jobs(id,task,team_id,provider_id,mode,status,paused,workspace,"
            "sandbox,token_budget,forked_from,rounds,created_at,updated_at)"
            " values(?,?,?,?,?,'queued',0,?,?,?,?,1,unixepoch('subsec'),unixepoch('subsec'))",
            (
                job_id,
                task,
                resolved_team,
                provider_id,
                mode,
                str(workspace),
                sandbox,
                # 0 is a deliberate "no cap", distinct from None, which is "whatever the
                # server default is at the moment the job runs" — so it is stored as
                # null and resolved later, exactly like the sandbox above.
                token_budget,
                forked_from,
            ),
        )
        await conn.execute(
            "insert into phases(job_id,seq,kind,name,owner,acceptance,status,round,created_at)"
            " values(?,0,'plan',?,?,?,'pending',1,unixepoch('subsec'))",
            (
                job_id,
                "Plan the work",
                team.orchestrator.id,
                "An ordered set of phases, each with one owner and checkable acceptance criteria.",
            ),
        )
        await _write_assignments(conn, job_id, agents)

    await events.record(job_id, "status", {"status": "queued"}, source="system")
    engine.start(job_id)
    log.info(
        "job created",
        extra={
            "job_id": job_id,
            "mode": mode,
            "team_id": resolved_team,
            "assigned_agents": len(agents),
            "forked_from": forked_from,
        },
    )
    return {
        "id": job_id,
        "status": "queued",
        "mode": mode,
        "team_id": resolved_team,
        "sandbox": sandbox,
        "token_budget": token_budget,
        "provider_id": provider_id,
        "forked_from": forked_from,
    }


@router.post("", status_code=201)
async def create_job(payload: JobCreate) -> dict[str, Any]:
    return await _spawn_job(
        task=payload.task,
        team_id=payload.team_id,
        mode=payload.mode,
        provider_id=payload.provider_id,
        sandbox=payload.sandbox,
        token_budget=payload.token_budget,
        agents=payload.agents,
    )


@router.get("")
async def list_jobs(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    """List jobs with the counts the list screen needs.

    Computed here rather than client-side: the v1 UI derived its "needs attention"
    figure from a job status the backend never set, so it was permanently zero, and
    an accurate count would otherwise cost one request per job.
    """
    rows = await db.fetch_all(
        """
        select j.*,
               (select count(*) from approvals a
                 where a.job_id=j.id and a.status='pending')          as pending_approvals,
               (select count(*) from questions q
                 where q.job_id=j.id and q.status='pending')          as pending_questions,
               (select count(*) from job_messages m
                 where m.job_id=j.id and m.consumed_at is null
                   and m.cancelled_at is null)                        as pending_messages,
               (select count(*) from phases p where p.job_id=j.id)    as phase_total,
               (select count(*) from phases p
                 where p.job_id=j.id and p.status='complete')         as phase_complete,
               (select count(*) from artifacts f where f.job_id=j.id) as artifact_count
          from jobs j order by j.created_at desc limit ?
        """,
        (limit,),
    )
    return [_job_dict(row) for row in rows]


@router.get("/{job_id}")
async def get_job(job_id: JobId) -> dict[str, Any]:
    """Full snapshot: the state the UI renders before it starts following the stream.

    Returned in one round trip, with the event ``cursor`` the client should open its
    stream at, so there is no window between snapshot and subscription.
    """
    job = _job_dict(await require_job(job_id))

    phases, agents, artifacts, messages, gates, calls, asks, history, usage, pinned = (
        await asyncio.gather(
            db.fetch_all("select * from phases where job_id=? order by seq", (job_id,)),
            db.fetch_all("select * from job_agents where job_id=? order by agent", (job_id,)),
            db.fetch_all(
                f"select {ARTIFACT_COLUMNS} from artifacts where job_id=? order by id", (job_id,)
            ),
            db.fetch_all("select * from job_messages where job_id=? order by id", (job_id,)),
            db.fetch_all("select * from approvals where job_id=? order by created_at", (job_id,)),
            db.fetch_all(
                f"select {TOOL_CALL_COLUMNS} from tool_calls where job_id=? order by created_at,rowid",
                (job_id,),
            ),
            questions_for_job(db, job_id),
            events.history(job_id, after=0, limit=2000),
            job_usage(db, job_id),
            db.fetch_all(
                "select agent,provider_id,model from job_agent_providers where job_id=?"
                " order by agent",
                (job_id,),
            ),
        )
    )

    return {
        **job,
        "team": [dict(row) for row in agents],
        "plan": [_phase_dict(row) for row in phases],
        "artifacts": [dict(row) for row in artifacts],
        "messages": [dict(row) for row in messages],
        "approvals": [dict(row) for row in gates],
        "questions": asks,
        "tool_calls": [_tool_call_dict(row) for row in calls],
        "events": [event.to_dict() for event in history],
        "usage": usage,
        "agent_providers": [dict(row) for row in pinned],
        # A finished job is not a closed one: the operator can add a round to it or
        # start a fresh job from it, and the UI needs to know which without guessing
        # from the status string.
        "can_continue": job["status"] in TERMINAL_JOB_STATUSES,
        "cursor": history[-1].id if history else 0,
    }


@router.get("/{job_id}/plan")
async def get_plan(job_id: JobId) -> list[dict[str, Any]]:
    await require_job(job_id)
    rows = await db.fetch_all("select * from phases where job_id=? order by seq", (job_id,))
    return [_phase_dict(row) for row in rows]


@router.get("/{job_id}/agents")
async def get_agents(job_id: JobId) -> list[dict[str, Any]]:
    await require_job(job_id)
    rows = await db.fetch_all("select * from job_agents where job_id=? order by agent", (job_id,))
    return [dict(row) for row in rows]


@router.get("/{job_id}/artifacts")
async def get_artifacts(job_id: JobId) -> list[dict[str, Any]]:
    """Artifact metadata for one job.

    Per-job by construction: v1's Artifacts screen read a key off the job list rows
    that the list endpoint never returned, so it was always empty.
    """
    await require_job(job_id)
    rows = await db.fetch_all(
        f"select {ARTIFACT_COLUMNS} from artifacts where job_id=? order by id", (job_id,)
    )
    return [dict(row) for row in rows]


@router.get("/{job_id}/artifacts/{artifact_id}")
async def download_artifact(job_id: JobId, artifact_id: Annotated[int, Path(ge=1)]) -> Response:
    await require_job(job_id)
    row = await db.fetch_one(
        "select name,mime_type,content from artifacts where id=? and job_id=?",
        (artifact_id, job_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    # Filename is server-generated, but quotes would still break the header.
    filename = str(row["name"]).replace('"', "")
    return Response(
        content=row["content"] or "",
        media_type=row["mime_type"] or "text/plain",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{job_id}/tool-calls")
async def get_tool_calls(
    job_id: JobId,
    phase_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[dict[str, Any]]:
    """Every command the team has run on this job, oldest first.

    Metadata only — ``stdout``/``stderr`` come from the single-call endpoint, so
    opening the Commands tab on a job that ran a test suite does not download the
    whole suite's output.
    """
    await require_job(job_id)
    where = "job_id=?"
    params: list[Any] = [job_id]
    if phase_id is not None:
        where += " and phase_id=?"
        params.append(phase_id)
    params.append(limit)
    rows = await db.fetch_all(
        f"select {TOOL_CALL_COLUMNS} from tool_calls where {where}"
        " order by created_at,rowid limit ?",
        tuple(params),
    )
    return [_tool_call_dict(row) for row in rows]


@router.get("/{job_id}/tool-calls/{call_id}")
async def get_tool_call(
    job_id: JobId,
    call_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> dict[str, Any]:
    """One command, including the captured output slice.

    The slice is what the database kept; the untruncated streams live in the
    workspace, which ``stdout_path``/``stderr_path`` name so the operator can go and
    read what was cut.
    """
    await require_job(job_id)
    row = await db.fetch_one("select * from tool_calls where id=? and job_id=?", (call_id, job_id))
    if row is None:
        raise HTTPException(status_code=404, detail="tool call not found")
    call = _tool_call_dict(row)
    call["stdout_path"] = f".agent-hub/tool-{call_id}.out"
    call["stderr_path"] = f".agent-hub/tool-{call_id}.err"
    return call


@router.get("/{job_id}/messages")
async def get_messages(job_id: JobId) -> list[dict[str, Any]]:
    await require_job(job_id)
    rows = await db.fetch_all("select * from job_messages where job_id=? order by id", (job_id,))
    return [dict(row) for row in rows]


@router.post("/{job_id}/messages", status_code=201)
async def post_message(job_id: JobId, payload: MessageCreate) -> dict[str, Any]:
    """Send operator guidance into a job.

    The engine drains unconsumed messages before each phase and injects them into
    the prompt, so this reaches the team instead of only being recorded.
    ``consumed_at`` is how the UI shows whether the team has picked it up yet.
    """
    job = await require_job(job_id)
    if job["status"] in TERMINAL_JOB_STATUSES:
        # Not a dead end: the conversation stays open, but a message dropped into a
        # finished job would sit unconsumed forever, so the operator is pointed at the
        # endpoint that actually does something with it. Spending a round of tokens is
        # a decision, not a side effect of typing.
        raise HTTPException(
            status_code=409,
            detail=f"job is {job['status']}; POST /api/jobs/{job_id}/continue to work on it further",
        )

    message_id = await db.insert(
        "insert into job_messages(job_id,agent,role,content,delivery,created_at)"
        " values(?,?,'operator',?,?,unixepoch('subsec'))",
        (job_id, payload.agent, payload.content, payload.delivery),
    )
    await events.record(
        job_id,
        "message",
        {
            "content": payload.content,
            "operator": True,
            "message_id": message_id,
            "delivery": payload.delivery,
        },
        source="operator",
    )
    return {
        "id": message_id,
        "job_id": job_id,
        "agent": payload.agent,
        "role": "operator",
        "content": payload.content,
        "delivery": payload.delivery,
        "consumed_at": None,
        "cancelled_at": None,
        "updated_at": None,
    }


async def _pending_message(job_id: str, message_id: int) -> Any:
    """A message that can still be changed, or a 404/409 explaining why not.

    409 rather than 404 once it has been delivered, because "too late" and "no such
    message" are different things to see in a UI — the first means the team already
    read it, and the operator's next move is to send a correction rather than hunt for
    a bug.
    """
    row = await db.fetch_one(
        "select * from job_messages where id=? and job_id=?", (message_id, job_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    if row["role"] != "operator":
        raise HTTPException(status_code=409, detail="only operator messages can be changed")
    if row["cancelled_at"] is not None:
        raise HTTPException(status_code=409, detail="message was already cancelled")
    if row["consumed_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="the team has already read this message; send another to correct it",
        )
    return row


@router.patch("/{job_id}/messages/{message_id}")
async def update_message(
    job_id: JobId, message_id: Annotated[int, Path(ge=1)], payload: MessageUpdate
) -> dict[str, Any]:
    """Edit a queued message, or change when it will be delivered.

    Both halves of "actually, I meant…" — a queued message used to be immutable, so a
    typo could only be followed by a second message contradicting the first, which is
    exactly the kind of thing a model resolves badly. Switching ``delivery`` to
    ``immediate`` is the "send it now" action: the running phase picks it up at its
    next turn rather than at the next boundary.
    """
    await require_job(job_id)
    row = await _pending_message(job_id, message_id)

    content = payload.content if payload.content is not None else str(row["content"])
    delivery = payload.delivery or str(row["delivery"] or "boundary")
    await db.execute(
        "update job_messages set content=?,delivery=?,updated_at=unixepoch('subsec')"
        " where id=? and consumed_at is null and cancelled_at is null",
        (content, delivery, message_id),
    )
    await events.record(
        job_id,
        "message",
        {
            "content": content,
            "operator": True,
            "message_id": message_id,
            "delivery": delivery,
            "action": "expedited" if payload.content is None else "edited",
        },
        source="operator",
    )
    return {**dict(row), "content": content, "delivery": delivery}


@router.delete("/{job_id}/messages/{message_id}")
async def cancel_message(
    job_id: JobId, message_id: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    """Withdraw a message the team has not read yet.

    Cancelled rather than deleted: the transcript should still show that something was
    typed and taken back, which is a real event in the supervision of a job, and a row
    that vanishes makes the stream lie about what happened.
    """
    await require_job(job_id)
    row = await _pending_message(job_id, message_id)
    await db.execute(
        "update job_messages set cancelled_at=unixepoch('subsec')"
        " where id=? and consumed_at is null and cancelled_at is null",
        (message_id,),
    )
    await events.record(
        job_id,
        "message",
        {
            "content": str(row["content"]),
            "operator": True,
            "message_id": message_id,
            "action": "cancelled",
        },
        source="operator",
    )
    return {**dict(row), "cancelled": True}


# ------------------------------------------------------------------------- questions


@router.get("/{job_id}/questions")
async def get_questions(job_id: JobId) -> list[dict[str, Any]]:
    await require_job(job_id)
    return await questions_for_job(db, job_id)


@router.post("/{job_id}/questions/{question_id}/answer")
async def answer_job_question(
    job_id: JobId,
    question_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    payload: QuestionAnswer,
) -> dict[str, Any]:
    """Answer an agent's question and let its phase carry on.

    409 when the question is no longer pending, which is the case worth being explicit
    about: it may have timed out, or the job may have been stopped, and either way the
    answer would go nowhere. The operator should see that rather than watch a job that
    never moves.
    """
    await require_job(job_id)
    row = await answer_question(
        db, events, job_id=job_id, question_id=question_id, text=payload.text,
        chosen=payload.chosen,
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="that question is not open for an answer (already answered, cancelled, or timed out)",
        )
    return row


# --------------------------------------------------------------------- continuation


@router.post("/{job_id}/continue", status_code=202)
async def continue_job(job_id: JobId, payload: JobContinue) -> dict[str, Any]:
    """Add a round of work to a job that has already finished.

    The same job continues rather than a copy being made: the earlier rounds' phases
    stay exactly where they are — terminal, with their output readable — and a new
    planning phase is appended behind them. The planner is given both the operator's
    instruction and what the team already produced, so a follow-up can say "now also
    check the login flow" without restating the original task.

    Why append instead of reopening the last phase: a completed phase's output is
    committed and other phases have read it. Rewriting it would make the transcript
    disagree with what actually happened, which is the one thing the durability rule
    exists to prevent.
    """
    job = await require_job(job_id)
    if job["status"] not in TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"job is {job['status']}; send guidance to /messages instead",
        )
    if engine.is_running(job_id):  # pragma: no cover - terminal jobs have no task
        raise HTTPException(status_code=409, detail="job is still winding down; try again")

    team = await load_team(db, int(job["team_id"]))
    round_number = int(job["rounds"] or 1) + 1

    async with db.transaction() as conn:
        await conn.execute(
            "insert into job_messages(job_id,agent,role,content,created_at)"
            " values(?,?,'operator',?,unixepoch('subsec'))",
            (job_id, team.orchestrator.id, payload.instruction),
        )
        async with conn.execute(
            "select coalesce(max(seq),0) from phases where job_id=?", (job_id,)
        ) as cursor:
            next_seq = int((await cursor.fetchone())[0]) + 1
        await conn.execute(
            "insert into phases(job_id,seq,kind,name,owner,acceptance,status,round,created_at)"
            " values(?,?,'plan',?,?,?,'pending',?,unixepoch('subsec'))",
            (
                job_id,
                next_seq,
                f"Plan round {round_number}",
                team.orchestrator.id,
                "A plan for the follow-up only, building on the work already done.",
                round_number,
            ),
        )
        # `error` is cleared because it described the round that has ended. Leaving it
        # set would make a successful follow-up render as a failed job.
        await conn.execute(
            "update jobs set status='queued',paused=0,error=null,rounds=?,"
            "updated_at=unixepoch('subsec') where id=?",
            (round_number, job_id),
        )

    await events.record(
        job_id,
        "notice",
        {
            "message": f"Round {round_number} requested by the operator.",
            "instruction": payload.instruction,
            "round": round_number,
        },
        source="operator",
    )
    await events.record(job_id, "status", {"status": "queued"}, source="system")
    engine.start(job_id)
    log.info("job continued", extra={"job_id": job_id, "round": round_number})
    return {"id": job_id, "status": "queued", "round": round_number}


@router.post("/{job_id}/rerun", status_code=201)
async def rerun_job(job_id: JobId, payload: JobRerun) -> dict[str, Any]:
    """Start a fresh job seeded from this one.

    For "do that again, but…" — the team, mode, sandbox, provider and every per-agent
    assignment carry over, so only the part that changes has to be sent. A new job
    rather than a new round because the workspace starts clean and the original stays
    exactly as it was; ``forked_from`` keeps the pair traceable.

    Allowed on a running job as well as a finished one: comparing two settings side by
    side is a reasonable thing to want, and nothing about the original is touched.
    """
    job = await require_job(job_id)

    if payload.agents is not None:
        agents = list(payload.agents)
    else:
        agents = [
            AgentAssignment(
                agent=row["agent"], provider_id=row["provider_id"], model=row["model"]
            )
            for row in await db.fetch_all(
                "select agent,provider_id,model from job_agent_providers where job_id=?"
                " order by agent",
                (job_id,),
            )
        ]

    created = await _spawn_job(
        task=payload.task or job["task"],
        team_id=payload.team_id if payload.team_id is not None else int(job["team_id"]),
        mode=payload.mode or job["mode"],
        # Absent means inherit; an explicit null means "no provider, use the server
        # default". `is not None` alone cannot say the second one, and a re-run form
        # that shows the inherited provider has to be able to clear it.
        provider_id=(
            payload.provider_id if "provider_id" in payload.model_fields_set else job["provider_id"]
        ),
        sandbox=payload.sandbox if "sandbox" in payload.model_fields_set else job["sandbox"],
        token_budget=(
            payload.token_budget
            if "token_budget" in payload.model_fields_set
            else job["token_budget"]
        ),
        agents=agents,
        forked_from=job_id,
    )
    # Recorded on the original too, so its transcript says where the follow-up went.
    await events.record(
        job_id, "notice", {"message": f"Re-run as job {created['id']}.", "job_id": created["id"]},
        source="operator",
    )
    return created


@router.get("/{job_id}/usage")
async def get_usage(job_id: JobId) -> dict[str, Any]:
    """What this job has spent, in total and broken down by agent and model.

    Every provider response already carried these numbers and every one of them was
    discarded, so a job that cost four million tokens looked exactly like one that cost
    four thousand.
    """
    await require_job(job_id)
    return await job_usage(db, job_id)


@router.patch("/{job_id}")
async def patch_job(job_id: JobId, payload: JobPatch) -> dict[str, Any]:
    """Retune a job that has not finished — provider, per-agent models, team, sandbox,
    token budget, or controlled/yolo mode — without stopping it.

    Validated with the very same helpers as job creation (``_check_provider``,
    ``_validate_assignments``, the sandbox-availability probe), so a bad switch is a 400
    here rather than a job that dies three phases in. Only the fields actually sent are
    written; an explicit ``null`` resets a nullable one to the server default. The change
    lands at the next phase boundary — a queued job reads it when it starts, a running one
    at its next phase, a paused one on resume — so a phase already in flight keeps the
    config it began with. Rerun, not this, is the tool for a finished job.
    """
    job = await require_job(job_id)
    if job["status"] in TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"job is {job['status']}; use rerun to start a fresh job from it",
        )

    fields = payload.model_fields_set

    # Resolve the *effective* team and provider — the new value if sent, else what the job
    # already runs with — so the assignment check runs against the configuration the job
    # will actually have, exactly as _spawn_job validates before a job exists.
    resolved_team = (
        await resolve_team_id(db, payload.team_id) if "team_id" in fields else int(job["team_id"])
    )
    effective_provider = payload.provider_id if "provider_id" in fields else job["provider_id"]

    if "provider_id" in fields:
        # Skips a null (which means "server default", resolved when the job runs) just as
        # _check_provider does; only re-checked when the provider is actually changing, so
        # an unrelated edit is not blocked by a provider disabled since the job started.
        await _check_provider(payload.provider_id, where="for the job")
    if payload.agents is not None:
        team = await load_team(db, resolved_team)
        await _validate_assignments(team, effective_provider, payload.agents)
    if "sandbox" in fields and payload.sandbox is not None:
        state = sandbox_status().get(payload.sandbox)
        if state is not None and not state.available:
            raise HTTPException(
                status_code=400,
                detail=f"the '{payload.sandbox}' sandbox is not available on this host: {state.reason}",
            )

    # Write only the columns actually sent. team_id is stored resolved (like _spawn_job),
    # so the row always names a real template. mode has no null meaning, so an explicit
    # null is skipped rather than blanking a NOT-NULL column.
    sets: list[str] = []
    args: list[Any] = []
    if "team_id" in fields:
        sets.append("team_id=?")
        args.append(resolved_team)
    if "mode" in fields and payload.mode is not None:
        sets.append("mode=?")
        args.append(payload.mode)
    if "provider_id" in fields:
        sets.append("provider_id=?")
        args.append(payload.provider_id)
    if "sandbox" in fields:
        sets.append("sandbox=?")
        args.append(payload.sandbox)
    if "token_budget" in fields:
        sets.append("token_budget=?")
        args.append(payload.token_budget)

    async with db.transaction() as conn:
        if sets:
            sets.append("updated_at=unixepoch('subsec')")
            await conn.execute(f"update jobs set {','.join(sets)} where id=?", (*args, job_id))
        if payload.agents is not None:
            # Replace-semantics: the sent list is the whole set of per-agent overrides now,
            # so a cleared row disappears rather than lingering from an earlier config.
            await conn.execute("delete from job_agent_providers where job_id=?", (job_id,))
            await _write_assignments(conn, job_id, payload.agents)

    await events.record(
        job_id,
        "notice",
        {"message": "Configuration updated; applies at the next phase.", "config": True},
        source="operator",
    )
    log.info("job config patched", extra={"job_id": job_id, "fields": sorted(fields)})
    return _job_dict(await require_job(job_id))


@router.patch("/{job_id}/budget")
async def set_budget(job_id: JobId, payload: BudgetUpdate) -> dict[str, Any]:
    """Raise, lower, or remove this job's token cap.

    A running job picks the new cap up at its next phase boundary, where the meter that
    enforces it is re-read from this row; lowering it below what has already been spent
    therefore stops the job at the next call after that boundary rather than retroactively,
    which is the only thing it could honestly do. (``PATCH /{job_id}`` changes the same
    column; this endpoint stays as the one-field shortcut the budget control uses.)
    """
    await require_job(job_id)
    await db.execute(
        "update jobs set token_budget=?,updated_at=unixepoch('subsec') where id=?",
        (payload.token_budget, job_id),
    )
    await events.record(
        job_id,
        "notice",
        {
            "message": (
                f"Token budget set to {payload.token_budget:,}."
                if payload.token_budget
                else "Token budget removed."
            ),
            "budget": True,
        },
        source="operator",
    )
    return await job_usage(db, job_id)


# ------------------------------------------------------------------------ control


@router.post("/{job_id}/pause")
async def pause_job(job_id: JobId) -> JobAction:
    job = await require_job(job_id)
    if job["status"] in {"complete", "error", "stopped"}:
        return await _action(job_id)
    await db.execute(
        "update jobs set paused=1,updated_at=unixepoch('subsec')"
        " where id=? and status not in ('complete','error','stopped')",
        (job_id,),
    )
    await events.record(job_id, "status", {"status": "paused"}, source="system")
    return await _action(job_id)


@router.post("/{job_id}/resume")
async def resume_job(job_id: JobId) -> JobAction:
    job = await require_job(job_id)
    if job["status"] in {"complete", "error", "stopped"}:
        return await _action(job_id)

    await db.execute("update jobs set paused=0,updated_at=unixepoch('subsec') where id=?", (job_id,))
    # Wake the pause gate instead of waiting for it to notice on its next recheck.
    engine.notify_resumed(job_id)
    if not engine.is_running(job_id):
        engine.start(job_id)
    return await _action(job_id)


@router.post("/{job_id}/stop")
async def stop_job(job_id: JobId) -> JobAction:
    """Stop a job, interrupting any in-flight provider call.

    ``status='stopped'`` is written *before* cancelling so the engine can tell an
    operator stop (terminal) from a process shutdown (resumable) when it unwinds.
    """
    job = await require_job(job_id)
    if job["status"] in {"complete", "error", "stopped"}:
        return await _action(job_id)

    await db.execute(
        "update jobs set status='stopped',paused=0,updated_at=unixepoch('subsec') where id=?",
        (job_id,),
    )
    await engine.stop(job_id)

    await db.execute(
        "update phases set status='skipped',error='job stopped',"
        "finished_at=unixepoch('subsec')"
        " where job_id=? and status in ('pending','active','blocked_on_approval')",
        (job_id,),
    )
    await db.execute(
        "update approvals set status='rejected',decided_at=unixepoch('subsec'),"
        "decision_note='job stopped' where job_id=? and status='pending'",
        (job_id,),
    )
    # Same for an open question: the agent that asked it is gone, so leaving it in the
    # inbox would invite an answer nothing will ever read.
    await cancel_questions(db, events, job_id=job_id, reason="The job was stopped.")
    # A command that was in flight died with the task that spawned it, so the row must
    # say so. Left as 'running' the Commands view would claim a stopped job is still
    # executing something, and the startup sweep deliberately skips terminal jobs, so
    # nothing else would ever correct it.
    await db.execute(
        "update tool_calls"
        " set status=case status when 'running' then 'interrupted' else 'cancelled' end,"
        "finished_at=unixepoch('subsec')"
        " where job_id=? and status in ('running','pending')",
        (job_id,),
    )
    await db.execute(
        "update job_agents set status='stopped',current_action='Stopped',"
        "updated_at=unixepoch('subsec') where job_id=?",
        (job_id,),
    )
    # Recorded last: this event closes every live stream for the job.
    await events.record(job_id, "status", {"status": "stopped"}, source="system")
    return await _action(job_id)


# ---------------------------------------------------------------------- streaming


@router.get("/{job_id}/events/history")
async def event_history(
    job_id: JobId,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[dict[str, Any]]:
    """Durable replay by cursor — the resync path when a live stream falls behind."""
    await require_job(job_id)
    return [event.to_dict() for event in await events.history(job_id, after=after, limit=limit)]


@router.get("/{job_id}/events")
async def event_stream(request: Request, job_id: JobId) -> StreamingResponse:
    """Server-sent events, used by the CLI.

    Backed by the same broker as the WebSocket, so it no longer polls SQLite every
    500ms per connected client. Honours ``Last-Event-ID`` and ``?after=`` so a
    reconnect resumes exactly where it left off.
    """
    await require_job(job_id)

    raw_cursor = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        cursor = max(0, int(raw_cursor))
    except ValueError:
        cursor = 0

    async def generate():
        async for event in job_event_stream(events, job_id, after=cursor, keepalive=15.0):
            if event is None:
                # Comment frame: keeps proxies and clients from timing the idle
                # connection out without inventing a fake event id.
                yield ": keepalive\n\n"
                continue
            yield f"id: {event.id}\ndata: {json.dumps(event.to_dict(), default=str)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold events
            # back until the buffer filled.
            "X-Accel-Buffering": "no",
        },
    )
