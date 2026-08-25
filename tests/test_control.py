"""Pause, resume and stop.

Both control paths were broken in v1 in ways that showed up in production:

- **Pause** was an ``asyncio.sleep(0.5)`` spin loop (`runtime.py:47-48`). Job
  ``65b79e8a7a7b`` sat in it for 40.6 hours and burned 7m19s of CPU doing nothing.
- **Stop** was only checked *between* phases (`runtime.py:44`), so a stop during a
  180-second provider call did nothing until the call returned on its own.

So these tests care about *how* the waiting and cancelling happen, not just that
the final status is right.
"""

from __future__ import annotations

import asyncio

import httpx

from app.db import db
from app.deps import engine
from app.orchestrator.engine import PAUSE_RECHECK_SECONDS
from tests.conftest import (
    FakeProvider,
    event_kinds,
    phase_rows,
    wait_for_job,
    wait_for_phase,
)


async def pause_at_the_first_phase_boundary(client: httpx.AsyncClient, job, provider: FakeProvider):
    """Park a job on the pause gate: hold planning, pause, then let planning finish."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0  # the planning call itself parks

    job_id = await job()
    await provider.wait_until_blocked()

    response = await client.post(f"/api/jobs/{job_id}/pause")
    assert response.status_code == 200
    assert response.json()["paused"] is True

    provider.open_gate()
    # Planning completes, then the loop hits the gate before running phase 1.
    assert await wait_for_phase(job_id, 0, "complete") == "complete"
    return job_id


async def test_pause_holds_the_job_and_resume_wakes_it_at_once(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Resume latency proves the wake-up is an Event, not a poll.

    ``PAUSE_RECHECK_SECONDS`` is 30s and is only a safety net for a missed
    notification. If resuming took anywhere near that long the gate would be
    polling; if it returns in milliseconds the notification path is doing the work.
    """
    assert PAUSE_RECHECK_SECONDS >= 30, "a short recheck would make this test pass by polling"

    job_id = await pause_at_the_first_phase_boundary(client, job, provider)

    calls_while_paused = len(provider.prompts)
    await asyncio.sleep(0.25)
    assert len(provider.prompts) == calls_while_paused, "a paused job must not run more phases"
    assert await db.fetch_value("select paused from jobs where id=?", (job_id,)) == 1
    assert (
        await db.fetch_value("select status from phases where job_id=? and seq=1", (job_id,))
        == "pending"
    )
    assert [e["status"] for e in await event_kinds(job_id, "status")].count("paused") == 1

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert (await client.post(f"/api/jobs/{job_id}/resume")).json()["paused"] is False
    await wait_for_phase(job_id, 1, "active", "complete")
    latency = loop.time() - started

    assert latency < 2.0, f"resume took {latency:.2f}s — the gate is polling, not waiting"
    assert await wait_for_job(job_id, "complete") == "complete"
    assert all(p["attempts"] == 1 for p in await phase_rows(job_id)), "pausing re-ran a phase"


async def test_pause_survives_a_restart(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The DB flag is the durable truth, so a paused job comes back paused."""
    job_id = await pause_at_the_first_phase_boundary(client, job, provider)

    await engine.shutdown(grace=2)
    assert await db.fetch_value("select paused from jobs where id=?", (job_id,)) == 1

    await engine.recover()
    await asyncio.sleep(0.25)
    assert (
        await db.fetch_value("select status from phases where job_id=? and seq=1", (job_id,))
        == "pending"
    ), "a recovered job must respect the pause flag"

    await client.post(f"/api/jobs/{job_id}/resume")
    assert await wait_for_job(job_id, "complete") == "complete"


async def test_stop_cancels_an_in_flight_provider_call(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The v1 bug: a stop mid-call was ignored until the call returned."""
    provider.gate = asyncio.Event()
    provider.gate_after = 1  # planning lands, 'Design it' parks

    job_id = await job()
    await provider.wait_until_blocked()
    assert await wait_for_phase(job_id, 1, "active") == "active"

    response = await client.post(f"/api/jobs/{job_id}/stop")
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"

    assert provider.cancelled == 1, "the in-flight provider call was not interrupted"
    assert not engine.is_running(job_id)

    phases = {p["seq"]: p for p in await phase_rows(job_id)}
    assert phases[0]["status"] == "complete", "work already committed stays committed"
    assert phases[1]["status"] == "skipped"
    assert phases[1]["error"] == "job stopped"
    assert {row["status"] for row in await db.fetch_all(
        "select status from job_agents where job_id=?", (job_id,)
    )} == {"stopped"}
    assert [e["status"] for e in await event_kinds(job_id, "status")][-1] == "stopped"

    provider.open_gate()


async def test_a_stopped_job_is_not_resumed_by_recovery(job, provider: FakeProvider) -> None:
    """``recover()`` must leave operator decisions alone."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()
    await engine.stop(job_id)

    # engine.stop alone does not mark the job terminal — the endpoint does — so
    # park it the way the endpoint would and confirm recovery skips it.
    await db.execute("update jobs set status='stopped' where id=?", (job_id,))
    provider.open_gate()
    assert job_id not in await engine.recover()


async def test_stopping_twice_is_harmless(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    first = await client.post(f"/api/jobs/{job_id}/stop")
    second = await client.post(f"/api/jobs/{job_id}/stop")
    assert first.json()["status"] == "complete", "a finished job is not retroactively stopped"
    assert second.json() == first.json()


async def test_control_endpoints_ignore_terminal_jobs(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")
    calls_before = len(await event_kinds(job_id, "status"))

    for verb in ("pause", "resume", "stop"):
        body = (await client.post(f"/api/jobs/{job_id}/{verb}")).json()
        assert body["status"] == "complete"
        assert body["paused"] is False

    assert len(await event_kinds(job_id, "status")) == calls_before, (
        "a no-op must not emit status events"
    )
    assert not engine.is_running(job_id), "resume must not relaunch a finished job"
