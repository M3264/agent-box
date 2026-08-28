"""Shared test fixtures.

Two things every test depends on:

- **A fresh database per test.** The app uses module-level singletons on purpose,
  so each test repoints ``db.path`` at a temporary file and runs the real lifespan
  around it. Exercising the actual startup path means migrations, provider
  bootstrap and job recovery are covered incidentally by every test.
- **A fake provider.** No test touches the network. ``FakeProvider`` is scripted
  per test and can block mid-phase, which is what makes the restart, pause and
  stop cases testable at all.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Set before app.config is imported: settings are frozen at import time.
_TMP = Path(tempfile.mkdtemp(prefix="agent-hub-tests-"))
os.environ["AGENT_HUB_DB"] = str(_TMP / "test.db")
os.environ["AGENT_HUB_WORKSPACES"] = str(_TMP / "workspaces")
# Point the codex fallback at nothing so provider bootstrap cannot pick up the
# developer's real credentials, and secret resolution is deterministic.
os.environ["AGENT_HUB_CODEX_CONFIG"] = str(_TMP / "no-such-config.toml")
os.environ["AGENT_HUB_LOG_FORMAT"] = "text"
os.environ["AGENT_HUB_LOG_LEVEL"] = "WARNING"
os.environ["AGENT_HUB_SHUTDOWN_GRACE"] = "2"
# Tools stay enabled so every test runs the real agent loop, but confinement is
# pinned to unconfined: whether bubblewrap works is a property of the host, not of
# the code under test, and `build_sandbox` refuses rather than degrading — so an
# unpinned default would turn a missing kernel feature into 65 failures. Commands
# in tests are harmless (`echo`, `cat`, `sleep`) and run in a temp workspace.
os.environ["AGENT_HUB_SANDBOX"] = "unconfined"
# Push fan-out is pinned off so the real lifespan every test runs never generates a
# VAPID key or schedules a send. The push tests opt in explicitly by calling
# `push.setup(vapid_file=..., enabled=True)` themselves.
os.environ["AGENT_HUB_PUSH"] = "0"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import db  # noqa: E402
from app import secret_store  # noqa: E402
from app.deps import engine  # noqa: E402
from app.events import broker, events  # noqa: E402
from app.main import app  # noqa: E402
from app.orchestrator import agentloop as agentloop_mod  # noqa: E402
from app.orchestrator import approvals as approvals_mod  # noqa: E402
from app.orchestrator import questions as questions_mod  # noqa: E402
from app.orchestrator import sandbox as sandbox_mod  # noqa: E402
from app.orchestrator import tools as tools_mod  # noqa: E402
from app.orchestrator import usage as usage_mod  # noqa: E402
from app.orchestrator import engine as engine_mod  # noqa: E402
from app.orchestrator import providers as providers_mod  # noqa: E402
from app.orchestrator.engine import TERMINAL_JOB_STATUSES  # noqa: E402
from app.orchestrator.providers import (  # noqa: E402
    Completion,
    Message,
    ProviderError,
    ToolCallRequest,
)

PLAN_MARKER = "Reply with JSON only"

DEFAULT_PLAN = {
    "phases": [
        {
            "name": "Design it",
            "owner": "architect",
            "acceptance": "a design exists",
            "requires_approval": False,
        },
        {
            "name": "Build it",
            "owner": "coder",
            "acceptance": "code exists",
            "requires_approval": False,
        },
        {
            "name": "Check it",
            "owner": "tester",
            "acceptance": "acceptance criteria are met",
            "requires_approval": False,
        },
    ],
    "notes": "straightforward",
}

#: One work phase, for any test that scripts a tool conversation: the planning and
#: synthesis calls are offered no tools, so they never consume a scripted turn and the
#: script lines up one-to-one with the loop's turns.
ONE_PHASE = {
    "phases": [
        {
            "name": "Do the work",
            "owner": "coder",
            "acceptance": "the commands ran and their output was read",
            "requires_approval": False,
        }
    ],
    "notes": "one phase, so the script and the turns line up",
}

#: Every module that imported `settings` by value and reads a limit off it. `tune`
#: repoints all of them, because which module reads which limit is an implementation
#: detail a test should not have to track.
_SETTINGS_READERS = (agentloop_mod, tools_mod, sandbox_mod, engine_mod, usage_mod, providers_mod)


def tune(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> Any:
    """Override the frozen settings everywhere they were imported.

    `Settings` is a frozen dataclass built from the environment at import time and
    each module holds its own reference, so a test can neither mutate it nor patch a
    single place. `replace` builds a new one from the current values — no environment
    is re-read — and every reader is repointed at it for the duration of the test.
    """
    tweaked = replace(settings, **changes)
    for module in _SETTINGS_READERS:
        monkeypatch.setattr(module, "settings", tweaked)
    return tweaked


@dataclass
class ScriptedTurn:
    """One turn of a tool conversation, as the fake provider will replay it.

    ``envelope`` renders the call as the text-shaped fallback documented in the
    system preamble instead of native ``tool_calls``, which is how the loop's
    fallback path is exercised without a provider that lacks function calling.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""
    envelope: bool = False


