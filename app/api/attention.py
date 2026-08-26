"""One inbox for everything that is waiting on the operator.

Three different things can hold a job up — a gate on an action, a question from an
agent, and a message the operator queued and has not sent yet — and until now each
lived on its own screen, per job. That is the wrong shape for the actual question,
which is never "show me approvals" but "is anything waiting for me". With five jobs
running, answering it meant opening five job screens and three tabs on each.

So this endpoint is deliberately cross-job and deliberately mixed. It joins the job's
task onto every row, because an item with no context is not actionable, and it returns
the counts separately so a badge does not have to count an array it also has to render.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.deps import db
from app.logging_setup import get_logger
from app.models import TERMINAL_JOB_STATUSES
from app.orchestrator.questions import pending_across_jobs

log = get_logger("agent_hub.api.attention")

router = APIRouter(prefix="/api", tags=["attention"])

#: Terminal jobs are excluded from every list here, and the filter lives in the query
#: rather than in each of the paths that ends a job. Stopping already rejects pending
#: gates and cancels open questions, so this is belt and braces for those — but a queued
#: message is different: it is not withdrawn when a job ends, because the operator did
#: not withdraw it. It simply became undeliverable, and an inbox that still offers to
#: edit and expedite it is offering something nothing can honour.
_TERMINAL = tuple(sorted(TERMINAL_JOB_STATUSES))
_PLACEHOLDERS = ",".join("?" * len(_TERMINAL))


@router.get("/attention")
async def attention(limit: Annotated[int, Query(ge=1, le=500)] = 200) -> dict[str, Any]:
    """Everything open, across every job, oldest first.

    Oldest first on purpose, unlike the job list: this is a work queue, and the thing
    that has been blocking a job for twenty minutes matters more than the one raised
    four seconds ago.
    """
    gates, asks, queued = await asyncio.gather(
        db.fetch_all(
            "select a.*, j.task as job_task, j.status as job_status, j.mode as job_mode,"
            " p.name as phase_name, p.seq as phase_seq"
            " from approvals a join jobs j on j.id=a.job_id"
            " left join phases p on p.id=a.phase_id"
            f" where a.status='pending' and j.status not in ({_PLACEHOLDERS})"
            " order by a.created_at limit ?",
            (*_TERMINAL, limit),
        ),
        pending_across_jobs(db, limit=limit),
        # Queued messages are not *blocking* anything, but they are pending operator
        # intent — the window in which an edit or a cancel is still possible — and that
        # window closes silently. Surfacing them here is what makes it visible.
        db.fetch_all(
            "select m.*, j.task as job_task, j.status as job_status"
            " from job_messages m join jobs j on j.id=m.job_id"
            " where m.role='operator' and m.consumed_at is null and m.cancelled_at is null"
            f" and j.status not in ({_PLACEHOLDERS})"
            " order by m.created_at limit ?",
            (*_TERMINAL, limit),
        ),
    )

    approvals = [{**dict(row), "auto": bool(row["auto"])} for row in gates]
    messages = [dict(row) for row in queued]
    return {
        "approvals": approvals,
        "questions": asks,
        "messages": messages,
        "counts": {
            "approvals": len(approvals),
            "questions": len(asks),
            "messages": len(messages),
            # What a badge should show: the two kinds that actually hold work up. A
            # queued message is the operator's own business, and a permanent badge for
            # something you did to yourself is noise.
            "blocking": len(approvals) + len(asks),
        },
    }
