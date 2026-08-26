"""Providers that serve many models, per-agent assignment, metering and continuation.

Four features share one test file because they share one question: *which model ran
this, and what did it cost?* Before this, a provider profile was a single model, every
agent used it, the token counts in every response were parsed and discarded, and a
finished job was a dead end.

Nothing here touches the network. The adapters are exercised against
``httpx.MockTransport``; the engine paths run through the ``FakeProvider`` and
``FakePool`` from ``conftest``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, get_args

import httpx
import pytest

from app.db import db
from app.models import ProviderUpsert
from app.orchestrator.providers import (
    KIND_IDS,
    PROVIDER_KINDS,
    PROVIDER_TEMPLATES,
    AnthropicProvider,
    Message,
    ProviderConfigError,
    ProviderError,
    ProviderPool,
    ToolCallRequest,
    _to_anthropic_messages,
    _to_anthropic_tool,
    build_provider,
    discover_models,
    kind_spec,
    normalize_usage,
)
from app.orchestrator.usage import UsageMeter, job_usage
from tests.conftest import FakeProvider, phase_rows, wait_for_job

# --------------------------------------------------------------- the usage vocabulary


def test_usage_is_normalized_from_both_dialects() -> None:
    """One shape out, whichever spelling came in.

    The whole point of the ledger is that a job's cost is comparable across providers,
    which it is not if an Anthropic call records zeros because it said `input_tokens`.
    """
    openai = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 64},
            "completion_tokens_details": {"reasoning_tokens": 8},
        }
    )
    assert openai == {"prompt": 100, "completion": 20, "total": 120, "cached": 64, "reasoning": 8}

    anthropic = normalize_usage(
        {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 64}
    )
    # No total reported, so it is derived rather than left at zero.
    assert anthropic["prompt"] == 100
    assert anthropic["completion"] == 20
    assert anthropic["total"] == 120
    assert anthropic["cached"] == 64


@pytest.mark.parametrize("value", [None, {}, "not a dict", {"prompt_tokens": None}])
def test_usage_of_a_provider_that_reports_nothing_is_zero(value: Any) -> None:
    """A missing count reads as 0. Accounting must not be able to throw."""
    assert normalize_usage(value) == {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "cached": 0,
        "reasoning": 0,
    }


def test_a_boolean_is_not_a_token_count() -> None:
    """`True` is an int in Python, and would otherwise record as one token."""
    assert normalize_usage({"prompt_tokens": True, "completion_tokens": 5})["prompt"] == 0


# ------------------------------------------------------------------ the kind catalogue


def test_the_kind_literal_and_the_catalogue_agree() -> None:
    """``ProviderUpsert.kind`` is a Literal; ``PROVIDER_KINDS`` is the source of truth.

    They are separate so ``app.models`` need not import the orchestrator. This is the
    assertion that keeps that separation from becoming a divergence — a kind added to
    the catalogue and not the Literal would be unsavable through the API.
    """
    literal = set(get_args(ProviderUpsert.model_fields["kind"].annotation))
    assert literal == set(KIND_IDS)


def test_every_template_names_a_real_kind_and_a_way_to_get_models() -> None:
    """A template that fills the form with an unusable kind is worse than no template.

    Seeded models are optional: for a gateway whose catalogue changes weekly, a
    hardcoded list goes stale and discovery does not. But a template must offer one or
    the other, or picking it leaves the operator with a profile that cannot be saved.
    """
    for template in PROVIDER_TEMPLATES:
        assert template["kind"] in KIND_IDS, template["id"]
        assert isinstance(template["base_url"], str)
        models = template["models"]
        assert isinstance(models, list)
        assert len(models) == len(set(models)), template["id"]
        if not models:
            assert kind_spec(template["kind"])["models_path"], template["id"]


async def test_the_kinds_and_templates_are_served_to_the_form(
    client: httpx.AsyncClient,
) -> None:
    kinds = (await client.get("/api/provider-kinds")).json()
    assert {entry["id"] for entry in kinds} == set(KIND_IDS)
    assert all(entry["label"] and entry["detail"] for entry in kinds)

    templates = (await client.get("/api/provider-templates")).json()
    assert len(templates) == len(PROVIDER_TEMPLATES)
    assert {entry["id"] for entry in templates} >= {"openai", "anthropic", "ollama", "custom"}


# ------------------------------------------------------------------- the model list


async def test_a_provider_keeps_a_list_of_models(client: httpx.AsyncClient) -> None:
    """The core of the ask: a provider is an endpoint, not a model."""
    response = await client.put(
        "/api/providers/multi",
        json={
            "id": "multi",
            "label": "Multi",
            "base_url": "https://example.test/v1",
            "model": "big",
            "models": [
                {"model": "big", "label": "Big"},
                {"model": "small", "supports_tools": False},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "big"
    assert [entry["model"] for entry in body["models"]] == ["big", "small"]
    assert [entry["supports_tools"] for entry in body["models"]] == [True, False]

    listed = (await client.get("/api/providers")).json()
    multi = next(entry for entry in listed if entry["id"] == "multi")
    assert len(multi["models"]) == 2


def test_a_default_model_outside_the_list_is_added_to_it() -> None:
    """Otherwise the profile row and its own model list disagree."""
    payload = ProviderUpsert(
        id="p", label="P", base_url="https://example.test/v1", model="chosen",
        models=[{"model": "other"}],  # type: ignore[list-item]
    )
    assert payload.model == "chosen"
    assert [entry.model for entry in payload.models] == ["chosen", "other"]


def test_a_provider_with_models_but_no_default_nominates_the_first() -> None:
    payload = ProviderUpsert(
        id="p", label="P", base_url="https://example.test/v1",
        models=[{"model": "first"}, {"model": "second"}],  # type: ignore[list-item]
    )
    assert payload.model == "first"


def test_a_provider_with_no_model_at_all_is_refused() -> None:
    """It would save cleanly and then fail on the first call of every job using it."""
    with pytest.raises(ValueError, match="at least one model"):
        ProviderUpsert(id="p", label="P", base_url="https://example.test/v1")


async def test_saving_a_provider_replaces_its_model_list(client: httpx.AsyncClient) -> None:
    """A write is a plain edit, not a merge — a removed model must actually go."""
    body = {
        "id": "multi",
        "label": "Multi",
        "base_url": "https://example.test/v1",
        "model": "a",
        "models": [{"model": "a"}, {"model": "b"}, {"model": "c"}],
    }
    await client.put("/api/providers/multi", json=body)

    body["models"] = [{"model": "a"}, {"model": "c"}]
    updated = (await client.put("/api/providers/multi", json=body)).json()
    assert [entry["model"] for entry in updated["models"]] == ["a", "c"]


# --------------------------------------------------------------------- discovery


def _transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_discovery_reads_the_openai_listing() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-z", "display_name": "GPT Z"},
                    {"id": "gpt-a"},
                    {"id": "gpt-a"},  # a duplicate the gateway reported twice
                    {"nonsense": True},
                ]
            },
        )

    async with _transport(handler) as http:
        found = await discover_models(
            {
                "id": "p",
                "kind": "openai_compatible",
                "base_url": "https://example.test/v1",
                "secret_ref": None,
                "headers": {},
            },
            http,
        )

    assert seen["url"] == "https://example.test/v1/models"
    assert seen["auth"] is None  # no secret_ref, so no header invented
    # Sorted, deduped, and the unusable entry dropped rather than crashing the call.
    assert found == [
        {"model": "gpt-a", "label": None},
        {"model": "gpt-z", "label": "GPT Z"},
    ]


async def test_discovery_reads_a_bare_array_and_string_entries() -> None:
    """Self-hosted servers do both. Neither should need a special provider kind."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["llama3", {"name": "qwen"}])

    async with _transport(handler) as http:
        found = await discover_models(
            {"id": "p", "kind": "openai_compatible", "base_url": "http://localhost:11434/v1"},
            http,
        )
    assert [entry["model"] for entry in found] == ["llama3", "qwen"]


