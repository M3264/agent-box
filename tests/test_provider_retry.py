"""Surviving a provider outage: the wait-and-retry ring around a call.

A job used to die the instant a provider hiccupped — one exhausted call raised, the
phase was marked failed, and every phase queued behind it was skipped. That is the
behaviour these tests exist to change. ``complete_with_retry`` turns a *transient* fault
— a 5xx, a rate limit, a dropped connection, a CDN/WAF error page, a timeout — into a
wait-and-try-again, while something a wait cannot fix — a bad request, a missing key, an
unusable profile — still fails at once.

The tests pin down that split, the give-up bound, the escalating delay, that an operator
stop interrupts a wait promptly, and — end to end — that a job actually survives a
provider that blips and recovers, leaving a visible notice rather than a silent hang.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.orchestrator import providers as providers_mod
from app.orchestrator.providers import (
    Completion,
    Message,
    OpenAICompatibleProvider,
    ProviderConfigError,
    ProviderError,
    _provider_retry_delay,
    complete_with_retry,
    complete_with_timeout,
)
from tests.conftest import event_kinds, tune, wait_for_job


class ScriptedProvider:
    """A provider that plays a fixed sequence of outcomes and counts its calls.

    Each outcome is either an exception to raise or a ``Completion`` to return. The last
    outcome repeats once the list runs out, so "always fails" is a one-item script and
    the call count — the thing most of these tests actually assert — stays the point.
    """

    id = "scripted"
    model = "scripted-1"

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.count = 0

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> Completion:
        self.count += 1
        outcome = self._outcomes[min(self.count - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, Completion)
        return outcome


def _recorder() -> tuple[list[tuple[int, float, str]], object]:
    """An ``on_wait`` that just remembers every (attempt, delay, error) it was told."""
    seen: list[tuple[int, float, str]] = []

    async def on_wait(attempt: int, delay: float, exc: ProviderError) -> None:
        seen.append((attempt, delay, str(exc)))

    return seen, on_wait


def _ask(provider: object, **kw: object) -> object:
    """The one call shape every unit test here makes."""
    return complete_with_retry(
        provider, system="be brief", messages=[Message("user", "hi")], **kw
    )


DONE = Completion(text="recovered", model="scripted-1")
BLIP = lambda: ProviderError("503 upstream busy", retryable=True)  # noqa: E731


# --------------------------------------------------------------- the retry/give-up split


async def test_a_retryable_error_is_waited_out_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    tune(monkeypatch, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider = ScriptedProvider([BLIP(), BLIP(), DONE])
    seen, on_wait = _recorder()

    result = await _ask(provider, on_wait=on_wait)

    assert result is DONE
    assert provider.count == 3, "two failures then the call that worked"
    assert [attempt for attempt, _, _ in seen] == [1, 2], "one wait per failure, none after success"


async def test_a_non_retryable_error_fails_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    tune(monkeypatch, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider = ScriptedProvider([ProviderError("context too long", retryable=False)])
    seen, on_wait = _recorder()

    with pytest.raises(ProviderError, match="context too long"):
        await _ask(provider, on_wait=on_wait)

    assert provider.count == 1, "a wait cannot fix a bad request, so do not spend one"
    assert seen == []


async def test_a_config_error_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing secret or absent profile is wrong now and wrong in a minute."""
    tune(monkeypatch, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider = ScriptedProvider([ProviderConfigError("secret not found")])

    with pytest.raises(ProviderConfigError):
        await _ask(provider)

    assert provider.count == 1


async def test_it_gives_up_after_the_attempt_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    tune(monkeypatch, provider_retry_attempts=3, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider = ScriptedProvider([BLIP()])
    seen, on_wait = _recorder()

    with pytest.raises(ProviderError, match="upstream busy"):
        await _ask(provider, on_wait=on_wait)

    assert provider.count == 3, "three attempts, then surface the outage honestly"
    assert [attempt for attempt, _, _ in seen] == [1, 2], "waited between attempts, not after the last"


async def test_disabled_means_a_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch: off restores the old one-call-and-fail behaviour."""
    tune(monkeypatch, provider_retry_enabled=False)
    provider = ScriptedProvider([BLIP()])
    seen, on_wait = _recorder()

    with pytest.raises(ProviderError, match="upstream busy"):
        await _ask(provider, on_wait=on_wait)

    assert provider.count == 1
    assert seen == []


async def test_a_stop_interrupts_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator stop set during a wait ends it at once, before another attempt."""
    tune(monkeypatch, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider = ScriptedProvider([BLIP()])
    cancel = asyncio.Event()

    async def on_wait(attempt: int, delay: float, exc: ProviderError) -> None:
        cancel.set()  # the operator stops the job mid-wait

    with pytest.raises(asyncio.CancelledError):
        await _ask(provider, cancel=cancel, on_wait=on_wait)

    assert provider.count == 1, "cancelled before a second attempt was made"


async def test_the_delay_escalates_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    tune(monkeypatch, provider_retry_base_delay=10, provider_retry_max_delay=60)
    assert [_provider_retry_delay(n) for n in range(1, 7)] == [10, 20, 40, 60, 60, 60]


# --------------------------------------------- classification comes from the adapter itself


def _adapter(client: httpx.AsyncClient) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        id="t", base_url="https://provider.invalid/v1", model="t-1", secret="s", client=client
    )


async def _call_against(handler: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with _adapter(http) as provider:
            await provider.complete(system="be brief", messages=[Message("user", "hi")])


async def test_exhausted_transient_status_is_tagged_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_mod, "RETRY_BACKOFF", (0.0, 0.0))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "busy"})

    with pytest.raises(ProviderError) as excinfo:
        await _call_against(handler)
    assert excinfo.value.retryable is True, "a run of 503s is exactly what waiting is for"


async def test_a_hard_4xx_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_mod, "RETRY_BACKOFF", (0.0, 0.0))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "context too long"}})

    with pytest.raises(ProviderError) as excinfo:
        await _call_against(handler)
    assert excinfo.value.retryable is False


async def test_a_timeout_is_tagged_retryable() -> None:
    class Hang:
        id = "hang"
        model = "hang-1"

        async def complete(self, **_: object) -> Completion:
            await asyncio.sleep(30)
            raise AssertionError("should have timed out")

    with pytest.raises(ProviderError) as excinfo:
        await complete_with_timeout(
            Hang(), system="be brief", messages=[Message("user", "hi")], timeout=0.01
        )
    assert excinfo.value.retryable is True
    assert "exceeded" in str(excinfo.value)


# ------------------------------------------------------------------- the whole job survives


async def test_job_survives_a_provider_that_blips_and_recovers(
    client: httpx.AsyncClient, job, provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end the user asked for: a small provider issue no longer ends the job.

    The first two model calls fail transiently; the retry ring waits (zero seconds here)
    and tries again, the planner's call lands, and the job runs to completion — with a
    notice on the timeline so the delay was visible rather than a silent stall.
    """
    tune(monkeypatch, provider_retry_base_delay=0, provider_retry_max_delay=0)
    provider.flaky = 2

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    retry_notices = [n for n in await event_kinds(job_id, "notice") if "provider_retry" in n]
    assert retry_notices, "each wait should leave a visible notice on the timeline"