def tool(name: str, **args: Any) -> ScriptedTurn:
    """A turn that calls one tool natively."""
    return ScriptedTurn(calls=[(name, args)])


def tools_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> ScriptedTurn:
    """A turn that calls several tools at once, in order."""
    return ScriptedTurn(calls=list(calls), text=text)


def envelope(name: str, **args: Any) -> ScriptedTurn:
    """A turn that asks for a tool through the JSON text envelope."""
    return ScriptedTurn(calls=[(name, args)], envelope=True)


def says(text: str) -> ScriptedTurn:
    """A turn with no tool call — which ends the loop and becomes the phase output."""
    return ScriptedTurn(text=text)


class FakeProvider:
    """A scripted provider.

    ``gate`` lets a test hold a phase open indefinitely — the only way to observe
    mid-flight behaviour (restart, pause, stop, streaming) deterministically.
    ``blocked`` is set once a call is genuinely parked on the gate, so a test can
    wait for that instead of assuming the call has started. ``fail_after`` breaks a
    chosen call, which is how the failure paths are reached: the engine opens one
    provider per job, so a test cannot swap in a broken one mid-run.

    ``tool_script`` is consumed only by calls that were offered tools, which is
    exactly the tool loop — planning, synthesis and the loop's own closing summary
    all pass ``tools=None`` and fall through to the default text response. So a
    script describes the work phases and nothing else.
    """

    def __init__(self, plan: dict[str, Any] | None = None) -> None:
        self.id = "fake"
        self.model = "fake-1"
        self.plan = plan if plan is not None else DEFAULT_PLAN
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.gate: asyncio.Event | None = None
        self.gate_after: int = 0
        self.blocked = asyncio.Event()
        self.entered = 0
        self.cancelled = 0
        self.fail_after: int | None = None
        self.fail_with = "upstream returned 502"
        #: Whether a ``fail_after`` failure is tagged retryable. Default False keeps
        #: it fatal (the historical behaviour); True lets the outer wait-and-retry
        #: ring see it as transient.
        self.fail_retryable = False
        #: Fail the next N calls with a *retryable* error, then behave normally — a
        #: provider that blips and recovers. Decremented on each raise.
        self.flaky = 0
        self.tool_script: list[ScriptedTurn] = []
        #: Reported on every completion, so a test can assert the ledger without a live
        #: endpoint. Deliberately the OpenAI spelling — `normalize_usage` is tested
        #: directly for the other dialects.
        self.usage: dict[str, Any] = {"prompt_tokens": 10, "completion_tokens": 4}
        #: Every ``tools`` argument received, so a test can assert the schemas were
        #: actually offered rather than inferring it from behaviour.
        self.tools_seen: list[list[dict[str, Any]] | None] = []
        self.tool_calls_made = 0

    async def __aenter__(self) -> FakeProvider:
        self.entered += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def open_gate(self) -> None:
        """Release a parked call and stop gating subsequent ones."""
        if self.gate is not None:
            self.gate.set()
        self.gate = None
        self.blocked.clear()

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        self.systems.append(system)
        self.tools_seen.append(tools)

        if self.gate is not None and len(self.prompts) > self.gate_after:
            self.blocked.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

        if self.flaky > 0:
            self.flaky -= 1
            raise ProviderError(self.fail_with, retryable=True)

        if self.fail_after is not None and len(self.prompts) > self.fail_after:
            raise ProviderError(self.fail_with, retryable=self.fail_retryable)

        if PLAN_MARKER in prompt:
            return Completion(text=json.dumps(self.plan), model=self.model, usage=dict(self.usage))

        if tools and self.tool_script:
            return self._scripted(self.tool_script.pop(0))

        first_line = prompt.splitlines()[0] if prompt else ""
        return Completion(
            text=f"[{len(self.prompts)}] output for {first_line}",
            model=self.model,
            usage=dict(self.usage),
        )

    def _scripted(self, turn: ScriptedTurn) -> Completion:
        if not turn.calls:
            return Completion(
                text=turn.text or "nothing further", model=self.model, usage=dict(self.usage)
            )

        self.tool_calls_made += len(turn.calls)
        if turn.envelope:
            name, args = turn.calls[0]
            return Completion(
                text=json.dumps({"tool": name, "args": args}),
                model=self.model,
                usage=dict(self.usage),
            )
        return Completion(
            text=turn.text,
            model=self.model,
            usage=dict(self.usage),
            tool_calls=[
                ToolCallRequest(
                    id=f"call_{self.tool_calls_made}_{index}",
                    name=name,
                    arguments=json.dumps(args),
                )
                for index, (name, args) in enumerate(turn.calls)
            ],
            finish_reason="tool_calls",
        )

    async def wait_until_blocked(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.blocked.wait(), timeout)

    # -- helpers for reading back what the engine actually sent ---------------

    def prompts_containing(self, needle: str) -> list[str]:
        return [prompt for prompt in self.prompts if needle in prompt]


