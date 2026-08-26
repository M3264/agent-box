"""Questions an agent asks the operator, and the answers that unblock it.

An approval already existed, and it is the wrong shape for this. An approval is a
veto on an action the agent has already chosen — the only answers are yes and no,
and "no" tells the model nothing except to stop. What was missing is the case where
the agent does not know something only the operator does: which of two directions
they want, a target that is not in the repo, which reading of an ambiguous
acceptance criterion was meant. Faced with that, a model guesses, and the guess
surfaces at the end as a paragraph explaining what it assumed.

So: durable first, in-memory second, exactly like ``approvals``. The row plus the
phase's ``blocked_on_question`` status are the truth and the ``asyncio.Event`` is
only a wake-up, which is what lets a restart re-attach to a question already on
screen instead of asking it a second time.

Two deliberate differences from approvals:

- **A question can time out.** An approval waits forever because the operator
  explicitly gated that action and the work must not proceed without them. A
  question is the agent's own initiative, and a job that hangs for a day because
  nobody was watching is worse than one that proceeded on its best guess — so the
  timeout is durable, recorded, and told to the model in those words.
- **Options carry stable values.** The agent writes labels, which are prose. The
  value is derived here so that "which option did the operator pick" survives an
  agent that rephrases its own labels between turns.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.events import EventStore
from app.logging_setup import get_logger
from app.models import TERMINAL_JOB_STATUSES

log = get_logger("agent_hub.questions")

#: A question on a job that has ended is waiting on nobody, so the inbox query filters
#: on this rather than trusting every terminal path to have closed its rows.
_TERMINAL = tuple(sorted(TERMINAL_JOB_STATUSES))
_PLACEHOLDERS = ",".join("?" * len(_TERMINAL))

#: Cap on how many choices an agent may offer. Past this it is not asking a
#: question, it is asking the operator to do its job.
MAX_OPTIONS = 8

#: Cap on the length of one option label, so a model cannot smuggle a paragraph
#: into a button.
MAX_LABEL = 120

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class Answer:
    """What ``wait_for`` resolved to.

    ``status`` is one of answered | cancelled | timeout. ``text`` is what should be
    handed back to the model, phrased for it rather than for a person.
    """

    status: str
    text: str
    chosen: str | None = None

    @property
    def answered(self) -> bool:
        return self.status == "answered"


class QuestionRegistry:
    """Wakes up agents waiting on an answer. Keyed by question id."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}

    def waiter(self, question_id: str) -> asyncio.Event:
        return self._waiters.setdefault(question_id, asyncio.Event())

    def notify(self, question_id: str) -> None:
        event = self._waiters.get(question_id)
        if event is not None:
            event.set()

    def release(self, question_id: str) -> None:
        self._waiters.pop(question_id, None)

    def pending_count(self) -> int:
        return len(self._waiters)


registry = QuestionRegistry()


def normalise_options(raw: Any) -> list[dict[str, str]]:
    """Coerce whatever the model sent into ``[{value, label, detail}]``.

    Models are inconsistent here in a way that is not worth arguing with: some send
    a list of strings, some a list of objects, some an object keyed by label. All
    three mean the same thing, so all three are accepted rather than refused with a
    schema error the model will only half-learn from.

    Values are slugified labels, deduped by suffix. A label that slugifies to
    nothing (an emoji, CJK text) falls back to its position, so every option always
    has a usable value.
    """
    entries: list[Any]
    if raw is None:
        return []
    if isinstance(raw, dict):
        entries = [{"label": key, "detail": value} for key, value in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        return []

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries[:MAX_OPTIONS]):
        if isinstance(entry, str):
            label, detail = entry.strip(), ""
        elif isinstance(entry, dict):
            label = str(entry.get("label") or entry.get("value") or entry.get("name") or "").strip()
            detail = str(entry.get("detail") or entry.get("description") or "").strip()
        else:
            continue
        if not label:
            continue
        label = label[:MAX_LABEL]
        base = _SLUG.sub("-", label.lower()).strip("-") or f"option-{index + 1}"
        value = base
        suffix = 2
        while value in seen:
            value = f"{base}-{suffix}"
            suffix += 1
        seen.add(value)
        options.append({"value": value, "label": label, "detail": detail})
    return options


