"""Approval gates that actually block execution.

In v1 nothing created an approval and nothing waited on one: ``decide_approval``
recorded a decision that unblocked no work, so §3's "risky actions pause for
approval" was decorative.

Here a gate is durable first and in-memory second. The ``approvals`` row plus the
phase's ``blocked_on_approval`` status are the source of truth; the ``asyncio.Event``
is only a wake-up. That split is what lets a restart re-register a waiter for a
phase that was already blocked, instead of re-running the phase or losing the gate.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.events import EventStore
from app.logging_setup import get_logger

log = get_logger("agent_hub.approvals")


class ApprovalRejected(Exception):
    """Raised inside a phase when the operator rejects its gate."""

    def __init__(self, approval_id: str, note: str | None = None) -> None:
        self.approval_id = approval_id
        self.note = note
        super().__init__(note or "operator rejected the action")


@dataclass(slots=True)
class Decision:
    status: str  # approved | rejected
    note: str | None
    auto: bool = False

    @property
    def approved(self) -> bool:
        return self.status == "approved"


class ApprovalRegistry:
    """Wakes up phases waiting on a decision.

    Keyed by approval id. A decision recorded while no waiter exists (for example
    approved through the API between a crash and a restart) is not lost: waiters
    always re-read the row before sleeping.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}

    def waiter(self, approval_id: str) -> asyncio.Event:
        return self._waiters.setdefault(approval_id, asyncio.Event())

    def notify(self, approval_id: str) -> None:
        event = self._waiters.get(approval_id)
        if event is not None:
            event.set()

    def release(self, approval_id: str) -> None:
        self._waiters.pop(approval_id, None)

    def pending_count(self) -> int:
        return len(self._waiters)


registry = ApprovalRegistry()


async def request(
    database: Database,
    store: EventStore,
    *,
    job_id: str,
    action: str,
    phase_id: int | None = None,
    agent: str | None = None,
    detail: str | None = None,
    risk: str = "high",
    auto_approve: bool = False,
    kind: str = "phase",
    tool_call_id: str | None = None,
) -> str:
    """Create a gate. In yolo mode it is recorded already-approved, not skipped.

    Recording the auto-approval keeps the audit trail honest about what ran
    without review, which is the point of having modes at all.

    ``kind`` separates a gate on a whole phase from a gate on a single command. Both
    carry the same ``phase_id``, and ``latest_for_phase`` must not mistake one for the
    other — a declined command would otherwise look like a declined phase after a
    restart.

    ``tool_call_id`` links the gate to the command row in the *same* commit. Stamped
    afterwards it is a separate write, and between the two the gate is already visible
    while the row it belongs to still reads ``approval_id = null`` — a window a poller
    can land in, and one that made a test flake before it made anything worse.
    """
    approval_id = uuid.uuid4().hex[:10]
    status = "approved" if auto_approve else "pending"

    async with database.transaction() as conn:
        await conn.execute(
            "insert into approvals(id,job_id,phase_id,agent,action,detail,risk,status,auto,kind,created_at,decided_at)"
            " values(?,?,?,?,?,?,?,?,?,?,unixepoch('subsec'),"
            + ("unixepoch('subsec')" if auto_approve else "null")
            + ")",
            (approval_id, job_id, phase_id, agent, action, detail, risk, status, int(auto_approve), kind),
        )
        if tool_call_id is not None:
            await conn.execute(
                "update tool_calls set approval_id=? where id=?", (approval_id, tool_call_id)
            )
    await store.record(
        job_id,
        "approval",
        {
            "approval_id": approval_id,
            "action": action,
            "detail": detail,
            "risk": risk,
            "status": status,
            "auto": auto_approve,
            "phase_id": phase_id,
            "kind": kind,
        },
        source=agent,
    )
    log.info(
        "approval recorded",
        extra={
            "job_id": job_id,
            "approval_id": approval_id,
            "status": status,
            "auto": auto_approve,
            "kind": kind,
        },
    )
    return approval_id


async def read(database: Database, approval_id: str) -> dict[str, Any] | None:
    row = await database.fetch_one("select * from approvals where id=?", (approval_id,))
    return dict(row) if row else None


async def decide(
    database: Database,
    store: EventStore,
    *,
    job_id: str,
    approval_id: str,
    decision: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """Record an operator decision and wake any waiting phase.

    Returns None when there is no pending approval to decide, so the caller can
    answer 404 rather than silently succeeding.
    """
    updated = await database.execute(
        "update approvals set status=?,decided_at=unixepoch('subsec'),decision_note=?"
        " where id=? and job_id=? and status='pending'",
        (decision, note, approval_id, job_id),
    )
    if not updated:
        return None

    await store.record(
        job_id,
        "approval",
        {"approval_id": approval_id, "status": decision, "note": note},
        source="operator",
    )
    registry.notify(approval_id)
    log.info("approval decided", extra={"job_id": job_id, "approval_id": approval_id, "decision": decision})
    return await read(database, approval_id)


async def wait_for(
    database: Database,
    approval_id: str,
    *,
    cancelled: asyncio.Event | None = None,
    poll_interval: float = 5.0,
) -> Decision:
    """Block until the approval is decided.

    Re-reads the row before every sleep, so a decision made while no waiter was
    registered (across a restart, or by the CLI) is picked up. ``poll_interval`` is
    a safety net for a missed notification, not the primary mechanism — the
    registry event is what normally wakes this up promptly.
    """
    waiter = registry.waiter(approval_id)
    try:
        while True:
            row = await read(database, approval_id)
            if row is None:
                raise ApprovalRejected(approval_id, "approval record disappeared")
            if row["status"] != "pending":
                return Decision(status=row["status"], note=row["decision_note"], auto=bool(row["auto"]))

            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()

            waiter.clear()
            wakeups: list[asyncio.Future[Any]] = [asyncio.ensure_future(waiter.wait())]
            if cancelled is not None:
                wakeups.append(asyncio.ensure_future(cancelled.wait()))
            try:
                done, pending = await asyncio.wait(
                    wakeups, timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for future in wakeups:
                    future.cancel()
            del done, pending
    finally:
        registry.release(approval_id)


async def pending_for_phase(database: Database, phase_id: int) -> str | None:
    """The open phase gate blocking a phase, if any. Used to re-attach after a restart."""
    return await database.fetch_value(
        "select id from approvals where phase_id=? and status='pending' and kind='phase'"
        " order by created_at limit 1",
        (phase_id,),
    )


async def latest_for_phase(database: Database, phase_id: int) -> dict[str, Any] | None:
    """The most recent *phase* gate for a phase, whatever its status.

    A restart must distinguish "already approved while we were down" from "never
    gated". Looking only for *pending* approvals would create a second gate and
    ask the operator to approve the same work twice. Restricted to ``kind='phase'``
    because a phase also accumulates a gate per risky command, and the last of those
    says nothing about whether the phase itself was approved.
    """
    row = await database.fetch_one(
        "select * from approvals where phase_id=? and kind='phase'"
        " order by created_at desc, rowid desc limit 1",
        (phase_id,),
    )
    return dict(row) if row else None
