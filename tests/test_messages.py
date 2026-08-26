"""Operator messages reaching the team.

PLAN.md §1 calls the lead conversation "the primary workflow". In v1 the endpoint
wrote a row and emitted an event, and ``run_team`` never read the table — one
message was sent in the service's lifetime and never consumed. The engine now
drains unconsumed messages before each phase, so these tests check the message
lands in a prompt, not merely in the database.

The second half of the file is about the window between typing a message and the team
reading it. That window used to be a black box: the message was immutable, so a typo
could only be followed by a second message contradicting the first — which is exactly
the kind of thing a model resolves badly — and there was no way to say "now, not at
the next phase". Edit, cancel and expedite all live in that window, and all three stop
working the instant ``consumed_at`` is set. That boundary is what these tests pin.
"""

from __future__ import annotations

import asyncio

import httpx

from app.db import db
from app.deps import engine
from tests.conftest import (
    ONE_PHASE,
    PLAN_MARKER,
    FakeProvider,
    event_kinds,
    says,
    tool,
    wait_for_job,
)

GUIDANCE = "Use Postgres, not SQLite, and say so explicitly."

#: What the tool loop prefixes a mid-phase interjection with. Asserted against rather
#: than "the message is somewhere in the prompts", because *how* it arrives is the
#: behaviour: as the operator speaking, not as tool output the model may read as its own.
INTERJECTION = "has just sent this while you were working"


async def messages_for(job_id: str) -> list[dict]:
    rows = await db.fetch_all("select * from job_messages where job_id=? order by id", (job_id,))
    return [dict(row) for row in rows]


async def test_message_is_consumed_into_the_next_phase_prompt(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Posted during planning, so the next phase to drain is the first work phase."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    created = await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    assert created.status_code == 201
    assert created.json()["role"] == "operator"
    assert created.json()["consumed_at"] is None

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    design_prompt = next(p for p in provider.prompts if "Your phase: Design it" in p)
    assert GUIDANCE in design_prompt
    assert "Operator guidance" in design_prompt

    stored = await messages_for(job_id)
    assert len(stored) == 1
    assert stored[0]["consumed_at"] is not None, "the message was never drained"

    guidance_events = await event_kinds(job_id, "guidance")
    assert [event["messages"] for event in guidance_events] == [[GUIDANCE]]

    later = [p for p in provider.prompts if "Your phase: Build it" in p]
    assert GUIDANCE not in later[0], "a consumed message must not be re-injected"


async def test_message_reaches_the_manager_at_plan_time(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """PLAN.md §4: the manager sees operator guidance at plan time and can replan.

    Planning starts the instant a job is created, so the only way to get a message
    in front of it is to interrupt planning first — which doubles as a check that a
    message posted to a parked job survives the restart.
    """
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    await engine.shutdown(grace=2)
    assert (await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})).status_code == 201

    provider.open_gate()
    await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    plan_prompts = [p for p in provider.prompts if PLAN_MARKER in p]
    assert len(plan_prompts) == 2, "planning ran once before the interrupt and once after"
    assert GUIDANCE not in plan_prompts[0]
    assert GUIDANCE in plan_prompts[1]
    assert (await messages_for(job_id))[0]["consumed_at"] is not None


