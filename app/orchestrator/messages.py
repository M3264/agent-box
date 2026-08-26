"""The operator's queue of things to say to a running job.

Split out of the engine because two layers now read this queue and they read it
differently. The engine drains it at a phase boundary, where a message becomes part of
the next prompt. The tool loop drains it *between turns*, where it becomes another user
message in a conversation already under way — which is the only place "stop what you
are doing and read this" can actually be honoured without killing work in flight.

Two rules hold in both places:

- **A cancelled message is never delivered.** ``cancelled_at`` and ``consumed_at`` both
  close a row, so neither query may test ``consumed_at is null`` alone. This is the
  whole point of having an undo: the window in which the operator most wants one is the
  window in which the message has not been read yet.
- **Consuming is a write, and it happens before the content is used.** A message read
  into a prompt and then lost to a crash would be re-delivered on the re-run, and an
  instruction the team acts on twice is worse than one it never sees — the operator can
  see an undelivered message still sitting in the queue and send it again.
"""

from __future__ import annotations

from typing import Any

from app.db import Database
from app.logging_setup import get_logger

log = get_logger("agent_hub.messages")


async def pending(database: Database, job_id: str) -> list[dict[str, Any]]:
    """Everything queued and not yet delivered, oldest first."""
    rows = await database.fetch_all(
        "select * from job_messages where job_id=? and role='operator'"
        " and consumed_at is null and cancelled_at is null order by id",
        (job_id,),
    )
    return [dict(row) for row in rows]


async def drain(
    database: Database, job_id: str, *, immediate_only: bool = False
) -> list[str]:
    """Consume queued messages and return their text.

    ``immediate_only`` is the mid-phase drain: it takes only what the operator marked
    urgent and leaves the rest for the boundary, because a message sent with no urgency
    should not interrupt a phase merely because the loop happened to look. The boundary
    drain takes everything, urgent messages included — one that was never picked up
    mid-phase (the phase may have ended first, or been a plan phase with no loop at all)
    must still arrive rather than silently expire.
    """
    where = "job_id=? and role='operator' and consumed_at is null and cancelled_at is null"
    params: list[Any] = [job_id]
    if immediate_only:
        where += " and delivery='immediate'"

    rows = await database.fetch_all(
        f"select id,content from job_messages where {where} order by id", tuple(params)
    )
    if not rows:
        return []

    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" * len(ids))
    await database.execute(
        f"update job_messages set consumed_at=unixepoch('subsec') where id in ({placeholders})",
        ids,
    )
    log.info(
        "operator messages delivered",
        extra={"job_id": job_id, "count": len(ids), "immediate": immediate_only},
    )
    return [str(row["content"]) for row in rows]


def as_prompt(guidance: list[str]) -> str:
    """Queued messages as a block for a prompt, phrased so they are not mistaken for data.

    Numbered because more than one is common — the operator typed twice while a phase
    ran — and a model given two unlabelled paragraphs tends to answer the second and
    forget the first.
    """
    if len(guidance) == 1:
        return guidance[0]
    return "\n\n".join(f"{index}. {text}" for index, text in enumerate(guidance, start=1))
