"""The restart-resume regression test.

This is the confirmed v1 bug: ``lifespan`` relaunched interrupted jobs from phase
one and appended to the same job. The archived database has job ``cb183d475f0a``
with message phases ``[1,1,2,3,1,2,3,4,5]`` after four restarts.

The test reproduces the exact conditions — interrupt mid-phase, restart, finish —
and asserts what v1 got wrong: completed phases do not re-run, and the phase
sequence in the event log is strictly increasing.
"""

from __future__ import annotations

import asyncio

from app.db import db
from app.deps import engine
from tests.conftest import FakeProvider, event_kinds, phase_rows, wait_for_job, wait_for_phase


async def test_restart_resumes_at_the_interrupted_phase(job, provider: FakeProvider) -> None:
    provider.gate = asyncio.Event()
    provider.gate_after = 2  # plan and 'Design it' land; 'Build it' hangs

    job_id = await job()
    await provider.wait_until_blocked()
    assert await wait_for_phase(job_id, 2, "active") == "active"
    design_output = await db.fetch_value(
        "select output from phases where job_id=? and seq=1", (job_id,)
    )
    assert design_output, "the completed phase must have persisted its output"

    # --- the restart -------------------------------------------------------
    await engine.shutdown(grace=2)
    assert not engine.is_running(job_id)
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "queued"
    assert (
        await db.fetch_value("select status from phases where job_id=? and seq=2", (job_id,))
        == "pending"
    ), "the interrupted phase must be left runnable"

    provider.open_gate()
    assert job_id in await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    # --- what v1 got wrong -------------------------------------------------
    phases = await phase_rows(job_id)
    attempts = {p["seq"]: p["attempts"] for p in phases}
    assert attempts[0] == 1, "planning must not run again"
    assert attempts[1] == 1, "a completed phase must not run again"
    assert attempts[2] == 2, "only the interrupted phase re-runs"
    assert all(p["status"] == "complete" for p in phases)

    assert (
        await db.fetch_value("select output from phases where job_id=? and seq=1", (job_id,))
        == design_output
    ), "completed output must be read back, not re-derived"

    sequences = [
        payload["seq"] for payload in await event_kinds(job_id, "message") if "seq" in payload
    ]
    assert sequences == sorted(set(sequences)), f"phase sequence must not repeat: {sequences}"


async def test_interrupted_planning_does_not_duplicate_the_plan(
    job, provider: FakeProvider
) -> None:
    """Planning is a phase too, so a crash during it must not double the plan."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0  # the planning call itself hangs

    job_id = await job()
    await provider.wait_until_blocked()

    await engine.shutdown(grace=2)
    provider.open_gate()
    await engine.recover()
    await wait_for_job(job_id, "complete")

    phases = await phase_rows(job_id)
    assert len(phases) == 5, f"the plan was inserted twice: {[p['name'] for p in phases]}"
    assert [p["seq"] for p in phases] == [0, 1, 2, 3, 4]
    assert len(await event_kinds(job_id, "plan")) == 1


async def test_recover_ignores_terminal_jobs(job, provider: FakeProvider) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")
    calls = len(provider.prompts)

    assert job_id not in await engine.recover()
    await asyncio.sleep(0.1)
    assert len(provider.prompts) == calls, "a finished job must not be re-run on restart"


async def test_shutdown_completes_within_its_grace_period(job, provider: FakeProvider) -> None:
    """Shutdown must not hang on a job that is mid-provider-call.

    v1 had no shutdown path at all: `systemctl stop` waited on background tasks that
    could never finish and systemd escalated to SIGKILL after 90 seconds, having
    burned 7m19s of CPU on one spinning job.
    """
    provider.gate = asyncio.Event()
    provider.gate_after = 0

    job_id = await job()
    await provider.wait_until_blocked()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await engine.shutdown(grace=2)
    elapsed = loop.time() - started

    assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s despite an in-flight provider call"
    assert provider.cancelled == 1, "the in-flight call was not actually interrupted"
    assert engine.active_count == 0
    provider.open_gate()
