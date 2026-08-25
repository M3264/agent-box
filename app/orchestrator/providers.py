"""Provider adapters.

v1 called ``runtime.provider()``, which read ``~/.codex/config.toml`` directly on
every run and ignored the ``provider_profiles`` table, so both the Settings screen
and the ``provider_id`` request field were decorative.

Here a job resolves its profile from the database, and the secret is resolved
server-side from ``secret_ref`` — environment first, the codex config only as a
fallback so the existing agentrouter profile keeps working. Secret *values* never
enter the database and are never returned by the API.

The ``Provider`` protocol keeps the call site provider-agnostic; only the
OpenAI-compatible adapter is implemented, since that is what the configured
endpoint speaks.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import settings
from app.db import Database
from app.logging_setup import get_logger

log = get_logger("agent_hub.providers")


class ProviderError(RuntimeError):
    """A provider call failed in a way worth surfacing to the operator."""


class ProviderConfigError(ProviderError):
    """The provider profile is unusable — missing secret, no profile, etc."""


#: Statuses that say "ask again later", not "your request was wrong".
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})
#: Total attempts per call, including the first.
MAX_ATTEMPTS = 3
#: Seconds to wait before attempt 2 and attempt 3.
RETRY_BACKOFF = (2.0, 6.0)


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the completion as JSON, tolerating fenced or prose-wrapped output."""
        return parse_json_response(self.text)