async def test_messages_list_shows_consumption_state(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """What the composer needs to show "delivered" versus "queued"."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    await client.post(f"/api/jobs/{job_id}/messages", json={"content": "first"})
    await client.post(f"/api/jobs/{job_id}/messages", json={"content": "second"})

    pending = (await client.get(f"/api/jobs/{job_id}/messages")).json()
    assert [m["content"] for m in pending] == ["first", "second"]
    assert all(m["consumed_at"] is None for m in pending)

    provider.open_gate()
    await wait_for_job(job_id, "complete")

    consumed = (await client.get(f"/api/jobs/{job_id}/messages")).json()
    assert all(m["consumed_at"] is not None for m in consumed)

    # Both were drained together, so both land in the same phase's prompt.
    design_prompt = next(p for p in provider.prompts if "Your phase: Design it" in p)
    assert "- first" in design_prompt
    assert "- second" in design_prompt


async def test_message_to_a_finished_job_is_rejected(client: httpx.AsyncClient, job) -> None:
    """Accepting it would silently drop it — nothing is left to drain the queue."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    response = await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    assert response.status_code == 409
    assert "complete" in response.json()["detail"]
    assert await messages_for(job_id) == []


async def test_blank_message_is_rejected(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    assert (
        await client.post(f"/api/jobs/{job_id}/messages", json={"content": "   "})
    ).status_code == 422
    assert (await client.post("/api/jobs/nope/messages", json={"content": "hi"})).status_code == 404
    await wait_for_job(job_id, "complete")


async def test_operator_message_is_recorded_as_an_event(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The conversation tab is built from the event log, so the post must appear there."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    operator_events = [e for e in await event_kinds(job_id, "message") if e.get("operator")]
    assert [e["content"] for e in operator_events] == [GUIDANCE]

    provider.open_gate()
    await wait_for_job(job_id, "complete")


# ------------------------------------------------- the window before it is read


async def park_at_planning(job, provider: FakeProvider) -> str:
    """A job held open at its first provider call, so nothing has drained yet."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()
    return job_id


async def park_in_the_work_phase(job, provider: FakeProvider) -> str:
    """A job held open *inside* a running phase, mid tool loop.

    One work phase and a two-turn script, gated after planning: the loop is parked on
    turn 1 with a turn 2 still to come, which is the only place an interjection has
    somewhere to land.
    """
    provider.plan = ONE_PHASE
    provider.tool_script = [
        tool("run", command="echo still-working"),
        says("Done, and I took the interruption into account."),
    ]
    provider.gate = asyncio.Event()
    provider.gate_after = 1
    job_id = await job()
    await provider.wait_until_blocked()
    return job_id


async def message_actions(job_id: str) -> list[str | None]:
    return [payload.get("action") for payload in await event_kinds(job_id, "message")]


async def test_a_queued_message_can_be_corrected_before_the_team_reads_it(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The alternative was a second message contradicting the first."""
    job_id = await park_at_planning(job, provider)

    created = await client.post(f"/api/jobs/{job_id}/messages", json={"content": "Use MySQL."})
    message_id = created.json()["id"]
    assert created.json()["delivery"] == "boundary", "the polite default"

    edited = await client.patch(
        f"/api/jobs/{job_id}/messages/{message_id}", json={"content": GUIDANCE}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["content"] == GUIDANCE
    assert edited.json()["delivery"] == "boundary", "editing the text must not change the timing"

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    design = next(p for p in provider.prompts if "Your phase: Design it" in p)
    assert GUIDANCE in design
    assert "MySQL" not in design, "the team was handed a message the operator had corrected"
    assert "edited" in await message_actions(job_id)

    too_late = await client.patch(
        f"/api/jobs/{job_id}/messages/{message_id}", json={"content": "third thoughts"}
    )
    assert too_late.status_code == 409, "a delivered message cannot be unsaid"
    assert "already read" in too_late.json()["detail"]


async def test_a_cancelled_message_is_never_delivered_but_still_happened(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Cancelled, not deleted: a row that vanishes makes the transcript lie."""
    job_id = await park_at_planning(job, provider)

    created = await client.post(
        f"/api/jobs/{job_id}/messages", json={"content": "Actually, use MySQL."}
    )
    message_id = created.json()["id"]
    cancelled = await client.delete(f"/api/jobs/{job_id}/messages/{message_id}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"] is True

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert not provider.prompts_containing("MySQL"), "a withdrawn message reached the team"
    row = (await messages_for(job_id))[0]
    assert row["cancelled_at"] is not None
    assert row["consumed_at"] is None, "a cancelled message must not also count as delivered"
    assert "cancelled" in await message_actions(job_id), "the withdrawal is itself an event"
    assert await event_kinds(job_id, "guidance") == [], "nothing was handed over"

    assert (
        await client.delete(f"/api/jobs/{job_id}/messages/{message_id}")
    ).status_code == 409, "cancelling twice is not idempotent success; the second is a mistake"


async def test_an_immediate_message_lands_in_the_running_phase_next_turn(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """"Stop doing that" is worth nothing if it waits for the phase that is doing it.

    Delivered between turns rather than mid-turn: work already in flight is not killed,
    which is as fast as this can be honoured without throwing away a command's output.
    """
    job_id = await park_in_the_work_phase(job, provider)

    now = "Stop touching the database and write the tests first."
    urgent = await client.post(
        f"/api/jobs/{job_id}/messages", json={"content": now, "delivery": "immediate"}
    )
    assert urgent.status_code == 201 and urgent.json()["delivery"] == "immediate"
    await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    injected = provider.prompts_containing(INTERJECTION)
    assert injected, f"the immediate message never reached the running phase: {provider.prompts}"
    assert now in injected[0]
    assert GUIDANCE not in injected[0], (
        "a boundary message was expedited by proximity to an immediate one"
    )

    guidance = await event_kinds(job_id, "guidance")
    immediate = [payload for payload in guidance if payload.get("immediate")]
    assert [payload["messages"] for payload in immediate] == [[now]]
    assert immediate[0]["turn"] == 2, "the interjection is announced at the turn it landed on"

    # The queued one still arrives, at the next boundary — which with a single work
    # phase is the synthesis. Both are consumed exactly once.
    assert provider.prompts_containing(GUIDANCE), "the boundary message was lost"
    assert all(row["consumed_at"] is not None for row in await messages_for(job_id))


async def test_a_queued_message_can_be_sent_now_without_retyping_it(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The "send it now" button: changes when, never what."""
    job_id = await park_in_the_work_phase(job, provider)

    created = await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    message_id = created.json()["id"]

    expedited = await client.patch(
        f"/api/jobs/{job_id}/messages/{message_id}", json={"delivery": "immediate"}
    )
    assert expedited.status_code == 200, expedited.text
    assert expedited.json()["delivery"] == "immediate"
    assert expedited.json()["content"] == GUIDANCE, "expediting rewrote the message"

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    injected = provider.prompts_containing(INTERJECTION)
    assert injected and GUIDANCE in injected[0]
    assert "expedited" in await message_actions(job_id)
    assert await db.fetch_value(
        "select delivery from job_messages where id=?", (message_id,)
    ) == "immediate"


async def test_message_controls_refuse_what_they_cannot_honour(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Every refusal here is a different thing for the composer to say."""
    job_id = await park_at_planning(job, provider)
    created = await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    message_id = created.json()["id"]

    assert (
        await client.patch(f"/api/jobs/{job_id}/messages/{message_id}", json={})
    ).status_code == 422, "a patch that changes nothing is a client bug"
    assert (
        await client.patch(f"/api/jobs/{job_id}/messages/{message_id}", json={"content": "  "})
    ).status_code == 422
    assert (
        await client.patch(f"/api/jobs/{job_id}/messages/{message_id}", json={"delivery": "soon"})
    ).status_code == 422, "delivery is a closed vocabulary"
    assert (
        await client.patch(f"/api/jobs/{job_id}/messages/9999", json={"content": "x"})
    ).status_code == 404
    assert (await client.delete(f"/api/jobs/{job_id}/messages/9999")).status_code == 404
    assert (
        await client.patch(f"/api/jobs/nope/messages/{message_id}", json={"content": "x"})
    ).status_code == 404

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"
