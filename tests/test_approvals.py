"""Approval gates.

PLAN.md §3 says risky actions pause for approval. In v1 nothing created a gate and
nothing waited on one — ``decide_approval`` recorded a decision that unblocked no
work — so these are all new behaviours rather than regressions.

The interesting cases are the ones that cross a restart: a gate must survive it
without re-running the phase and without asking the operator twice.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx

from app.db import db
from app.deps import engine
from tests.conftest import (
    DEFAULT_PLAN,
    FakeProvider,
    phase_rows,
    wait_for_approval,
    wait_for_job,
    wait_for_phase,
    wait_until,
)


def plan_gating_first_phase() -> dict[str, Any]:
    plan = copy.deepcopy(DEFAULT_PLAN)
    plan["phases"][0]["requires_approval"] = True
    return plan


async def approvals_for(job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select * from approvals where job_id=? order by created_at", (job_id,)
    )
    return [dict(row) for row in rows]


async def phase_events(job_id: str, seq: int) -> list[str]:
    """The statuses announced for one phase, in order."""
    rows = await db.fetch_all(
        "select payload from events where job_id=? and kind='phase'"
        " and json_extract(payload,'$.seq')=? order by id",
        (job_id, seq),
    )
    return [json.loads(row["payload"])["status"] for row in rows]


async def status_events(job_id: str) -> list[str]:
    """The job-level statuses announced, in order."""
    rows = await db.fetch_all(
        "select payload from events where job_id=? and kind='status' order by id", (job_id,)
    )
    return [json.loads(row["payload"])["status"] for row in rows]


async def test_gate_blocks_execution_until_approved(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    provider.plan = plan_gating_first_phase()

    job_id = await job()
    gate = await wait_for_approval(job_id)

    assert await wait_for_phase(job_id, 1, "blocked_on_approval") == "blocked_on_approval"
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "blocked"
    assert gate["phase_id"] == await db.fetch_value(
        "select id from phases where job_id=? and seq=1", (job_id,)
    )
    assert gate["auto"] == 0
    assert provider.prompts_containing("Your phase: Design it") == [], (
        "the gated phase must not have run before approval"
    )

    response = await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}",
        json={"decision": "approved", "note": "looks fine"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    assert await wait_for_job(job_id, "complete") == "complete"
    assert len(provider.prompts_containing("Your phase: Design it")) == 1


async def test_rejection_skips_the_phase_with_a_reason(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """A rejected phase is skipped, and the job carries on with the rest."""
    provider.plan = plan_gating_first_phase()

    job_id = await job()
    gate = await wait_for_approval(job_id)

    await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}",
        json={"decision": "rejected", "note": "wrong approach"},
    )
    assert await wait_for_job(job_id, "complete") == "complete"

    phases = {p["seq"]: p for p in await phase_rows(job_id)}
    assert phases[1]["status"] == "skipped"
    assert "wrong approach" in phases[1]["error"]
    assert phases[2]["status"] == "complete", "later phases still run"
    assert provider.prompts_containing("Your phase: Design it") == []


async def test_gate_survives_a_restart_without_asking_twice(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    provider.plan = plan_gating_first_phase()

    job_id = await job()
    gate = await wait_for_approval(job_id)

    await engine.shutdown(grace=2)
    assert (
        await db.fetch_value("select status from phases where job_id=? and seq=1", (job_id,))
        == "blocked_on_approval"
    ), "a blocked phase must not be reset to pending"

    await engine.recover()
    assert await wait_for_phase(job_id, 1, "blocked_on_approval") == "blocked_on_approval"
    assert len(await approvals_for(job_id)) == 1, "the restart created a second gate"

    await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "approved"}
    )
    assert await wait_for_job(job_id, "complete") == "complete"
    assert len(await approvals_for(job_id)) == 1


async def test_a_restart_does_not_announce_the_same_gate_twice(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Re-attaching to a gate is not the phase blocking again.

    The sibling test above proves the restart raises no second gate in the database.
    This one is about the story the Timeline tells: recovery used to re-emit the
    phase's ``blocked_on_approval`` event, so the same gate appeared twice and the
    operator could not tell a re-attach from a phase that had genuinely re-blocked.
    """
    provider.plan = plan_gating_first_phase()

    job_id = await job()
    gate = await wait_for_approval(job_id)
    assert await phase_events(job_id, 1) == ["blocked_on_approval"]

    await engine.shutdown(grace=2)
    await engine.recover()

    # Waiting on the phase status would prove nothing: the row already reads
    # 'blocked_on_approval' from before the restart, so the assertion would fire
    # before the recovered run ever reached the gate. Wait for evidence that it did —
    # `_run` sets the job column to 'running' on its way in and the gate sets it back
    # to 'blocked', so two fresh status events mean the gate code has run.
    before = await status_events(job_id)

    async def gate_ran() -> bool:
        return len(await status_events(job_id)) >= len(before) + 2

    await wait_until(gate_ran)
    assert (await status_events(job_id))[len(before) :] == ["running", "blocked"]

    assert await phase_events(job_id, 1) == ["blocked_on_approval"], (
        "recovery announced the gate a second time"
    )

    await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "approved"}
    )
    assert await wait_for_job(job_id, "complete") == "complete"
    assert await phase_events(job_id, 1) == ["blocked_on_approval", "active", "complete"]


