"""Cursor-based event streaming shared by the WebSocket and SSE endpoints.

Both transports need identical semantics — replay from a cursor, then follow live,
never gap, never duplicate — so the logic lives here once and each endpoint only
handles framing.

Ordering matters: the subscription is opened *before* history is read. The v1
WebSocket handler read history first and registered its queue afterwards, so any
event recorded in that window was lost with no way to notice. Subscribing first
means the worst case is a duplicate id, which the cursor filters.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.events import Event, EventStore
from app.logging_setup import get_logger

log = get_logger("agent_hub.streams")

#: Statuses after which no further events can arrive, so the stream may close.
#: 'paused' and 'blocked' are deliberately absent — those jobs are still live.
TERMINAL_STATUSES = frozenset({"complete", "error", "stopped"})

#: How long to keep following a job whose *row* is terminal but whose terminal
#: *event* has not been recorded yet. Writers set the column first and record the
#: event last (see ``stop_job``), and the gap spans a task cancellation, so this
#: has to comfortably exceed the engine's 10s cancel wait. It only elapses when a
#: process died between those two writes, in which case the event never arrives.
TERMINAL_EVENT_GRACE = 15.0

_REPLAY_BATCH = 1000


_TERMINAL_EVENT_SQL = (
    "select 1 from events where job_id=? and kind='status'"
    f" and json_extract(payload,'$.status') in ({','.join('?' * len(TERMINAL_STATUSES))})"
)


async def _job_status(store: EventStore, job_id: str) -> str | None:
    return await store.db.fetch_value("select status from jobs where id=?", (job_id,))


async def _terminal_event_recorded(store: EventStore, job_id: str) -> bool:
    """Is a terminal status event already durable, wherever the cursor sits?"""
    return await store.db.exists(_TERMINAL_EVENT_SQL, (job_id, *sorted(TERMINAL_STATUSES)))


def _is_terminal(event: Event) -> bool:
    return event.kind == "status" and event.payload.get("status") in TERMINAL_STATUSES


def _drain(subscription) -> None:
    while True:
        try:
            subscription.queue.get_nowait()
        except asyncio.QueueEmpty:
            return


async def job_event_stream(
    store: EventStore,
    job_id: str,
    after: int = 0,
    keepalive: float | None = None,
) -> AsyncIterator[Event | None]:
    """Yield events for ``job_id`` after cursor ``after``, then follow live.

    Yields ``None`` as a heartbeat every ``keepalive`` seconds when idle, so the
    caller can emit a transport-level ping and detect dead peers. Returns once the
    job's terminal status event has been delivered.
    """
    cursor = max(0, after)
    loop = asyncio.get_running_loop()
    closed = False  # has the terminal status event been handed to the client yet?

    async with store.broker.subscribe(job_id) as subscription:
        # 1. Replay everything already durable, in batches.
        while True:
            batch = await store.history(job_id, after=cursor, limit=_REPLAY_BATCH)
            for event in batch:
                yield event
                cursor = event.id
                closed = closed or _is_terminal(event)
            if len(batch) < _REPLAY_BATCH:
                break
        if closed:
            return

        # 2. If the job finished while we were replaying, flush whatever landed in
        #    the meantime. Without this drain the terminal status event itself could
        #    be swallowed by the race.
        #
        #    A terminal `jobs.status` is *not* proof the terminal event is durable:
        #    every writer updates the column before recording the event, and
        #    `stop_job` cancels the running task in between. Closing on the column
        #    alone drops the one event clients are waiting for — a stopped job that
        #    reads 'planning' until the operator reloads. So close on the event, and
        #    fall through to the live loop under a deadline if it has not landed.
        deadline: float | None = None
        if (await _job_status(store, job_id)) in TERMINAL_STATUSES:
            for event in await store.history(job_id, after=cursor):
                yield event
                cursor = event.id
                closed = closed or _is_terminal(event)
            if closed or await _terminal_event_recorded(store, job_id):
                # Either the terminal event was just delivered, or it is already
                # durable *behind* this cursor — a client reconnecting with the
                # cursor it held when the job finished. Nothing more can arrive, so
                # closing on the grace timer instead would leave the UI claiming to
                # be live for 15s after a finished job was reopened.
                return
            deadline = loop.time() + TERMINAL_EVENT_GRACE

        # 3. Follow live.
        while True:
            if subscription.lagged:
                # Buffered events may be arbitrarily stale; the durable log is
                # authoritative. Reset the flag first so events arriving during
                # the catch-up read are not silently cleared afterwards.
                subscription.lagged = False
                _drain(subscription)
                caught_up = await store.history(job_id, after=cursor)
                for event in caught_up:
                    yield event
                    cursor = event.id
                    if _is_terminal(event):
                        return
                continue

            wait = keepalive
            if deadline is not None:
                remaining = max(0.0, deadline - loop.time())
                wait = remaining if wait is None else min(wait, remaining)

            try:
                event = (
                    await asyncio.wait_for(subscription.get(), wait)
                    if wait is not None
                    else await subscription.get()
                )
            except TimeoutError:
                if deadline is not None and loop.time() >= deadline:
                    log.warning(
                        "closing stream: job is terminal but recorded no terminal event",
                        extra={"job_id": job_id, "cursor": cursor},
                    )
                    return
                yield None  # heartbeat
                continue

            if event.id <= cursor:
                continue  # already replayed from history
            yield event
            cursor = event.id
            if _is_terminal(event):
                return
