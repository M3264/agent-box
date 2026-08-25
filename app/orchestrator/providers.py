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
class ToolCallRequest:
    """One tool the model wants run.

    ``arguments`` is kept as the raw string the provider sent, because that is what
    has to be echoed back verbatim in the assistant message for the conversation to
    stay well-formed. ``args()`` is the parsed view, and it is deliberately
    forgiving: a model that emits slightly-off JSON should produce a tool error the
    model can read and correct, not an exception that kills the phase.
    """

    id: str
    name: str
    arguments: str = "{}"

    def args(self) -> dict[str, Any]:
        if not self.arguments.strip():
            return {}
        try:
            parsed = parse_json_response(self.arguments)
        except ProviderError:
            return {"__unparsed__": self.arguments}
        return parsed if isinstance(parsed, dict) else {"__unparsed__": self.arguments}

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant | tool
    content: str
    #: Set on an assistant message that asked for tools. Echoed back unchanged.
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    #: Set on a ``role="tool"`` message, tying the result to the request.
    tool_call_id: str | None = None
    #: The tool's name. Not required by the spec, but some endpoints reject a tool
    #: message without it.
    name: str | None = None

    def wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.wire() for call in self.tool_calls]
            # An assistant turn that only calls tools has no prose. Sending "" is
            # accepted more widely than null, so only the empty string is used.
            payload["content"] = self.content or ""
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None

    def json(self) -> Any:
        """Parse the completion as JSON, tolerating fenced or prose-wrapped output."""
        return parse_json_response(self.text)

    def as_message(self) -> Message:
        """This completion as the assistant message to append to the conversation."""
        return Message(role="assistant", content=self.text, tool_calls=list(self.tool_calls))


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
        tools: list[dict[str, Any]] | None = None,
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
        supports_tools: bool = True,
    ) -> None:
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._secret = secret
        self._headers = headers or {}
        self._client = client
        self._owns_client = client is None
        self.supports_tools = supports_tools

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
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        """Call the endpoint, retrying transient failures.

        Without this, one hiccup from a proxied endpoint ends a whole job: a live
        run lost a five-phase job to a single HTML error page served by the
        provider's CDN.

        Retrying is safe *here* and only here. The provider call itself has no side
        effects, so replaying it costs tokens and nothing else — but the tool loop
        wrapped around it does have side effects, so it is never retried as a unit.
        """
        if self._client is None:
            raise ProviderConfigError("provider used outside its async context")

        headers = dict(self._headers)
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [message.wire() for message in messages],
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        # Omitted entirely for profiles flagged as not supporting function calling:
        # some proxies 400 on an unknown parameter rather than ignoring it, which
        # would take out text-only jobs too.
        if tools and self.supports_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

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
            choice = data["choices"][0]
            message = choice["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected provider response shape: {exc}") from exc

        text = message.get("content")
        calls = _parse_tool_calls(message.get("tool_calls"))

        # An assistant turn that only calls tools has null content, which is
        # correct rather than empty — so "empty" is only an error when there is no
        # tool call either.
        if text is None and not calls:
            raise ProviderError("provider returned an empty message")

        return Completion(
            text=text or "",
            model=data.get("model", self.model),
            usage=data.get("usage") or {},
            tool_calls=calls,
            finish_reason=choice.get("finish_reason"),
        )


def _parse_tool_calls(raw: Any) -> list[ToolCallRequest]:
    """Read the ``tool_calls`` array, skipping anything malformed.

    Tolerant on purpose: this endpoint is a proxy in front of several upstreams, and
    one unrecognised entry should cost that call, not the job.
    """
    if not isinstance(raw, list):
        return []
    calls: list[ToolCallRequest] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function") or {}
        name = function.get("name") or entry.get("name")
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):  # some proxies pre-parse it
            arguments = json.dumps(arguments)
        calls.append(
            ToolCallRequest(
                id=str(entry.get("id") or f"call_{index}"),
                name=str(name),
                arguments=str(arguments or "{}"),
            )
        )
    return calls


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
        # Defaults on for profiles predating the column.
        supports_tools=bool(profile.get("supports_tools", 1)),
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
    tools: list[dict[str, Any]] | None = None,
) -> Completion:
    limit = timeout or (settings.provider_timeout + 30)
    try:
        async with asyncio.timeout(limit):
            return await provider.complete(
                system=system, messages=messages, temperature=temperature, tools=tools
            )
    except TimeoutError as exc:
        raise ProviderError(f"provider call exceeded {limit:.0f}s") from exc
