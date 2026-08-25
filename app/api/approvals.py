"""Approval endpoints.

The decision recorded here is what releases a phase sitting in
``blocked_on_approval``. In v1 this endpoint wrote a row that unblocked nothing,
because no code path ever waited on an approval.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query

from app.deps import db, events, require_job
from app.logging_setup import get_logger
from app.models import ApprovalCreate, ApprovalDecision
from app.orchestrator import approvals as gates

log = get_logger("agent_hub.api.approvals")

router = APIRouter(prefix="/api", tags=["approvals"])

JobId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
ApprovalId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

Status = Literal["pending", "approved", "rejected", "all"]


def _approval_dict(row: Any) -> dict[str, Any]:
    approval = dict(row)
    approval["auto"] = bool(approval.get("auto"))
    return approval


@router.get("/approvals")
async def list_approvals(
    status: Status = "pending",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Approvals across all jobs, newest first.

    Joined against ``jobs`` so the inbox can show what each gate is for without a
    follow-up request per row.
    """
    sql = (
        "select a.*, j.task as job_task, j.status as job_status, j.mode as job_mode,"
        " p.name as phase_name, p.seq as phase_seq"
        " from approvals a"
        " join jobs j on j.id=a.job_id"
        " left join phases p on p.id=a.phase_id"
    )
    params: list[Any] = []
    if status != "all":
        sql += " where a.status=?"
        params.append(status)
    sql += " order by a.created_at desc limit ?"
    params.append(limit)

    return [_approval_dict(row) for row in await db.fetch_all(sql, params)]


@router.get("/jobs/{job_id}/approvals")
async def job_approvals(job_id: JobId) -> list[dict[str, Any]]:
    await require_job(job_id)
    rows = await db.fetch_all(
        "select a.*, p.name as phase_name, p.seq as phase_seq"
        " from approvals a left join phases p on p.id=a.phase_id"
        " where a.job_id=? order by a.created_at",
        (job_id,),
    )
    return [_approval_dict(row) for row in rows]


@router.post("/jobs/{job_id}/approvals", status_code=201)
async def create_approval(job_id: JobId, payload: ApprovalCreate) -> dict[str, Any]:
    """Raise a gate manually.

    The engine creates its own gates for phases flagged ``requires_approval``; this
    exists for gates raised out of band, and is what an agent tool call will use
    once tools land.
    """
    await require_job(job_id)

    if payload.phase_id is not None and not await db.exists(
        "select 1 from phases where id=? and job_id=?", (payload.phase_id, job_id)
    ):
        raise HTTPException(status_code=400, detail="phase_id does not belong to this job")

    approval_id = await gates.request(
        db,
        events,
        job_id=job_id,
        action=payload.action,
        phase_id=payload.phase_id,
        agent=payload.agent,
        detail=payload.detail,
        risk=payload.risk,
    )
    row = await gates.read(db, approval_id)
    if row is None:  # pragma: no cover - only reachable if the row vanished
        raise HTTPException(status_code=500, detail="approval could not be read back")
    return _approval_dict(row)


@router.post("/jobs/{job_id}/approvals/{approval_id}")
async def decide_approval(
    job_id: JobId, approval_id: ApprovalId, payload: ApprovalDecision
) -> dict[str, Any]:
    """Approve or reject, releasing whatever phase is waiting on this gate.

    Deciding an already-decided gate is a 409 rather than a silent success, so two
    operators racing in the UI both learn what actually happened.
    """
    await require_job(job_id)

    decided = await gates.decide(
        db,
        events,
        job_id=job_id,
        approval_id=approval_id,
        decision=payload.decision,
        note=payload.note,
    )
    if decided is not None:
        return _approval_dict(decided)

    existing = await gates.read(db, approval_id)
    if existing is None or existing["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="approval not found")
    raise HTTPException(
        status_code=409,
        detail=f"approval is already {existing['status']}",
    )