async def request(
    database: Database,
    store: EventStore,
    *,
    job_id: str,
    agent: str,
    question: str,
    phase_id: int | None = None,
    detail: str | None = None,
    options: Any = None,
    allow_free_text: bool = True,
) -> tuple[str, list[dict[str, str]]]:
    """Record a question and return its id and the normalised options.

    The options are returned rather than only stored because the caller has to tell
    the model which values it may be answered with, and those values are derived
    here.
    """
    question_id = uuid.uuid4().hex[:10]
    normalised = normalise_options(options)

    await database.execute(
        "insert into questions(id,job_id,phase_id,agent,question,detail,options,"
        "allow_free_text,status,created_at)"
        " values(?,?,?,?,?,?,?,?,'pending',unixepoch('subsec'))",
        (
            question_id,
            job_id,
            phase_id,
            agent,
            question,
            detail,
            json.dumps(normalised),
            # An agent that offers no options is asking an open question whatever it
            # claimed, so free text is forced on rather than leaving a question that
            # cannot be answered at all.
            int(bool(allow_free_text) or not normalised),
        ),
    )
    await store.record(
        job_id,
        "question",
        {
            "question_id": question_id,
            "question": question,
            "detail": detail,
            "options": normalised,
            "allow_free_text": bool(allow_free_text) or not normalised,
            "status": "pending",
            "phase_id": phase_id,
        },
        source=agent,
    )
    log.info(
        "question asked",
        extra={
            "job_id": job_id,
            "question_id": question_id,
            "agent": agent,
            "options": len(normalised),
        },
    )
    return question_id, normalised


async def read(database: Database, question_id: str) -> dict[str, Any] | None:
    row = await database.fetch_one("select * from questions where id=?", (question_id,))
    return dict(row) if row else None


async def answer(
    database: Database,
    store: EventStore,
    *,
    job_id: str,
    question_id: str,
    text: str | None = None,
    chosen: str | None = None,
) -> dict[str, Any] | None:
    """Record the operator's answer and wake the waiting agent.

    Returns None when there is no pending question to answer, so the caller can
    answer 404 instead of silently succeeding. When ``chosen`` names an option and
    no free text was typed, the option's label becomes the answer text — the model
    should receive words, not a slug it never saw.
    """
    row = await read(database, question_id)
    if row is None or row["job_id"] != job_id or row["status"] != "pending":
        return None

    options = _options_of(row)
    picked = next((entry for entry in options if entry["value"] == chosen), None) if chosen else None
    if chosen and picked is None:
        # An unknown value is a bug in the caller, not an answer. Refusing it keeps
        # `chosen` meaning "one of the options the agent offered".
        return None

    resolved = (text or "").strip() or (picked["label"] if picked else "")
    if not resolved:
        return None

    updated = await database.execute(
        "update questions set status='answered',answer=?,chosen=?,"
        "answered_at=unixepoch('subsec') where id=? and status='pending'",
        (resolved, picked["value"] if picked else None, question_id),
    )
    if not updated:
        return None

    await store.record(
        job_id,
        "question",
        {
            "question_id": question_id,
            "question": row["question"],
            "status": "answered",
            "answer": resolved,
            "chosen": picked["value"] if picked else None,
        },
        source="operator",
    )
    registry.notify(question_id)
    log.info("question answered", extra={"job_id": job_id, "question_id": question_id})
    return await read(database, question_id)


async def cancel_open(
    database: Database, store: EventStore, *, job_id: str, reason: str
) -> list[str]:
    """Close every pending question on a job. Called when the job stops or errors.

    Without this a stopped job leaves questions in the operator's inbox that no
    agent is listening for any more — the inbox equivalent of a dangling pointer.
    """
    rows = await database.fetch_all(
        "select id from questions where job_id=? and status='pending'", (job_id,)
    )
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    await database.execute(
        f"update questions set status='cancelled',answer=?,"
        f"answered_at=unixepoch('subsec') where id in ({placeholders})",
        (reason, *ids),
    )
    for question_id in ids:
        await store.record(
            job_id,
            "question",
            {"question_id": question_id, "status": "cancelled", "answer": reason},
            source="system",
        )
        registry.notify(question_id)
    log.info("questions cancelled", extra={"job_id": job_id, "count": len(ids), "reason": reason})
    return ids


