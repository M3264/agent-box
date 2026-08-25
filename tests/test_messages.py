"""Operator messages reaching the team.

PLAN.md §1 calls the lead conversation "the primary workflow". In v1 the endpoint
wrote a row and emitted an event, and ``run_team`` never read the table — one
message was sent in the service's lifetime and never consumed. The engine now
drains unconsumed messages before each phase, so these tests check the message
lands in a prompt, not merely in the database.
"""

from __future__ import annotations

import asyncio

import httpx

from app.db import db
from app.deps import engine
from tests.conftest import (
    PLAN_MARKER,
    FakeProvider,
    event_kinds,
    wait_for_job,
)

GUIDANCE = "Use Postgres, not SQLite, and say so explicitly."


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
