"""Provider profiles and team templates as real configuration.

Both tables existed in v1 and neither was used: ``runtime.provider()`` read
``~/.codex/config.toml`` directly and the role list was hardcoded, so ``provider_id``
and ``team_id`` were parsed and dropped. The one thing v1 got right — never
returning a secret value from ``/api/providers`` — is pinned down here so it cannot
regress into a convenience "show the key" feature later.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.db import db
from tests.conftest import FakeProvider, phase_rows, wait_for_job

SECRET_VALUE = "sk-live-do-not-leak-this"

CUSTOM_TEAM = {
    "name": "Pair",
    "roles": [
        {
            "id": "manager",
            "name": "Manager",
            "instructions": "Plan and synthesise.",
            "orchestrator": True,
        },
        {"id": "coder", "name": "Coder", "instructions": "Write the code."},
    ],
}


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TEST_PROVIDER_KEY", SECRET_VALUE)
    return "TEST_PROVIDER_KEY"


async def test_providers_expose_the_reference_never_the_secret(
    client: httpx.AsyncClient, secret: str
) -> None:
    saved = await client.put(
        "/api/providers/leaky",
        json={
            "id": "leaky",
            "label": "Leaky",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-test",
            "secret_ref": secret,
        },
    )
    assert saved.status_code == 200
    assert SECRET_VALUE not in saved.text

    listed = await client.get("/api/providers")
    assert SECRET_VALUE not in listed.text
    profile = next(item for item in listed.json() if item["id"] == "leaky")
    assert profile["secret_ref"] == secret
    assert profile["secret_ok"] is True, "the Settings screen needs to know it resolves"

    stored = await db.fetch_all("select * from provider_profiles where id='leaky'")
    assert SECRET_VALUE not in json.dumps([dict(row) for row in stored]), (
        "a secret value must never be written to the database"
    )


async def test_unresolvable_secret_is_reported_not_hidden(client: httpx.AsyncClient) -> None:
    """A misconfigured ref is otherwise only discoverable by watching a job fail."""
    await client.put(
        "/api/providers/broken",
        json={
            "id": "broken",
            "label": "Broken",
            "base_url": "https://api.example.com/v1",
            "model": "gpt-test",
            "secret_ref": "NO_SUCH_ENV_VAR",
        },
    )
    profiles = {item["id"]: item for item in (await client.get("/api/providers")).json()}
    assert profiles["broken"]["secret_ok"] is False
    assert profiles["fake"]["secret_ok"] is None, "no secret required is not the same as broken"


async def test_upsert_updates_in_place(client: httpx.AsyncClient) -> None:
    body = {
        "id": "edit-me",
        "label": "First",
        "base_url": "https://one.example.com/v1/",
        "model": "a",
        "headers": {"X-Tenant": "acme"},
    }
    await client.put("/api/providers/edit-me", json=body)
    updated = await client.put(
        "/api/providers/edit-me", json={**body, "label": "Second", "model": "b", "enabled": False}
    )

    assert updated.json()["label"] == "Second"
    assert updated.json()["model"] == "b"
    assert updated.json()["enabled"] is False
    assert updated.json()["headers"] == {"X-Tenant": "acme"}
    assert updated.json()["base_url"] == "https://one.example.com/v1", "trailing slash normalised"
    assert await db.fetch_value("select count(*) from provider_profiles where id='edit-me'") == 1


async def test_upsert_validates_the_body(client: httpx.AsyncClient) -> None:
    good = {"id": "v", "label": "V", "base_url": "https://v.example.com/v1", "model": "m"}
    mismatch = await client.put("/api/providers/other", json=good)
    assert mismatch.status_code == 400

    bad_url = await client.put(
        "/api/providers/v", json={**good, "base_url": "ftp://v.example.com"}
    )
    assert bad_url.status_code == 422


async def test_delete_keeps_a_profile_that_history_depends_on(
    client: httpx.AsyncClient, job
) -> None:
    """Deleting a referenced profile would orphan the record of what ran a job."""
    unused = {"id": "unused", "label": "U", "base_url": "https://u.example.com/v1", "model": "m"}
    await client.put("/api/providers/unused", json=unused)
    assert (await client.delete("/api/providers/unused")).json() == {
        "id": "unused",
        "deleted": True,
        "disabled": False,
        "jobs": 0,
    }
    assert (await client.delete("/api/providers/unused")).status_code == 404

    job_id = await job(provider_id="fake")
    await wait_for_job(job_id, "complete")
    result = (await client.delete("/api/providers/fake")).json()
    assert result == {"id": "fake", "deleted": False, "disabled": True, "jobs": 1}
    assert await db.fetch_value("select enabled from provider_profiles where id='fake'") == 0


async def test_seeded_team_is_the_four_role_default(client: httpx.AsyncClient) -> None:
    teams = (await client.get("/api/teams")).json()
    # Two seeded rows now: the original, and the v2 rewrite whose specialists are told
    # they have a shell. Templates are append-only, so migration 002 adds a row and
    # moves `is_default` rather than editing the instructions a finished job ran with.
    assert len(teams) == 2
    defaults = [team for team in teams if team["is_default"]]
    assert len(defaults) == 1, "exactly one template may be the default"
    default = defaults[0]
    assert default["id"] == max(team["id"] for team in teams), "the newest seed leads"
    assert [role["id"] for role in default["roles"]] == ["manager", "architect", "coder", "tester"]
    assert [role["id"] for role in default["roles"] if role["orchestrator"]] == ["manager"]

    # The point of v2 is that the roles know they can act. A template that still reads
    # like v1 would run the tools code and never use it.
    instructions = {role["id"]: role["instructions"] for role in default["roles"]}
    assert "write_file" in instructions["coder"]
    assert "Verify the work by executing it" in instructions["tester"]

    assert (await client.get(f"/api/teams/{default['id']}")).json() == default
    assert (await client.get("/api/teams/999")).status_code == 404


async def test_created_team_actually_runs_the_job(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """``team_id`` was accepted and ignored in v1; roles now come from the template."""
    created = await client.post("/api/teams", json=CUSTOM_TEAM)
    assert created.status_code == 201
    team_id = created.json()["id"]

    job_id = await job(team_id=team_id)
    await wait_for_job(job_id, "complete")

    owners = {phase["owner"] for phase in await phase_rows(job_id)}
    assert owners == {"manager", "coder"}, "phases must only name roles the team has"

    joined = "\n".join(provider.systems)
    assert "You are Coder." in joined
    assert "You are Tester." not in joined, "a role outside the template must not appear"
    assert {
        row["agent"] for row in await db.fetch_all(
            "select agent from job_agents where job_id=?", (job_id,)
        )
    } == {"manager", "coder"}


async def test_team_must_have_exactly_one_orchestrator(client: httpx.AsyncClient) -> None:
    """Two leads or none is unrunnable, so it fails at creation, not at the first job."""
    two_leads = {
        "name": "Confused",
        "roles": [
            {**CUSTOM_TEAM["roles"][0]},
            {**CUSTOM_TEAM["roles"][1], "orchestrator": True},
        ],
    }
    # Snapshot rather than a literal: the claim is that nothing was *added*, which is
    # independent of how many templates the migrations happen to seed.
    seeded = await db.fetch_value("select count(*) from team_templates")
    assert (await client.post("/api/teams", json=two_leads)).status_code == 422

    no_lead = {"name": "Leaderless", "roles": [{**CUSTOM_TEAM["roles"][1]}]}
    assert (await client.post("/api/teams", json=no_lead)).status_code == 422

    duplicates = {"name": "Dupes", "roles": [CUSTOM_TEAM["roles"][0], CUSTOM_TEAM["roles"][0]]}
    assert (await client.post("/api/teams", json=duplicates)).status_code == 422

    assert await db.fetch_value("select count(*) from team_templates") == seeded, (
        "a rejected template must not be left behind"
    )


async def test_default_team_is_used_when_a_job_omits_one(client: httpx.AsyncClient, job) -> None:
    team_id = (await client.post("/api/teams", json=CUSTOM_TEAM)).json()["id"]
    promoted = await client.post(f"/api/teams/{team_id}/default")
    assert promoted.status_code == 200
    assert promoted.json()["is_default"] is True

    teams = {item["id"]: item for item in (await client.get("/api/teams")).json()}
    assert [item["id"] for item in teams.values() if item["is_default"]] == [team_id], (
        "only one template may be the default"
    )

    job_id = await job()
    assert await db.fetch_value("select team_id from jobs where id=?", (job_id,)) == team_id
    await wait_for_job(job_id, "complete")

    assert (await client.post("/api/teams/999/default")).status_code == 404
