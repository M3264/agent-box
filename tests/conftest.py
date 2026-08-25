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

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db import db  # noqa: E402
from app.deps import engine  # noqa: E402
from app.events import broker, events  # noqa: E402
from app.main import app  # noqa: E402
from app.orchestrator import approvals as approvals_mod  # noqa: E402
from app.orchestrator import engine as engine_mod  # noqa: E402
from app.orchestrator.engine import TERMINAL_JOB_STATUSES  # noqa: E402
from app.orchestrator.providers import Completion, Message, ProviderError  # noqa: E402

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


class FakeProvider:
    """A scripted provider.

    ``gate`` lets a test hold a phase open indefinitely — the only way to observe
    mid-flight behaviour (restart, pause, stop, streaming) deterministically.
    ``blocked`` is set once a call is genuinely parked on the gate, so a test can
    wait for that instead of assuming the call has started. ``fail_after`` breaks a
    chosen call, which is how the failure paths are reached: the engine opens one
    provider per job, so a test cannot swap in a broken one mid-run.
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
    ) -> Completion:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        self.systems.append(system)

        if self.gate is not None and len(self.prompts) > self.gate_after:
            self.blocked.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

        if self.fail_after is not None and len(self.prompts) > self.fail_after:
            raise ProviderError(self.fail_with)

        if PLAN_MARKER in prompt:
            return Completion(text=json.dumps(self.plan), model=self.model)
        first_line = prompt.splitlines()[0] if prompt else ""
        return Completion(text=f"[{len(self.prompts)}] output for {first_line}", model=self.model)

    async def wait_until_blocked(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.blocked.wait(), timeout)

    # -- helpers for reading back what the engine actually sent ---------------

    def prompts_containing(self, needle: str) -> list[str]:
        return [prompt for prompt in self.prompts if needle in prompt]


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture(autouse=True)
def patch_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> FakeProvider:
    """Route every provider call to the fake, in the engine and at job creation."""
    monkeypatch.setattr(engine_mod, "build_provider", lambda profile, client=None: provider)
    return provider


@pytest.fixture(autouse=True)
async def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    """Run the real application lifespan against a per-test database."""
    monkeypatch.setattr(db, "path", tmp_path / "agent-hub.db")

    async with app.router.lifespan_context(app):
        await db.execute(
            "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,"
            "enabled,created_at)"
            " values('fake','Fake','openai_compatible','http://localhost:1/v1','fake-1',"
            "null,'{}',1,unixepoch('subsec'))"
        )
        try:
            yield
        finally:
            # Leaving a task or subscriber behind would silently couple tests.
            assert broker.subscriber_count() == 0, "a test leaked an event subscriber"

    assert engine.active_count == 0, "a test leaked a running job"
    approvals_mod.registry._waiters.clear()


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


async def phase_rows(job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all("select * from phases where job_id=? order by seq", (job_id,))
    return [dict(row) for row in rows]


async def event_kinds(job_id: str, kind: str) -> list[dict[str, Any]]:
    return [
        event.payload for event in await events.history(job_id, limit=5000) if event.kind == kind
    ]