async def test_discovery_reports_an_endpoint_that_refuses() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="no key")

    async with _transport(handler) as http:
        with pytest.raises(ProviderError, match="HTTP 401"):
            await discover_models(
                {"id": "p", "kind": "openai_compatible", "base_url": "https://example.test/v1"},
                http,
            )


async def test_discovery_reports_html_instead_of_crashing() -> None:
    """A base_url pointing at a web page is the most common misconfiguration."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not an API</html>")

    async with _transport(handler) as http:
        with pytest.raises(ProviderError, match="did not return JSON"):
            await discover_models(
                {"id": "p", "kind": "openai_compatible", "base_url": "https://example.test"},
                http,
            )


async def test_the_discovery_endpoint_saves_nothing(client: httpx.AsyncClient) -> None:
    """Read-only on purpose: a gateway listing 400 models must not become the picker."""
    await client.put(
        "/api/providers/multi",
        json={
            "id": "multi",
            "label": "Multi",
            "base_url": "https://example.test/v1",
            "model": "a",
            "models": [{"model": "a"}],
        },
    )
    # The endpoint is unreachable in tests, which is itself the case worth asserting:
    # the operator gets 502 and a reason, not a 500, and can still type ids by hand.
    response = await client.post("/api/providers/multi/models/discover")
    assert response.status_code == 502
    assert "example.test" in response.json()["detail"]

    unchanged = (await client.get("/api/providers")).json()
    multi = next(entry for entry in unchanged if entry["id"] == "multi")
    assert [entry["model"] for entry in multi["models"]] == ["a"]


async def test_a_provider_named_by_one_agent_is_not_hard_deleted(
    client: httpx.AsyncClient, job
) -> None:
    """The audit trail of what an agent ran on outlives the operator's tidying up."""
    await client.put(
        "/api/providers/side",
        json={
            "id": "side",
            "label": "Side",
            "base_url": "https://example.test/v1",
            "model": "side-1",
            "models": [{"model": "side-1"}],
        },
    )
    job_id = await job(agents=[{"agent": "coder", "provider_id": "side"}])
    await wait_for_job(job_id, "complete")

    response = await client.delete("/api/providers/side")
    assert response.status_code == 200
    body = response.json()
    # The job's own provider_id is 'fake', so only the per-agent row references this.
    assert body == {"id": "side", "deleted": False, "disabled": True, "jobs": 1}
    assert await db.exists("select 1 from provider_profiles where id='side'")


