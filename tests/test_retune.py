"""Retuning a job that is already running.

A job used to be frozen to the configuration it started with: the engine read provider,
team, assignments, sandbox and budget once, before the phase loop, and never again. So a
provider that went flaky or a budget that turned out too low left only stop-and-rerun. This
pins the seam that changed that — ``PATCH /api/jobs/{id}`` writes the columns, and the engine
re-reads them at each phase boundary — and, more importantly, the *timing* guarantee that
makes it safe: a change lands on the next phase, never on the one already in flight.

The validation half matters just as much. A mid-run switch runs through the very same checks
as job creation, so pointing a live job at an unknown model or an unavailable sandbox is a 400
the operator sees at once, not a job that dies three phases later.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.db import db
from app.orchestrator import sandbox as sandbox_mod
from app.orchestrator.sandbox import SandboxStatus
from tests.conftest import (
    FakeProvider,
    FakePool,
    event_kinds,
    phase_rows,
    wait_for_job,
    wait_for_phase,
)

#: The fake reports 10 prompt + 4 completion tokens on every call, so one call costs this
#: much. Named here rather than imported so a change to the fake surfaces as a failure in
#: this file too, not a silent drift.
PER_CALL = 14


async def _roster(job_id: str) -> set[str]:
    return {
        row["agent"]
        for row in await db.fetch_all("select agent from job_agents where job_id=?", (job_id,))
    }


async def _add_provider(pid: str) -> None:
    """A second enabled profile to switch a running job onto."""
    await db.execute(
        "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,"
        "enabled,created_at)"
        " values(?,?,'openai_compatible','http://localhost:1/v1','fake-1',null,'{}',1,"
        "unixepoch('subsec'))",
        (pid, pid.title()),
    )
    for model in ("fake-1", "fake-2"):
        await db.execute(
            "insert into provider_models(provider_id,model,label,supports_tools,created_at)"
            " values(?,?,null,1,unixepoch('subsec'))",
            (pid, model),
        )


async def _parked_at_planning(job, provider: FakeProvider, **payload) -> str:
    """A job held open on its planning call, so it is running but has advanced nowhere.

    The only deterministic way to PATCH a job that is genuinely mid-run: without the gate a
    fake-driven job reaches a terminal state in milliseconds and the patch races it.
    """
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job(**payload)
    await provider.wait_until_blocked()
    return job_id


# ---------------------------------------------------------------- validation & shape


async def test_retune_refuses_a_finished_job(client: httpx.AsyncClient, job) -> None:
    """Rerun, not retune, is the tool for a job that has already ended."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    response = await client.patch(f"/api/jobs/{job_id}", json={"mode": "yolo"})
    assert response.status_code == 409
    assert "rerun" in response.json()["detail"]
    assert await db.fetch_value("select mode from jobs where id=?", (job_id,)) == "controlled", (
        "a refused patch must not have written anything"
    )


async def test_retune_404_for_an_unknown_job(client: httpx.AsyncClient) -> None:
    assert (await client.patch("/api/jobs/nope", json={"mode": "yolo"})).status_code == 404


