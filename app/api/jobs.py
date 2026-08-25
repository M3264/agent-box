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
from app.models import JobAction, JobCreate, MessageCreate
from app.orchestrator.roles import load_team, resolve_team_id
from app.streams import job_event_stream

log = get_logger("agent_hub.api.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: Job ids are generated, so anything outside this shape is a client bug — reject
#: it at the edge rather than running a query with it.
JobId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

ARTIFACT_COLUMNS = (
    "id,job_id,phase_id,agent,name,mime_type,length(content) as size,created_at"
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


@router.post("", status_code=201)
async def create_job(payload: JobCreate) -> dict[str, Any]:
    """Create a job and start it.

    Only the ``seq 0`` planning phase is seeded — the manager authors the rest. v1
    pre-inserted one phase per hardcoded role here and never updated them, which is
    why every job's plan showed five permanently-queued phases.
    """
    job_id = uuid.uuid4().hex[:12]
    team_id = await resolve_team_id(db, payload.team_id)
    team = await load_team(db, team_id)

    if payload.provider_id and not await db.exists(
        "select 1 from provider_profiles where id=? and enabled=1", (payload.provider_id,)
    ):
        raise HTTPException(
            status_code=400, detail=f"provider '{payload.provider_id}' not found or disabled"
        )

    workspace = settings.workspace_root / job_id
    workspace.mkdir(parents=True, exist_ok=True)

    async with db.transaction() as conn:
        await conn.execute(
            "insert into jobs(id,task,team_id,provider_id,mode,status,paused,workspace,"
            "created_at,updated_at)"
            " values(?,?,?,?,?,'queued',0,?,unixepoch('subsec'),unixepoch('subsec'))",
            (job_id, payload.task, team_id, payload.provider_id, payload.mode, str(workspace)),
        )
        await conn.execute(
            "insert into phases(job_id,seq,kind,name,owner,acceptance,status,created_at)"
            " values(?,0,'plan',?,?,?,'pending',unixepoch('subsec'))",
            (
                job_id,
                "Plan the work",
                team.orchestrator.id,
                "An ordered set of phases, each with one owner and checkable acceptance criteria.",
            ),
        )

    await events.record(job_id, "status", {"status": "queued"}, source="system")
    engine.start(job_id)
    log.info("job created", extra={"job_id": job_id, "mode": payload.mode, "team_id": team_id})
    return {"id": job_id, "status": "queued", "mode": payload.mode, "team_id": team_id}


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

    phases, agents, artifacts, messages, gates, history = await asyncio.gather(
        db.fetch_all("select * from phases where job_id=? order by seq", (job_id,)),
        db.fetch_all("select * from job_agents where job_id=? order by agent", (job_id,)),
        db.fetch_all(
            f"select {ARTIFACT_COLUMNS} from artifacts where job_id=? order by id", (job_id,)
        ),
        db.fetch_all("select * from job_messages where job_id=? order by id", (job_id,)),
        db.fetch_all("select * from approvals where job_id=? order by created_at", (job_id,)),
        events.history(job_id, after=0, limit=2000),
    )

    return {
        **job,
        "team": [dict(row) for row in agents],
        "plan": [_phase_dict(row) for row in phases],
        "artifacts": [dict(row) for row in artifacts],
        "messages": [dict(row) for row in messages],
        "approvals": [dict(row) for row in gates],
        "events": [event.to_dict() for event in history],
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
    if job["status"] in {"complete", "error", "stopped"}:
        raise HTTPException(status_code=409, detail=f"job is {job['status']}; nothing will read this")

    message_id = await db.insert(
        "insert into job_messages(job_id,agent,role,content,created_at)"
        " values(?,?,'operator',?,unixepoch('subsec'))",
        (job_id, payload.agent, payload.content),
    )
    await events.record(
        job_id,
        "message",
        {"content": payload.content, "operator": True, "message_id": message_id},
        source="operator",
    )
    return {
        "id": message_id,
        "job_id": job_id,
        "agent": payload.agent,
        "role": "operator",
        "content": payload.content,
        "consumed_at": None,
    }


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