class FakePool:
    """Stands in for :class:`ProviderPool` and hands out one fake for every request.

    ``asked`` records each ``(provider_id, model)`` the engine resolved, which is how the
    per-agent assignment tests assert what *would* have been called without needing two
    live endpoints. The fake is shared across agents on purpose: tests script it as one
    conversation, and a per-agent instance would split that script.
    """

    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.asked: list[tuple[str | None, str | None]] = []
        #: Every job-default the engine repointed the pool at, in order. The engine calls
        #: ``set_default_provider`` at every phase boundary (with the unchanged id when
        #: nothing was retuned), so this records the switch a mid-run provider change makes.
        self.defaults: list[str | None] = []
        self.default_provider_id: str | None = None
        self.entered = 0

    async def __aenter__(self) -> FakePool:
        self.entered += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def profile(self, provider_id: str | None = None) -> dict[str, Any]:
        return {"id": provider_id or self.provider.id, "model": self.provider.model}

    async def get(
        self, provider_id: str | None = None, model: str | None = None
    ) -> FakeProvider:
        self.asked.append((provider_id, model))
        return self.provider

    def set_default_provider(self, provider_id: str | None) -> None:
        """Mirror the real pool's mid-run repoint so ``_reload_config`` can call it.

        The engine repoints the pool at every phase boundary; recording each one lets a
        retune test assert the pool was actually pointed at the switched-in provider, over
        and above seeing the resolved ids land in ``asked``.
        """
        self.default_provider_id = provider_id
        self.defaults.append(provider_id)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def pool(provider: FakeProvider) -> FakePool:
    return FakePool(provider)


@pytest.fixture(autouse=True)
def patch_provider(
    monkeypatch: pytest.MonkeyPatch, provider: FakeProvider, pool: FakePool
) -> FakeProvider:
    """Route every provider call to the fake.

    The seam is the pool, not ``build_provider``: the engine now opens one pool per job
    and asks it per phase, so patching the constructor is what keeps a single fake
    reachable from every agent regardless of what provider or model that agent named.
    ``build_provider`` itself is deliberately left alone, so the tests that exercise the
    real pool and the real adapters against a mock transport still reach them.
    """
    monkeypatch.setattr(engine_mod, "ProviderPool", lambda *a, **kw: pool)
    return provider


@pytest.fixture(autouse=True)
async def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    """Run the real application lifespan against a per-test database."""
    monkeypatch.setattr(db, "path", tmp_path / "agent-hub.db")
    # The secrets file is derived from `settings.db_path`, which is frozen at import and
    # does not follow the per-test `db.path` above. Repoint the store's live path resolver
    # so a key saved in one test can never leak into the next (and no test touches real
    # `data/`), which is the isolation the `provider_secrets_file` docstring promises.
    monkeypatch.setattr(secret_store, "_path", lambda: tmp_path / "provider_secrets.json")

    async with app.router.lifespan_context(app):
        await db.execute(
            "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,"
            "enabled,created_at)"
            " values('fake','Fake','openai_compatible','http://localhost:1/v1','fake-1',"
            "null,'{}',1,unixepoch('subsec'))"
        )
        # The model list matters now: a per-agent assignment naming a model the profile
        # does not serve is rejected at job creation, so a profile with an empty list
        # would make that check vacuous in every test.
        for model in ("fake-1", "fake-2"):
            await db.execute(
                "insert into provider_models(provider_id,model,label,supports_tools,created_at)"
                " values('fake',?,null,1,unixepoch('subsec'))",
                (model,),
            )
        try:
            yield
        finally:
            # Leaving a task or subscriber behind would silently couple tests.
            assert broker.subscriber_count() == 0, "a test leaked an event subscriber"

    assert engine.active_count == 0, "a test leaked a running job"
    approvals_mod.registry._waiters.clear()
    questions_mod.registry._waiters.clear()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