async def test_retune_rejects_an_unknown_model(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The same check job creation runs, so the operator learns which model at the boundary."""
    job_id = await _parked_at_planning(job, provider, provider_id="fake")

    bad = await client.patch(
        f"/api/jobs/{job_id}", json={"agents": [{"agent": "coder", "model": "no-such-model"}]}
    )
    assert bad.status_code == 400
    assert "no-such-model" in bad.json()["detail"]

    unknown_agent = await client.patch(
        f"/api/jobs/{job_id}", json={"agents": [{"agent": "ghost", "model": "fake-1"}]}
    )
    assert unknown_agent.status_code == 400

    assert await db.fetch_value(
        "select count(*) from job_agent_providers where job_id=?", (job_id,)
    ) == 0, "a rejected assignment must not be half-written"

    provider.open_gate()
    await wait_for_job(job_id, "complete")


async def test_retune_rejects_an_unavailable_sandbox(
    client: httpx.AsyncClient, job, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live job can no more be moved to a backend this host lacks than started on one."""
    monkeypatch.setitem(
        sandbox_mod._status,
        "sandboxed",
        SandboxStatus(
            id="sandboxed",
            label="Sandboxed",
            available=False,
            reason="the kernel denies unprivileged user namespaces",
        ),
    )
    job_id = await _parked_at_planning(job, provider)
    before = await db.fetch_value("select sandbox from jobs where id=?", (job_id,))
    assert before != "sandboxed", "the job must not already be on the backend under test"

    response = await client.patch(f"/api/jobs/{job_id}", json={"sandbox": "sandboxed"})
    assert response.status_code == 400
    assert "not available on this host" in response.json()["detail"]
    assert "user namespaces" in response.json()["detail"], "the reason must be actionable"
    assert await db.fetch_value("select sandbox from jobs where id=?", (job_id,)) == before, (
        "a rejected sandbox switch must leave the column exactly as it was"
    )

    provider.open_gate()
    await wait_for_job(job_id, "complete")


async def test_retune_writes_only_the_fields_sent(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Omitted leaves a column alone; an explicit null resets a nullable one to the default."""
    job_id = await _parked_at_planning(
        job, provider, provider_id="fake", sandbox="unconfined", token_budget=1000
    )

    # An empty patch is refused rather than treated as a no-op — nothing to change is a
    # mistake worth surfacing, not a silent 200.
    assert (await client.patch(f"/api/jobs/{job_id}", json={})).status_code == 422

    one = (await client.patch(f"/api/jobs/{job_id}", json={"mode": "yolo"})).json()
    assert one["mode"] == "yolo"
    assert one["provider_id"] == "fake", "an omitted field is left untouched"
    assert one["sandbox"] == "unconfined"
    assert one["token_budget"] == 1000

    # Explicit null is the reset: the provider goes back to the server default (resolved when
    # the job runs), which is a different statement from leaving it pinned.
    reset = (await client.patch(f"/api/jobs/{job_id}", json={"provider_id": None})).json()
    assert reset["provider_id"] is None
    assert reset["mode"] == "yolo", "the earlier change is still there"

    notices = [payload for payload in await event_kinds(job_id, "notice") if payload.get("config")]
    assert len(notices) == 2, "each applied patch tells the operator on the stream"
    assert "next phase" in notices[0]["message"]

    provider.open_gate()
    await wait_for_job(job_id, "complete")


# ------------------------------------------------------- the timing guarantee (mid-run)


async def test_a_provider_switch_lands_on_the_next_phase_not_the_running_one(
    client: httpx.AsyncClient, job, provider: FakeProvider, pool: FakePool
) -> None:
    """The whole point of the boundary re-read: the in-flight phase keeps its provider.

    'Build it' parks mid-call on the fake; the provider is switched while it is parked; and
    the phase that was running is asserted to have resolved the *old* provider, while every
    phase after the boundary resolves the new one. The fake returns the same object for any
    id, so the switch is only ever visible in what the pool was *asked* for — which is exactly
    the resolution the engine performs.
    """
    await _add_provider("fake2")
    provider.gate = asyncio.Event()
    provider.gate_after = 1  # planning lands, then 'Design it' (seq 1) parks mid-call

    job_id = await job(provider_id="fake")
    await provider.wait_until_blocked()
    assert await wait_for_phase(job_id, 1, "active") == "active"

    # Everything resolved up to and including the running phase used the old provider.
    asked_when_parked = list(pool.asked)
    assert set(asked_when_parked) == {("fake", None)}, asked_when_parked

    switched = await client.patch(f"/api/jobs/{job_id}", json={"provider_id": "fake2"})
    assert switched.status_code == 200
    assert switched.json()["provider_id"] == "fake2"

    provider.open_gate()
    assert await wait_for_job(job_id, "complete") == "complete"

    # The running phase never switched; a later phase did, and the pool was repointed.
    assert ("fake2", None) not in asked_when_parked, "the in-flight phase must keep its provider"
    assert ("fake2", None) in pool.asked, "phases after the boundary resolve the new provider"
    assert pool.defaults[-1] == "fake2", "the pool's job-default was repointed at the boundary"
    assert [p["status"] for p in await phase_rows(job_id)][1] == "complete", (
        "the phase that was in flight during the switch still finished"
    )


async def test_a_budget_lowered_mid_run_stops_at_the_next_call(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Lowering the cap under what is already spent bites at the next call, not retroactively.

    This is the latent bug the re-read also fixed: the meter's budget is a live field, so once
    ``_reload_config`` reassigns it the very next over-budget check trips. The phase running
    when the cap was lowered still finishes — its call had already passed the check.
    """
    provider.gate = asyncio.Event()
    provider.gate_after = 1  # planning lands (14 spent), 'Design it' parks before it returns

    job_id = await job()  # uncapped to begin with
    await provider.wait_until_blocked()
    assert await wait_for_phase(job_id, 1, "active") == "active"

    lowered = await client.patch(f"/api/jobs/{job_id}", json={"token_budget": 1})
    assert lowered.status_code == 200

    provider.open_gate()
    assert await wait_for_job(job_id, "error") == "error"

    phases = {p["seq"]: p for p in await phase_rows(job_id)}
    assert phases[1]["status"] == "complete", "the phase in flight when the cap dropped finished"
    assert phases[2]["status"] == "failed" and "budget" in phases[2]["error"], (
        "the next phase after the boundary is the one the lowered cap stops"
    )

    usage = (await client.get(f"/api/jobs/{job_id}/usage")).json()
    assert usage["totals"]["total"] == 2 * PER_CALL, (
        "planning and the in-flight phase were paid for; the stopped phase's call never ran"
    )
    budget_notices = [p for p in await event_kinds(job_id, "notice") if p.get("budget")]
    assert budget_notices and "budget" in budget_notices[0]["message"]


async def test_a_team_switch_reseeds_the_remaining_phases(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """A team swapped mid-run only touches what is left: new roles are seeded at the boundary.

    The plan the manager already authored keeps its phase owners, so the job runs to the end
    under the new template; a role the new team adds (here 'reviewer') appears in the agent
    roster, seeded queued exactly as the initial roles were.
    """
    team_b = await client.post(
        "/api/teams",
        json={
            "name": "With a reviewer",
            "roles": [
                {"id": "manager", "name": "Manager", "instructions": "Lead.", "orchestrator": True},
                {"id": "architect", "name": "Architect", "instructions": "Design."},
                {"id": "coder", "name": "Coder", "instructions": "Build."},
                {"id": "tester", "name": "Tester", "instructions": "Check."},
                {"id": "reviewer", "name": "Reviewer", "instructions": "Review."},
            ],
        },
    )
    assert team_b.status_code == 201
    team_b_id = team_b.json()["id"]

    provider.gate = asyncio.Event()
    provider.gate_after = 1  # 'Design it' parks; the swap happens while it is in flight
    job_id = await job()
    await provider.wait_until_blocked()
    assert await wait_for_phase(job_id, 1, "active") == "active"

    assert "reviewer" not in await _roster(job_id), "the new role is absent before the swap"

    switched = await client.patch(f"/api/jobs/{job_id}", json={"team_id": team_b_id})
    assert switched.status_code == 200
    assert await db.fetch_value("select team_id from jobs where id=?", (job_id,)) == team_b_id

    provider.open_gate()
    # The plan's phase owners still resolve under the new team, so it runs to the end; and the
    # role the new team adds was seeded at the boundary — 'reviewer' only ever enters the
    # roster through _reload_config's re-seed, never through planning or finish.
    assert await wait_for_job(job_id, "complete") == "complete"
    assert "reviewer" in await _roster(job_id), "the new team's role was seeded mid-run"