# ----------------------------------------------------------------------- the pool


async def test_the_pool_builds_one_adapter_per_provider_and_model() -> None:
    """A six-phase job must not re-resolve the same secret six times."""
    async with _transport(lambda _: httpx.Response(200, json={})) as http:
        async with ProviderPool(db, default_provider_id="fake", client=http) as pool:
            first = await pool.get("fake", "fake-1")
            again = await pool.get("fake", "fake-1")
            other = await pool.get("fake", "fake-2")

            assert first is again
            assert other is not first
            assert other.model == "fake-2"
            # The default resolves to the same cache entry as naming it explicitly.
            assert await pool.get() is first


async def test_the_pool_refuses_a_provider_that_is_gone() -> None:
    """No silent fallback to the job's default.

    Running an agent on a model the operator did not choose is the same class of
    mistake as running a command unsandboxed because the sandbox was missing.
    """
    async with _transport(lambda _: httpx.Response(200, json={})) as http:
        async with ProviderPool(db, default_provider_id="fake", client=http) as pool:
            with pytest.raises(ProviderError):
                await pool.get("no-such-provider")


async def test_a_profile_with_no_model_is_refused_at_build_time() -> None:
    with pytest.raises(ProviderConfigError, match="no model"):
        build_provider({"id": "p", "base_url": "https://example.test/v1", "model": ""})


def test_an_unknown_kind_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ProviderConfigError, match="unsupported provider kind"):
        build_provider(
            {"id": "p", "base_url": "https://x.test", "model": "m", "kind": "telepathy"}
        )


# ------------------------------------------------------------------ the anthropic kind