async def job(client: httpx.AsyncClient) -> AsyncIterator[Callable[..., Any]]:
    """Create jobs and guarantee they are stopped before the test ends."""
    created: list[str] = []

    async def _create(task: str = "Ship a status page", **payload: Any) -> str:
        response = await client.post("/api/jobs", json={"task": task, **payload})
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        created.append(job_id)
        return job_id

    yield _create

    for job_id in created:
        if engine.is_running(job_id):
            await engine.stop(job_id)


# --------------------------------------------------------------------- waiting
#
# The engine runs jobs in background tasks, so tests wait on state rather than
# sleeping for a guessed duration. Comparisons happen inside these helpers: an
# `await`-less predicate over a coroutine silently never matches.
#
# The 12s default is not arbitrary. `approvals.wait_for` polls the row every 5s as
# a safety net for a missed notification, so a gated job's worst case is one full
# poll interval per gate even when everything works. A deadline anywhere near 5s
# races that safety net and flakes under load instead of failing honestly.


async def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 12.0, interval: float = 0.01
) -> Any:
    """Poll until ``predicate`` returns something truthy. Awaits awaitable results."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Any = None
    while True:
        last = predicate()
        if inspect.isawaitable(last):
            last = await last
        if last:
            return last
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s (last value: {last!r})")
        await asyncio.sleep(interval)


async def wait_for_job(job_id: str, *statuses: str, timeout: float = 12.0) -> str:
    """Wait for a job to reach one of ``statuses``, returning the one it reached.

    For a terminal status this also waits for the matching ``status`` event to be
    durable. Every writer updates ``jobs.status`` before recording the event, so a
    helper that returned on the column alone would hand back control inside that
    window and any test then reading the event log would race it.
    """

    async def check() -> str | None:
        current = await db.fetch_value("select status from jobs where id=?", (job_id,))
        if current not in statuses:
            return None
        if current in TERMINAL_JOB_STATUSES and not await db.exists(
            "select 1 from events where job_id=? and kind='status'"
            " and json_extract(payload,'$.status')=?",
            (job_id, current),
        ):
            return None
        return current

    return await wait_until(check, timeout=timeout)


async def wait_for_phase(job_id: str, seq: int, *statuses: str, timeout: float = 12.0) -> str:
    async def check() -> str | None:
        current = await db.fetch_value(
            "select status from phases where job_id=? and seq=?", (job_id, seq)
        )
        return current if current in statuses else None

    return await wait_until(check, timeout=timeout)


async def wait_for_approval(job_id: str, timeout: float = 12.0) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        row = await db.fetch_one(
            "select * from approvals where job_id=? and status='pending'"
            " order by created_at desc limit 1",
            (job_id,),
        )
        return dict(row) if row else None

    return await wait_until(check, timeout=timeout)


async def wait_for_question(job_id: str, timeout: float = 12.0) -> dict[str, Any]:
    """Wait for an agent to be parked on a question, and return the row.

    Waits for the row rather than for the job's ``blocked`` status because the row is
    what an answer is posted against — and the two are written in the other order, so a
    helper keyed on the status would return before the question could be answered.
    """

    async def check() -> dict[str, Any] | None:
        row = await db.fetch_one(
            "select * from questions where job_id=? and status='pending'"
            " order by created_at desc limit 1",
            (job_id,),
        )
        return dict(row) if row else None

    return await wait_until(check, timeout=timeout)


async def phase_rows(job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all("select * from phases where job_id=? order by seq", (job_id,))
    return [dict(row) for row in rows]


async def event_kinds(job_id: str, kind: str) -> list[dict[str, Any]]:
    return [
        event.payload for event in await events.history(job_id, limit=5000) if event.kind == kind
    ]
