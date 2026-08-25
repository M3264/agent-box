"""Durable event log plus in-process fan-out.

The database is the source of truth; the broker is only a latency optimisation.
Every subscriber tracks a cursor, so a subscriber that falls behind or drops can
always rebuild exact state from ``events`` by id. That property is what lets both
transports (WebSocket for the UI, SSE for the CLI) share one code path.

Two defects in the v1 implementation are fixed here:

- Cleanup used ``"queue" in locals()`` inside a single-iteration for loop and
  could pop a job's entire subscriber set, silently detaching other live
  clients. Subscriptions are now scoped by an async context manager.
- Subscriber queues were unbounded, so one stalled client grew memory without
  limit. Queues are bounded; overflow marks the subscription lagged and the
  consumer resyncs from the durable log rather than losing events.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from app.db import Database, db
from app.logging_setup import get_logger

log = get_logger("agent_hub.events")

DEFAULT_QUEUE_SIZE = 512


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    job_id: str
    created_at: float
    kind: str
    source: str | None
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Event:
        raw = row["payload"]
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # Never let one malformed row break a whole replay.
            payload = {"_unparsed": raw}
        return cls(
            id=int(row["id"]),
            job_id=row["job_id"],
            created_at=float(row["created_at"]),
            kind=row["kind"],
            source=row["source"],
            payload=payload if isinstance(payload, dict) else {"value": payload},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "source": self.source,
            "payload": self.payload,
        }


@dataclass(eq=False)
class Subscription:
    """A bounded feed of events for one job.

    ``lagged`` is set when the queue overflowed. The consumer should then discard
    what it holds and re-read from ``EventStore.history`` at its last cursor.

    ``eq=False`` keeps identity equality and hashing: the broker holds
    subscriptions in a set, and two clients watching the same job are distinct
    subscriptions even though their fields compare equal.
    """

    job_id: str
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(DEFAULT_QUEUE_SIZE))
    lagged: bool = False

    async def get(self) -> Event:
        return await self.queue.get()

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            if not self.lagged:
                log.warning(
                    "subscriber lagged; will resync from the event log",
                    extra={"job_id": self.job_id, "queue_size": self.queue.qsize()},
                )
            self.lagged = True


class EventBroker:
    """Fan-out of freshly recorded events to live subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[Subscription]] = {}

    @asynccontextmanager
    async def subscribe(self, job_id: str) -> AsyncIterator[Subscription]:
        subscription = Subscription(job_id=job_id)
        self._subscribers.setdefault(job_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            # Remove only this subscription, and drop the job key only once its
            # own set is empty.
            peers = self._subscribers.get(job_id)
            if peers is not None:
                peers.discard(subscription)
                if not peers:
                    self._subscribers.pop(job_id, None)

    def publish(self, event: Event) -> None:
        for subscription in tuple(self._subscribers.get(event.job_id, ())):
            subscription.offer(event)

    def subscriber_count(self, job_id: str | None = None) -> int:
        if job_id is not None:
            return len(self._subscribers.get(job_id, ()))
        return sum(len(peers) for peers in self._subscribers.values())


class EventStore:
    """Append-only event log with fan-out on commit."""

    def __init__(self, database: Database, broker: EventBroker) -> None:
        self.db = database
        self.broker = broker

    async def record(
        self,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> Event:
        body = json.dumps(payload or {}, default=str)
        async with self.db.transaction() as conn:
            async with conn.execute(
                "insert into events(job_id,created_at,kind,source,payload)"
                " values(?,unixepoch('subsec'),?,?,?) returning id, created_at",
                (job_id, kind, source, body),
            ) as cursor:
                row = await cursor.fetchone()
            await conn.execute(
                "update jobs set updated_at=unixepoch('subsec') where id=?", (job_id,)
            )

        event = Event(
            id=int(row["id"]),
            job_id=job_id,
            created_at=float(row["created_at"]),
            kind=kind,
            source=source,
            payload=payload or {},
        )
        # Published only after commit, so a subscriber can never observe an event
        # that a crash would have rolled back.
        self.broker.publish(event)
        return event

    async def history(self, job_id: str, after: int = 0, limit: int = 1000) -> list[Event]:
        rows = await self.db.fetch_all(
            "select * from events where job_id=? and id>? order by id limit ?",
            (job_id, max(0, after), max(1, min(limit, 5000))),
        )
        return [Event.from_row(row) for row in rows]

    async def latest_id(self, job_id: str) -> int:
        return int(await self.db.fetch_value(
            "select coalesce(max(id),0) from events where job_id=?", (job_id,), default=0
        ))


broker = EventBroker()
events = EventStore(db, broker)