def test_the_anthropic_adapter_is_selected_by_kind() -> None:
    provider = build_provider(
        {
            "id": "claude",
            "kind": "anthropic",
            "base_url": "https://api.anthropic.test",
            "model": "claude-opus-5",
            "secret_ref": None,
            "headers": {},
        }
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-opus-5"


async def test_the_anthropic_adapter_speaks_the_messages_api() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["version"] = request.headers.get("anthropic-version")
        sent["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 11, "output_tokens": 3},
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "tu_1", "name": "run", "input": {"cmd": "ls"}},
                ],
            },
        )

    async with _transport(handler) as http:
        provider = AnthropicProvider(
            id="claude",
            base_url="https://api.anthropic.test",
            model="claude-opus-5",
            secret="k",
            client=http,
        )
        completion = await provider.complete(
            system="be brief",
            messages=[Message(role="user", content="hello")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "run",
                        "description": "run a command",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

    assert sent["url"] == "https://api.anthropic.test/messages"
    assert sent["version"]
    assert sent["body"]["system"] == "be brief"
    # Required by the API, unlike the OpenAI dialect where it is optional.
    assert sent["body"]["max_tokens"] > 0
    assert sent["body"]["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    assert completion.text == "checking"
    assert [call.name for call in completion.tool_calls] == ["run"]
    assert completion.tool_calls[0].args() == {"cmd": "ls"}
    assert completion.finish_reason == "tool_use"
    assert normalize_usage(completion.usage)["total"] == 14


def test_a_tool_result_becomes_a_user_message_with_a_tool_result_block() -> None:
    """The shape rule the OpenAI dialect does not have. Getting it wrong is a 400."""
    turns = _to_anthropic_messages(
        [
            Message(role="user", content="do it"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCallRequest(id="tu_1", name="run", arguments='{"cmd":"ls"}')],
            ),
            Message(role="tool", content="a.txt", tool_call_id="tu_1", name="run"),
        ]
    )

    assert [turn["role"] for turn in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"][0]["type"] == "tool_use"
    assert turns[1]["content"][0]["input"] == {"cmd": "ls"}
    result = turns[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_1"


def test_consecutive_same_role_turns_are_merged() -> None:
    """Two tool results in a row are one user turn; the API rejects them as two."""
    turns = _to_anthropic_messages(
        [
            Message(role="tool", content="one", tool_call_id="a"),
            Message(role="tool", content="two", tool_call_id="b"),
        ]
    )
    assert len(turns) == 1
    assert [block["tool_use_id"] for block in turns[0]["content"]] == ["a", "b"]


def test_an_empty_conversation_still_produces_a_turn() -> None:
    """The API rejects an empty list; a 400 is a worse answer than an empty turn."""
    assert _to_anthropic_messages([]) == [
        {"role": "user", "content": [{"type": "text", "text": ""}]}
    ]


def test_the_tool_schema_is_translated_from_either_nesting() -> None:
    wrapped = _to_anthropic_tool(
        {"type": "function", "function": {"name": "run", "description": "d", "parameters": {"x": 1}}}
    )
    bare = _to_anthropic_tool({"name": "run", "description": "d", "parameters": {"x": 1}})
    assert wrapped == bare == {"name": "run", "description": "d", "input_schema": {"x": 1}}


# ------------------------------------------------------------------------ the ledger


async def test_a_job_records_what_it_spent(client: httpx.AsyncClient, job) -> None:
    """Totals on the job row must equal the calls behind them.

    Written in one transaction for exactly this reason: a snapshot showing a total that
    disagrees with its own ledger is worse than showing no total.
    """
    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    usage = snapshot["usage"]
    assert usage["totals"]["calls"] > 0
    assert usage["totals"]["prompt"] == usage["totals"]["calls"] * 10
    assert usage["totals"]["completion"] == usage["totals"]["calls"] * 4
    assert usage["totals"]["total"] == usage["totals"]["prompt"] + usage["totals"]["completion"]

    ledger = await db.fetch_all(
        "select purpose, agent, total_tokens from token_usage where job_id=? order by id",
        (job_id,),
    )
    assert len(ledger) == usage["totals"]["calls"]
    assert sum(int(row["total_tokens"]) for row in ledger) == usage["totals"]["total"]
    # Planning, work and synthesis are all metered — the tool loop's turns included,
    # which is what the wrapper design buys.
    assert {row["purpose"] for row in ledger} == {"plan", "work", "synthesis"}

    standalone = (await client.get(f"/api/jobs/{job_id}/usage")).json()
    assert standalone["totals"] == usage["totals"]


async def test_usage_is_attributed_per_agent_and_per_model(
    client: httpx.AsyncClient, job
) -> None:
    """"Which agent burned the budget" is the question this breakdown answers."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    usage = (await client.get(f"/api/jobs/{job_id}/usage")).json()
    agents = {entry["agent"] for entry in usage["by_agent"]}
    assert {"manager", "coder", "tester", "architect"} & agents
    assert all(entry["total"] > 0 for entry in usage["by_agent"])
    assert [entry["model"] for entry in usage["by_model"]] == ["fake-1"]


async def test_phase_totals_add_up_to_the_job_total(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    rows = await db.fetch_all(
        "select total_tokens from phases where job_id=?", (job_id,)
    )
    phase_total = sum(int(row["total_tokens"] or 0) for row in rows)
    job_total = int(
        await db.fetch_value("select total_tokens from jobs where id=?", (job_id,), default=0)
    )
    assert phase_total == job_total > 0


async def test_a_broken_ledger_write_does_not_fail_the_phase(job) -> None:
    """The numbers are valuable; they are not the work."""
    meter = UsageMeter(db=db, job_id="no-such-job")  # violates the foreign key
    counts = await meter.record(
        completion=type("C", (), {"usage": {"prompt_tokens": 5}, "model": "m"})(),  # type: ignore[arg-type]
        provider_id="fake",
        requested_model="m",
    )
    assert counts["prompt"] == 5  # reported back even though nothing was stored


async def test_usage_of_a_job_that_never_ran_is_zero_not_missing() -> None:
    await db.execute(
        "insert into jobs(id,task,team_id,mode,status,paused,created_at,updated_at)"
        " values('empty','t',1,'controlled','queued',0,unixepoch('subsec'),unixepoch('subsec'))"
    )
    usage = await job_usage(db, "empty")
    assert usage == {
        "totals": {"prompt": 0, "completion": 0, "total": 0, "calls": 0},
        "by_agent": [],
        "by_model": [],
    }


# -------------------------------------------------------------- per-agent assignment


async def test_each_agent_can_be_given_its_own_provider_and_model(
    client: httpx.AsyncClient, job, pool
) -> None:
    """The customization the operator asked for, observed at the point of resolution."""
    job_id = await job(
        provider_id="fake",
        agents=[
            {"agent": "coder", "model": "fake-2"},
            {"agent": "tester", "provider_id": "fake", "model": "fake-1"},
        ],
    )
    assert await wait_for_job(job_id, "complete") == "complete"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["agent_providers"] == [
        {"agent": "coder", "provider_id": None, "model": "fake-2"},
        {"agent": "tester", "provider_id": "fake", "model": "fake-1"},
    ]
    # A model named without a provider keeps the job's provider rather than resolving
    # to nothing — that precedence is what makes "same endpoint, bigger model" sayable.
    assert ("fake", "fake-2") in pool.asked


async def test_the_model_an_agent_actually_used_is_on_its_phase_events(
    client: httpx.AsyncClient, job
) -> None:
    """Attribution has to be readable after the fact, not inferred from the plan."""
    job_id = await job(agents=[{"agent": "coder", "model": "fake-2"}])
    await wait_for_job(job_id, "complete")

    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    phases = [
        entry["payload"]
        for entry in history
        if entry["kind"] == "phase" and entry["payload"].get("status") == "active"
    ]
    assert phases, "no phase events recorded"
    assert all("model" in payload for payload in phases)


async def test_an_assignment_for_a_role_the_team_does_not_have_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """A typo'd role id would otherwise be silently ignored for the whole job."""
    response = await client.post(
        "/api/jobs",
        json={"task": "t", "agents": [{"agent": "nobody", "model": "fake-1"}]},
    )
    assert response.status_code == 400
    assert "not a role" in response.json()["detail"]


async def test_a_model_the_provider_does_not_serve_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """Caught at creation, naming the agent — not on the job's third phase."""
    response = await client.post(
        "/api/jobs",
        json={"task": "t", "agents": [{"agent": "coder", "model": "gpt-imaginary"}]},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "gpt-imaginary" in detail and "fake-1" in detail


async def test_a_disabled_provider_cannot_be_assigned_to_an_agent(
    client: httpx.AsyncClient,
) -> None:
    await client.put(
        "/api/providers/off",
        json={
            "id": "off",
            "label": "Off",
            "base_url": "https://example.test/v1",
            "model": "off-1",
            "models": [{"model": "off-1"}],
            "enabled": False,
        },
    )
    response = await client.post(
        "/api/jobs",
        json={"task": "t", "agents": [{"agent": "coder", "provider_id": "off"}]},
    )
    assert response.status_code == 400
    assert "coder" in response.json()["detail"]


async def test_the_same_agent_cannot_be_assigned_twice(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/jobs",
        json={
            "task": "t",
            "agents": [
                {"agent": "coder", "model": "fake-1"},
                {"agent": "coder", "model": "fake-2"},
            ],
        },
    )
    assert response.status_code == 422


async def test_an_assignment_that_pins_nothing_stores_nothing(
    client: httpx.AsyncClient, job
) -> None:
    """A row of nulls would show the operator an override that changes nothing."""
    job_id = await job(agents=[{"agent": "coder"}])
    await wait_for_job(job_id, "complete")
    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["agent_providers"] == []


# ------------------------------------------------------------------- continuation


async def test_a_finished_job_can_be_continued(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The chat stays open: a completed job takes another round in place."""
    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    before = await phase_rows(job_id)
    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["can_continue"] is True

    response = await client.post(
        f"/api/jobs/{job_id}/continue", json={"instruction": "now also check the login flow"}
    )
    assert response.status_code == 202
    assert response.json()["round"] == 2

    assert await wait_for_job(job_id, "complete") == "complete"
    after = await phase_rows(job_id)
    # The earlier rounds are untouched — their output is committed and other phases
    # have read it, so rewriting them would make the transcript disagree with history.
    assert [(row["seq"], row["status"]) for row in after][: len(before)] == [
        (row["seq"], row["status"]) for row in before
    ]
    assert len(after) > len(before)
    assert {int(row["round"]) for row in after} == {1, 2}

    # The follow-up instruction reached the planner rather than only being recorded.
    assert provider.prompts_containing("login flow")


async def test_a_continued_round_only_plans_the_new_ask(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """The planner is told what already happened, so round 2 is not round 1 again."""
    job_id = await job()
    await wait_for_job(job_id, "complete")
    await client.post(f"/api/jobs/{job_id}/continue", json={"instruction": "one more thing"})
    await wait_for_job(job_id, "complete")

    plan_prompts = provider.prompts_containing("Reply with JSON only")
    assert len(plan_prompts) == 2
    assert "follow-up" in plan_prompts[1].lower()
    # Round 1's output is quoted into round 2's planning prompt as context.
    assert "output for" in plan_prompts[1]


async def test_continuing_a_running_job_is_refused(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """A live job takes guidance through /messages; continuing would double-plan it."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    response = await client.post(f"/api/jobs/{job_id}/continue", json={"instruction": "x"})
    assert response.status_code == 409
    assert "/messages" in response.json()["detail"]

    provider.open_gate()
    await wait_for_job(job_id, "complete")


async def test_a_message_to_a_finished_job_points_at_continue(
    client: httpx.AsyncClient, job
) -> None:
    """The guard stays: a message here would sit unconsumed forever.

    Kept deliberately rather than removed. Spending another round of tokens should be
    an explicit action, and the detail names the endpoint that does it.
    """
    job_id = await job()
    await wait_for_job(job_id, "complete")

    response = await client.post(f"/api/jobs/{job_id}/messages", json={"content": "hello?"})
    assert response.status_code == 409
    assert "/continue" in response.json()["detail"]


async def test_a_failed_job_can_still_be_continued(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Retrying after a provider outage is the most likely reason to continue at all.

    A round scoped to itself is what makes this work: the error from round 1 is cleared
    and round 2's own phases decide the job's status.
    """
    provider.fail_after = 1
    job_id = await job()
    assert await wait_for_job(job_id, "error") == "error"

    provider.fail_after = None
    response = await client.post(f"/api/jobs/{job_id}/continue", json={"instruction": "try again"})
    assert response.status_code == 202
    assert await wait_for_job(job_id, "complete") == "complete"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert snapshot["error"] is None
    # Round 1's failure stays visible in the plan; it just no longer decides the job.
    assert any(row["status"] == "failed" for row in await phase_rows(job_id))


async def test_a_continued_job_reruns_its_agents_rather_than_leaving_them_complete(
    client: httpx.AsyncClient, job
) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")
    await client.post(f"/api/jobs/{job_id}/continue", json={"instruction": "again"})
    await wait_for_job(job_id, "complete")

    agents = (await client.get(f"/api/jobs/{job_id}/agents")).json()
    # Every agent that worked in round 2 ends terminal again — none stuck 'active'.
    assert all(entry["status"] in {"complete", "queued", "stopped"} for entry in agents)


async def test_usage_accumulates_across_rounds(client: httpx.AsyncClient, job) -> None:
    """One job, one bill — the ledger does not reset when a round does."""
    job_id = await job()
    await wait_for_job(job_id, "complete")
    first = (await client.get(f"/api/jobs/{job_id}/usage")).json()["totals"]["total"]

    await client.post(f"/api/jobs/{job_id}/continue", json={"instruction": "more"})
    await wait_for_job(job_id, "complete")
    second = (await client.get(f"/api/jobs/{job_id}/usage")).json()["totals"]["total"]

    assert second > first


# ------------------------------------------------------------------------- rerun


async def test_a_job_can_be_rerun_with_its_settings_inherited(
    client: httpx.AsyncClient, job
) -> None:
    """"Do that again, but…" — only what changes has to be sent."""
    job_id = await job(
        mode="yolo",
        agents=[{"agent": "coder", "model": "fake-2"}],
    )
    await wait_for_job(job_id, "complete")

    response = await client.post(f"/api/jobs/{job_id}/rerun", json={})
    assert response.status_code == 201
    forked = response.json()
    assert forked["id"] != job_id
    assert forked["forked_from"] == job_id
    assert forked["mode"] == "yolo"
    assert await wait_for_job(forked["id"], "complete") == "complete"

    snapshot = (await client.get(f"/api/jobs/{forked['id']}")).json()
    assert snapshot["task"] == (await client.get(f"/api/jobs/{job_id}")).json()["task"]
    # The per-agent assignments carry over, which is the whole point of inheriting.
    assert snapshot["agent_providers"] == [
        {"agent": "coder", "provider_id": None, "model": "fake-2"}
    ]


async def test_a_rerun_can_override_the_task_and_the_assignments(
    client: httpx.AsyncClient, job
) -> None:
    job_id = await job(agents=[{"agent": "coder", "model": "fake-2"}])
    await wait_for_job(job_id, "complete")

    forked = (
        await client.post(
            f"/api/jobs/{job_id}/rerun",
            json={"task": "a different task", "agents": []},
        )
    ).json()
    await wait_for_job(forked["id"], "complete")

    snapshot = (await client.get(f"/api/jobs/{forked['id']}")).json()
    assert snapshot["task"] == "a different task"
    assert snapshot["agent_providers"] == []  # an empty list clears rather than inherits


async def test_a_rerun_tells_an_absent_provider_from_an_explicit_null(
    client: httpx.AsyncClient, job
) -> None:
    """Two different meanings that both arrive as ``provider_id=None``.

    Absent means inherit. An explicit null means "clear the pin, use the server
    default" — which is what a re-run form showing the inherited provider needs in
    order to be able to unset it. Only ``model_fields_set`` can tell them apart, so
    the two cases are asserted side by side.
    """
    job_id = await job(provider_id="fake")
    await wait_for_job(job_id, "complete")

    inherited = (await client.post(f"/api/jobs/{job_id}/rerun", json={})).json()
    cleared = (
        await client.post(f"/api/jobs/{job_id}/rerun", json={"provider_id": None})
    ).json()
    await wait_for_job(inherited["id"], "complete")
    await wait_for_job(cleared["id"], "complete")

    assert inherited["provider_id"] == "fake"
    assert cleared["provider_id"] is None


async def test_a_rerun_leaves_the_original_alone(client: httpx.AsyncClient, job) -> None:
    """A fresh workspace and an untouched original are why this is not a new round."""
    job_id = await job()
    await wait_for_job(job_id, "complete")
    before = (await client.get(f"/api/jobs/{job_id}")).json()

    forked = (await client.post(f"/api/jobs/{job_id}/rerun", json={})).json()
    await wait_for_job(forked["id"], "complete")

    after = (await client.get(f"/api/jobs/{job_id}")).json()
    assert after["status"] == before["status"]
    assert after["usage"]["totals"] == before["usage"]["totals"]
    assert after["workspace"] != (await client.get(f"/api/jobs/{forked['id']}")).json()["workspace"]
    # The original's transcript says where the follow-up went.
    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    assert any(
        entry["kind"] == "notice" and forked["id"] in str(entry["payload"]) for entry in history
    )