async def test_decision_taken_while_the_process_was_down_is_honoured(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Approving between a crash and a restart must not re-gate the phase.

    This is the case that made ``latest_for_phase`` necessary: looking only for
    *pending* approvals would find none and raise a second gate for work the
    operator had already approved.
    """
    provider.plan = plan_gating_first_phase()

    job_id = await job()
    gate = await wait_for_approval(job_id)
    await engine.shutdown(grace=2)

    response = await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "approved"}
    )
    assert response.status_code == 200

    await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"
    assert len(await approvals_for(job_id)) == 1, "the restart asked for approval again"
    assert len(provider.prompts_containing("Your phase: Design it")) == 1


async def test_yolo_mode_auto_approves_and_records_that_it_did(
    job, provider: FakeProvider
) -> None:
    """Modes finally do something. Auto-approval is recorded, not skipped.

    Skipping the gate entirely would be simpler but would leave no trace that work
    with real consequences ran without review.
    """
    provider.plan = plan_gating_first_phase()

    job_id = await job(mode="yolo")
    assert await wait_for_job(job_id, "complete") == "complete"

    gates = await approvals_for(job_id)
    assert len(gates) == 1
    assert gates[0]["status"] == "approved"
    assert gates[0]["auto"] == 1
    assert gates[0]["decided_at"] is not None
    assert len(provider.prompts_containing("Your phase: Design it")) == 1


async def test_deciding_an_already_decided_gate_is_a_conflict(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    provider.plan = plan_gating_first_phase()
    job_id = await job()
    gate = await wait_for_approval(job_id)

    first = await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "approved"}
    )
    second = await client.post(
        f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "rejected"}
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert "already approved" in second.json()["detail"]
    await wait_for_job(job_id, "complete")


async def test_unknown_gate_is_404(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    response = await client.post(
        f"/api/jobs/{job_id}/approvals/nope", json={"decision": "approved"}
    )
    assert response.status_code == 404
    await wait_for_job(job_id, "complete")


async def test_manual_gate_appears_in_the_inbox(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The inbox count the Jobs screen shows was permanently 0 in v1."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    created = await client.post(
        f"/api/jobs/{job_id}/approvals",
        json={"action": "Delete the production bucket", "risk": "high", "agent": "coder"},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"

    inbox = (await client.get("/api/approvals?status=pending")).json()
    row = next(item for item in inbox if item["id"] == created.json()["id"])
    assert row["job_id"] == job_id
    assert row["job_task"] == "Ship a status page"

    listed = (await client.get(f"/api/jobs/{job_id}/approvals")).json()
    assert [item["action"] for item in listed] == ["Delete the production bucket"]
    assert (await client.get("/api/jobs")).json()[0]["pending_approvals"] == 1

    provider.open_gate()
    await wait_for_job(job_id, "complete")


async def test_manual_gate_rejects_a_foreign_phase(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    first = await job()
    await wait_for_job(first, "complete")
    other_phase = await db.fetch_value(
        "select id from phases where job_id=? limit 1", (first,)
    )

    second = await job("A different task")
    response = await client.post(
        f"/api/jobs/{second}/approvals",
        json={"action": "something", "phase_id": other_phase},
    )
    assert response.status_code == 400
    await wait_for_job(second, "complete")