async def wait_for(
    database: Database,
    store: EventStore,
    question_id: str,
    *,
    job_id: str,
    cancelled: asyncio.Event | None = None,
    timeout: float | None = None,
    poll_interval: float = 5.0,
) -> Answer:
    """Block until answered, cancelled, or timed out.

    Re-reads the row before every sleep for the same reason approvals do: an answer
    posted while no waiter was registered — across a restart, or by another worker —
    must still be picked up.

    The timeout is measured against the row's ``created_at``, not against when this
    call started. A question asked before a restart has already been on screen for
    however long the process was down, and restarting the clock would silently give
    it a fresh full timeout every time the service bounced.
    """
    waiter = registry.waiter(question_id)
    try:
        while True:
            row = await read(database, question_id)
            if row is None:
                return Answer(
                    status="cancelled",
                    text="The question record disappeared; proceed on your own judgement.",
                )
            if row["status"] != "pending":
                return _resolved(row)

            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()

            if timeout is not None:
                age = await _now(database) - float(row["created_at"])
                if age >= timeout:
                    return await _time_out(database, store, job_id=job_id, question_id=question_id)
                remaining: float | None = min(poll_interval, timeout - age)
            else:
                remaining = poll_interval

            waiter.clear()
            wakeups: list[asyncio.Future[Any]] = [asyncio.ensure_future(waiter.wait())]
            if cancelled is not None:
                wakeups.append(asyncio.ensure_future(cancelled.wait()))
            try:
                await asyncio.wait(
                    wakeups, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for future in wakeups:
                    future.cancel()
    finally:
        registry.release(question_id)


async def pending_for_phase(database: Database, phase_id: int) -> str | None:
    """The open question blocking a phase, for re-attaching after a restart."""
    return await database.fetch_value(
        "select id from questions where phase_id=? and status='pending'"
        " order by created_at limit 1",
        (phase_id,),
    )


async def for_job(database: Database, job_id: str) -> list[dict[str, Any]]:
    rows = await database.fetch_all(
        "select * from questions where job_id=? order by created_at, rowid", (job_id,)
    )
    return [_public(dict(row)) for row in rows]


async def pending_across_jobs(database: Database, limit: int = 200) -> list[dict[str, Any]]:
    """Every open question on a live job, newest last, with the job's task for context.

    Joined here rather than in the API layer because the operator's inbox is useless
    without knowing which job is asking. Terminal jobs are excluded even though every
    path that ends a job calls ``cancel_open``: a question no agent is listening for is
    the inbox equivalent of a dangling pointer, and one row missed by one of those paths
    would be indistinguishable from a live question.
    """
    rows = await database.fetch_all(
        "select q.*, j.task as job_task, j.status as job_status, p.name as phase_name"
        " from questions q join jobs j on j.id=q.job_id"
        " left join phases p on p.id=q.phase_id"
        f" where q.status='pending' and j.status not in ({_PLACEHOLDERS})"
        " order by q.created_at limit ?",
        (*_TERMINAL, limit),
    )
    return [_public(dict(row)) for row in rows]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Parse `options` back into a list so callers never see the JSON blob."""
    row["options"] = _options_of(row)
    row["allow_free_text"] = bool(row.get("allow_free_text", 1))
    return row


def _options_of(row: Any) -> list[dict[str, str]]:
    """Options as a list, whether the row still holds the stored JSON or a parsed list."""
    raw = row["options"]
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _resolved(row: dict[str, Any]) -> Answer:
    status = str(row["status"])
    text = str(row["answer"] or "")
    if status == "answered":
        return Answer(status=status, text=text, chosen=row["chosen"])
    if status == "timeout":
        return Answer(
            status=status,
            text=(
                "The operator did not answer in time. Proceed with the most reasonable "
                "assumption and state clearly in your output which assumption you made."
            ),
        )
    return Answer(
        status="cancelled",
        text=text or "The question was cancelled; stop waiting on it.",
    )


async def _now(database: Database) -> float:
    """Wall clock from SQLite, so ages are measured on the same clock as the rows."""
    value = await database.fetch_value("select unixepoch('subsec')")
    return float(value or 0.0)


async def _time_out(
    database: Database, store: EventStore, *, job_id: str, question_id: str
) -> Answer:
    updated = await database.execute(
        "update questions set status='timeout',answered_at=unixepoch('subsec')"
        " where id=? and status='pending'",
        (question_id,),
    )
    if not updated:
        # Answered in the gap between the age check and this write. The answer wins.
        row = await read(database, question_id)
        return _resolved(row) if row else Answer(status="timeout", text="Question vanished.")
    await store.record(
        job_id,
        "question",
        {"question_id": question_id, "status": "timeout"},
        source="system",
    )
    log.info("question timed out", extra={"job_id": job_id, "question_id": question_id})
    return _resolved({"status": "timeout", "answer": None, "chosen": None})