class Provider(Protocol):
    id: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Completion: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json_response(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Models wrap JSON in prose or code fences often enough that a bare
    ``json.loads`` makes planning brittle. Tries the whole string, then fenced
    blocks, then the outermost brace/bracket span.
    """
    candidates: list[str] = [text.strip()]
    candidates.extend(match.strip() for match in _FENCE.findall(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ProviderError(f"response was not JSON: {text[:400]}")


def resolve_secret(secret_ref: str | None, profile_id: str) -> str | None:
    """Resolve a secret by reference. Never logs or returns it via the API."""
    if not secret_ref:
        return None

    for key in (secret_ref, secret_ref.upper(), f"AGENT_HUB_{secret_ref.upper()}"):
        value = os.environ.get(key, "").strip()
        if value:
            return value

    # Fallback: the codex config this deployment already had in place.
    config = settings.codex_config
    if config.exists():
        try:
            with config.open("rb") as handle:
                data = tomllib.load(handle)
            section = data.get("model_providers", {}).get(profile_id, {})
            value = str(section.get(secret_ref, "") or "").strip()
            if value:
                return value
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("could not read fallback secret source", extra={"error": str(exc)})

    raise ProviderConfigError(
        f"secret '{secret_ref}' for provider '{profile_id}' not found in the environment"
        f" or {config}"
    )


def _retryable(response: httpx.Response) -> bool:
    """Is this error worth another attempt?

    Beyond the usual transient statuses, a response that is not JSON did not come
    from the API at all — it is a CDN or WAF error page, and its status code says
    nothing about the request. The live 405 that killed a job was an Aliyun error
    page of exactly that shape. A genuine API-level 4xx stays fatal.
    """
    if response.status_code in RETRYABLE_STATUSES:
        return True
    return "json" not in response.headers.get("content-type", "").lower()


class OpenAICompatibleProvider:
    """Adapter for any ``/chat/completions`` endpoint."""
    def __init__(
        self,
        *,
        id: str,
        base_url: str,
        model: str,
        secret: str | None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._secret = secret
        self._headers = headers or {}
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OpenAICompatibleProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.provider_timeout, connect=settings.provider_connect_timeout)
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Completion:
        """Call the endpoint, retrying transient failures.

        Without this, one hiccup from a proxied endpoint ends a whole job: a live
        run lost a five-phase job to a single HTML error page served by the
        provider's CDN. LLM calls have no side effects, so replaying one is safe.
        """
        if self._client is None:
            raise ProviderConfigError("provider used outside its async context")

        headers = dict(self._headers)
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens

        failure: ProviderError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=body
                )
            except httpx.HTTPError as exc:
                failure = ProviderError(f"provider request failed: {exc}")
            else:
                if response.status_code < 400:
                    return self._parse(response)
                # Include a bounded slice of the body: upstream errors are the
                # single most common cause of a failed job and were previously
                # invisible.
                failure = ProviderError(
                    f"provider returned HTTP {response.status_code}: {response.text[:500]}"
                )
                if not _retryable(response):
                    raise failure

            if attempt < MAX_ATTEMPTS:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)) - 1]
                log.warning(
                    "retrying provider call",
                    extra={
                        "provider": self.id,
                        "attempt": attempt,
                        "of": MAX_ATTEMPTS,
                        "retry_in": delay,
                        "error": str(failure),
                    },
                )
                await asyncio.sleep(delay)

        raise failure if failure is not None else ProviderError("provider call failed")

    def _parse(self, response: httpx.Response) -> Completion:
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected provider response shape: {exc}") from exc

        if text is None:
            raise ProviderError("provider returned an empty message")

        return Completion(text=text, model=data.get("model", self.model), usage=data.get("usage") or {})


async def load_profile(database: Database, provider_id: str | None = None) -> dict[str, Any]:
    """Fetch a provider profile row, falling back to any enabled profile."""
    if provider_id:
        row = await database.fetch_one(
            "select * from provider_profiles where id=? and enabled=1", (provider_id,)
        )
        if row is None:
            raise ProviderConfigError(f"provider profile '{provider_id}' not found or disabled")
    else:
        row = await database.fetch_one(
            "select * from provider_profiles where enabled=1 order by rowid limit 1"
        )
        if row is None:
            raise ProviderConfigError(
                "no enabled provider profile is configured; add one in Settings"
            )

    profile = dict(row)
    try:
        profile["headers"] = json.loads(profile.get("headers") or "{}")
    except json.JSONDecodeError:
        profile["headers"] = {}
    return profile


def build_provider(profile: dict[str, Any], client: httpx.AsyncClient | None = None) -> Provider:
    kind = profile.get("kind") or "openai_compatible"
    if kind != "openai_compatible":
        raise ProviderConfigError(f"unsupported provider kind '{kind}'")
    return OpenAICompatibleProvider(
        id=profile["id"],
        base_url=profile["base_url"],
        model=profile["model"],
        secret=resolve_secret(profile.get("secret_ref"), profile["id"]),
        headers=profile.get("headers") or {},
        client=client,
    )


async def bootstrap_profiles(database: Database) -> None:
    """Seed a profile from the codex config on first start, if one is discoverable.

    Keeps the existing deployment working without manual setup. Runs only when the
    table is empty, so it never overwrites operator-managed profiles.
    """
    if await database.exists("select 1 from provider_profiles limit 1"):
        return

    config = settings.codex_config
    if not config.exists():
        log.info("no provider profiles and no codex config to seed from", extra={"path": str(config)})
        return

    try:
        with config.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse codex config", extra={"error": str(exc)})
        return

    name = data.get("model_provider") or "agentrouter"
    section = data.get("model_providers", {}).get(name, {})
    base_url = section.get("base_url")
    if not base_url:
        log.info("codex config has no usable provider section", extra={"provider": name})
        return

    await database.execute(
        "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,enabled,created_at)"
        " values(?,?,?,?,?,?,?,1,unixepoch('subsec'))",
        (
            name,
            name.replace("_", " ").title(),
            "openai_compatible",
            base_url.rstrip("/"),
            data.get("model") or "gpt-5.6-sol",
            "experimental_bearer_token" if "experimental_bearer_token" in section else None,
            json.dumps({"originator": "codex_cli_rs"}),
        ),
    )
    log.info("seeded provider profile from codex config", extra={"provider": name, "base_url": base_url})


# Guard against a provider hanging past its timeout and wedging a job.
async def complete_with_timeout(
    provider: Provider,
    *,
    system: str,
    messages: list[Message],
    temperature: float = 0.2,
    timeout: float | None = None,
) -> Completion:
    limit = timeout or (settings.provider_timeout + 30)
    try:
        async with asyncio.timeout(limit):
            return await provider.complete(system=system, messages=messages, temperature=temperature)
    except TimeoutError as exc:
        raise ProviderError(f"provider call exceeded {limit:.0f}s") from exc
