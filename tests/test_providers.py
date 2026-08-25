"""The OpenAI-compatible adapter, in particular its retry policy.

A live run lost a five-phase job to a single HTML error page from the provider's
CDN — an HTTP 405 that said nothing about the request. These tests pin down which
failures are worth another attempt and which are the operator's problem.
"""

from __future__ import annotations

import httpx
import pytest

from app.orchestrator import providers as providers_mod
from app.orchestrator.providers import (
    MAX_ATTEMPTS,
    Message,
    OpenAICompatibleProvider,
    ProviderError,
)

WAF_PAGE = "<!doctypehtml><html><title>405</title><body>not allowed</body></html>"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry timing out of the test runtime."""
    monkeypatch.setattr(providers_mod, "RETRY_BACKOFF", (0.0, 0.0))


def build(client: httpx.AsyncClient) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        id="test",
        base_url="https://provider.invalid/v1",
        model="test-1",
        secret="s3cret",
        headers={"originator": "codex_cli_rs"},
        client=client,
    )


def reply(text: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": text}}], "model": "test-1"}
    )


async def call(handler) -> str:  # noqa: ANN001
    """Run one completion against a mock transport, closing the client after."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with build(http) as provider:
            result = await provider.complete(
                system="be brief", messages=[Message("user", "hi")]
            )
    return result.text


async def test_transient_status_is_retried_then_succeeds() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, json={"error": "upstream busy"})
        return reply("done")

    assert await call(handler) == "done"
    assert len(seen) == 3


async def test_a_gateway_error_page_is_retried() -> None:
    """The status is 405, but the body proves the API never saw the request."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(405, text=WAF_PAGE, headers={"content-type": "text/html"})
        return reply("recovered")

    assert await call(handler) == "recovered"
    assert len(seen) == 2


async def test_a_real_api_rejection_is_not_retried() -> None:
    """A JSON 400 is the operator's problem; hammering it three times helps nobody."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(400, json={"error": {"message": "context too long"}})

    with pytest.raises(ProviderError, match="context too long"):
        await call(handler)
    assert len(seen) == 1, "a genuine 4xx must fail on the first attempt"


async def test_retries_are_bounded() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(502, json={"error": "bad gateway"})

    with pytest.raises(ProviderError, match="HTTP 502"):
        await call(handler)
    assert len(seen) == MAX_ATTEMPTS


async def test_connection_failures_are_retried() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 2:
            raise httpx.ConnectError("connection reset", request=request)
        return reply("after reconnect")

    assert await call(handler) == "after reconnect"
    assert len(seen) == 2


async def test_the_secret_and_profile_headers_reach_the_endpoint() -> None:
    """The profile's `headers` column is load-bearing: without ``originator`` the
    live endpoint answers 401 'unauthorized client detected'."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return reply("ok")

    await call(handler)
    assert captured["authorization"] == "Bearer s3cret"
    assert captured["originator"] == "codex_cli_rs", "profile headers must reach the endpoint"
