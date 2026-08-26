"""The vertical slice, plus the invariants that hold on every healthy job."""

from __future__ import annotations

import httpx
import pytest

from app.db import db
from app.migrations import discover, migrate
from app.orchestrator.providers import ProviderError
from tests.conftest import FakeProvider, phase_rows, wait_for_job


async def test_health_reports_schema_and_state(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Every shipped migration should be applied by the time health says "ok", so this
    # tracks the migration set rather than a literal — a mismatch means the service is
    # reporting a schema it does not actually have.
    assert body["schema_version"] == max(version for version, _ in discover())
    assert body["active_jobs"] == 0


async def test_migrations_are_idempotent() -> None:
    """A second run applies nothing and leaves the schema alone."""
    before = await db.fetch_value("select count(*) from sqlite_master where type='table'")
    assert await migrate(db) == []
    assert await db.fetch_value("select count(*) from sqlite_master where type='table'") == before


async def test_job_runs_plan_work_synthesis(client: httpx.AsyncClient, job) -> None:
    """Manager plans, specialists execute, manager synthesises — all phases terminal.

    This is the check v1 could never pass: its phases were a static copy of the
    role list and stayed 'queued' even on completed jobs.
    """
    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    phases = await phase_rows(job_id)
    assert [(p["seq"], p["kind"], p["owner"]) for p in phases] == [
        (0, "plan", "manager"),
        (1, "work", "architect"),
        (2, "work", "coder"),
        (3, "work", "tester"),
        (4, "synthesis", "manager"),
    ]
    assert all(p["status"] == "complete" for p in phases)
    assert all(p["output"] for p in phases), "every phase must persist its output"
    assert all(p["attempts"] == 1 for p in phases), "no phase should run twice"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["status"] == "complete"
    assert snapshot["result"]["content"]
    assert snapshot["cursor"] == snapshot["events"][-1]["id"]
    assert {agent["status"] for agent in snapshot["team"]} == {"complete"}


async def test_synthesis_writes_an_artifact(client: httpx.AsyncClient, job) -> None:
    """Artifacts are written by the engine.

    v1 had a ``maybe_artifact`` helper that no code path called, so the table stayed
    empty and the Artifacts screen had nothing to show.
    """
    job_id = await job()
    await wait_for_job(job_id, "complete")

    artifacts = (await client.get(f"/api/jobs/{job_id}/artifacts")).json()
    assert [a["name"] for a in artifacts] == ["result.md"]
    assert artifacts[0]["size"] > 0

    download = await client.get(f"/api/jobs/{job_id}/artifacts/{artifacts[0]['id']}")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/markdown")
    assert download.text


async def test_prior_phase_output_reaches_the_next_specialist(
    job, provider: FakeProvider
) -> None:
    """Each phase sees earlier phases' work, so the chain is a real handoff."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    build_prompt = next(p for p in provider.prompts if "Your phase: Build it" in p)
    assert "Design it (by architect)" in build_prompt
    assert "code exists" in build_prompt, "acceptance criteria must be in the prompt"


async def test_each_role_gets_its_own_system_prompt(job, provider: FakeProvider) -> None:
    """Roles come from the template, not a hardcoded list in the runtime."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    joined = "\n".join(provider.systems)
    for role in ("Manager", "Architect", "Coder", "Tester"):
        assert f"You are {role}." in joined


async def test_list_endpoint_reports_progress_counts(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    rows = (await client.get("/api/jobs")).json()
    row = next(item for item in rows if item["id"] == job_id)
    assert row["phase_total"] == 5
    assert row["phase_complete"] == 5
    assert row["pending_approvals"] == 0
    assert row["artifact_count"] == 1


async def test_handoffs_are_recorded_between_owners(job) -> None:
    """PLAN.md §3 wants handoffs as records; this part of v1 already worked."""
    from tests.conftest import event_kinds

    job_id = await job()
    await wait_for_job(job_id, "complete")

    handoffs = [(h["from"], h["to"]) for h in await event_kinds(job_id, "handoff")]
    assert handoffs == [
        ("manager", "architect"),
        ("architect", "coder"),
        ("coder", "tester"),
        ("tester", "manager"),
    ]


async def test_unknown_job_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/jobs/doesnotexist")).status_code == 404
    assert (await client.post("/api/jobs/doesnotexist/pause")).status_code == 404


async def test_unknown_team_falls_back_to_default(job) -> None:
    """v1 accepted any ``team_id`` and ignored it; the default was 3 with only 1 existing."""
    job_id = await job(team_id=999)
    # The default template moves as new versions are seeded (migration 002 appends a
    # tool-aware v2), so the claim is "it lands on whichever row is default", not "1".
    default_id = await db.fetch_value("select id from team_templates where is_default=1")
    assert await db.fetch_value("select team_id from jobs where id=?", (job_id,)) == default_id


async def test_unknown_provider_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/jobs", json={"task": "anything", "provider_id": "nope"})
    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


async def test_blank_task_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/jobs", json={"task": "   "})).status_code == 422


# ------------------------------------------------------------------- failure paths
#
# A terminal job must never hold a non-terminal phase. That invariant is what lets
# the Plan tab and `job show` be read literally: anything not terminal is still
# live. v1 broke it constantly — its phases stayed 'queued' on finished jobs.


async def test_unusable_provider_fails_the_job_and_closes_its_phases(
    client: httpx.AsyncClient, job, pool
) -> None:
    """A provider that cannot even be built fails the job before phase 0 runs."""

    async def unusable(provider_id=None, model=None):  # noqa: ANN001, ANN202
        raise ProviderError("secret 'AGENT_HUB_NO_SUCH_SECRET' not found")

    # The pool is where a provider is built now, and it is asked once per phase — so
    # this also covers the case of a profile that stops resolving partway through a job.
    pool.get = unusable

    job_id = await job()
    assert await wait_for_job(job_id, "error") == "error"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert "AGENT_HUB_NO_SUCH_SECRET" in snapshot["error"]
    # Phase 0 never started, but the job is over, so it must not read as pending.
    assert [(p["seq"], p["status"]) for p in snapshot["plan"]] == [(0, "skipped")]
    assert "job failed" in snapshot["plan"][0]["error"]


async def test_failed_phase_fails_and_later_phases_are_skipped(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The phase that broke reads 'failed'; the ones queued behind it read 'skipped'."""
    provider.fail_after = 1  # the manager plans, then the first work phase breaks
    provider.fail_with = "upstream returned 502"

    job_id = await job()
    assert await wait_for_job(job_id, "error") == "error"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert "502" in snapshot["error"]
    assert [(p["seq"], p["status"]) for p in snapshot["plan"]] == [
        (0, "complete"),
        (1, "failed"),
        (2, "skipped"),
        (3, "skipped"),
        (4, "skipped"),
    ]
    assert "502" in snapshot["plan"][1]["error"], "the failing phase records its own cause"
    assert all(p["finished_at"] for p in snapshot["plan"]), "a terminal phase has an end time"
    # Phase 0's output survives the failure, so a retry would not re-plan.
    assert snapshot["plan"][0]["output"]
