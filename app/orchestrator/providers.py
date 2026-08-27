"""Provider adapters.

v1 called ``runtime.provider()``, which read ``~/.codex/config.toml`` directly on
every run and ignored the ``provider_profiles`` table, so both the Settings screen
and the ``provider_id`` request field were decorative.

Here a job resolves its profile from the database, and the secret is resolved
server-side from ``secret_ref`` — environment first, the codex config only as a
fallback so the existing agentrouter profile keeps working. Secret *values* never
enter the database and are never returned by the API.

Two axes, deliberately kept apart:

- **Kind** is the wire protocol, and there is one adapter class per kind. Adding a
  kind means writing code, so the catalogue lives here rather than in the database.
- **Template** is a preset that fills a profile's fields in for a known vendor. It
  needs no code, which is why there are many of them and why "which API does this
  endpoint speak" stops being something an operator has to work out by watching a
  job fail.

A profile is also no longer one model. ``provider_profiles.model`` is its *default*
model and ``provider_models`` holds the rest; a caller asks for a model by name and
gets an adapter bound to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import httpx

from app.config import settings
from app.db import Database
from app.logging_setup import get_logger

log = get_logger("agent_hub.providers")


class ProviderError(RuntimeError):
    """A provider call failed in a way worth surfacing to the operator.

    ``retryable`` says whether waiting could plausibly help: True for a transient
    fault (a 5xx, a rate limit, a dropped connection, a CDN/WAF error page, a
    timeout), False for something a wait cannot fix (a bad request, a missing
    key). :func:`complete_with_retry` reads it to decide between waiting and
    giving up. It defaults False so anything raised without a considered opinion
    fails fast rather than looping.
    """

    def __init__(self, *args: object, retryable: bool = False) -> None:
        super().__init__(*args)
        self.retryable = retryable


class ProviderConfigError(ProviderError):
    """The provider profile is unusable — missing secret, no profile, etc.

    Never retryable: the profile is wrong, and it will still be wrong in a minute.
    """


#: Statuses that say "ask again later", not "your request was wrong".
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})
#: Total attempts per call, including the first.
MAX_ATTEMPTS = 3
#: Seconds to wait before attempt 2 and attempt 3.
RETRY_BACKOFF = (2.0, 6.0)

#: Anthropic pins its request/response shape to a dated version header.
ANTHROPIC_VERSION = "2023-06-01"
#: The Messages API requires ``max_tokens``; OpenAI-compatible endpoints do not.
ANTHROPIC_DEFAULT_MAX_TOKENS = 8192


# --------------------------------------------------------------------------- kinds

#: The wire protocols with an adapter behind them.
#:
#: Each entry describes the dialect in the terms an operator sees elsewhere — most
#: CLI agents and gateways describe themselves as "OpenAI compatible", so that is
#: the label used here too. ``models_path`` is what discovery calls; a kind whose
#: endpoint has no listing sets it to None and the operator types model ids in.
PROVIDER_KINDS: tuple[dict[str, Any], ...] = (
    {
        "id": "openai_compatible",
        "label": "OpenAI compatible",
        "detail": (
            "POST {base_url}/chat/completions with a Bearer token. What OpenAI, "
            "OpenRouter, Groq, Together, DeepSeek, vLLM, llama.cpp, LM Studio and "
            "most gateways speak."
        ),
        "auth": "Authorization: Bearer <secret>",
        "models_path": "/models",
        "supports_tools": True,
    },
    {
        "id": "anthropic",
        "label": "Anthropic Messages",
        "detail": (
            "POST {base_url}/messages with an x-api-key header and a version header. "
            "Tool calls use content blocks rather than a tool_calls array."
        ),
        "auth": "x-api-key: <secret>",
        "models_path": "/models",
        "supports_tools": True,
    },
)

KIND_IDS = frozenset(kind["id"] for kind in PROVIDER_KINDS)

#: Presets, not protocols. Picking one fills in the fields a new profile needs; the
#: operator can still change every one of them afterwards. ``models`` are seeds for
#: the model list, used when the endpoint has no discoverable listing or discovery
#: is not reachable from this host.
PROVIDER_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "label": "OpenAI",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "secret_ref": "OPENAI_API_KEY",
        "models": ["gpt-5.2", "gpt-5.2-mini", "gpt-4.1", "o4-mini"],
        "headers": {},
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "secret_ref": "ANTHROPIC_API_KEY",
        "models": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "headers": {},
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "kind": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "secret_ref": "OPENROUTER_API_KEY",
        "models": [],
        "headers": {},
    },
    {
        "id": "groq",
        "label": "Groq",
        "kind": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "secret_ref": "GROQ_API_KEY",
        "models": [],
        "headers": {},
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "kind": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "secret_ref": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "headers": {},
    },
    {
        "id": "together",
        "label": "Together AI",
        "kind": "openai_compatible",
        "base_url": "https://api.together.xyz/v1",
        "secret_ref": "TOGETHER_API_KEY",
        "models": [],
        "headers": {},
    },
    {
        "id": "mistral",
        "label": "Mistral",
        "kind": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "secret_ref": "MISTRAL_API_KEY",
        "models": [],
        "headers": {},
    },
    {
        "id": "gemini_openai",
        "label": "Google Gemini (OpenAI endpoint)",
        "kind": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "secret_ref": "GEMINI_API_KEY",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "headers": {},
    },
    {
        "id": "ollama",
        "label": "Ollama (local)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "secret_ref": None,
        "models": [],
        "headers": {},
    },
    {
        "id": "lmstudio",
        "label": "LM Studio (local)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:1234/v1",
        "secret_ref": None,
        "models": [],
        "headers": {},
    },
    {
        "id": "vllm",
        "label": "vLLM / self-hosted",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:8000/v1",
        "secret_ref": None,
        "models": [],
        "headers": {},
    },
    {
        "id": "custom",
        "label": "Something else, OpenAI compatible",
        "kind": "openai_compatible",
        "base_url": "https://",
        "secret_ref": None,
        "models": [],
        "headers": {},
    },
)


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Flatten whatever an endpoint reported into one shape.

    OpenAI-compatible endpoints send ``prompt_tokens``/``completion_tokens``;
    Anthropic sends ``input_tokens``/``output_tokens``; both nest their cached and
    reasoning counts differently, and plenty of gateways send neither. Every field
    is optional, so a missing count reads as 0 rather than breaking accounting.
    """
    if not isinstance(usage, dict):
        return {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "reasoning": 0}

    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    def nested(section: str, *keys: str) -> int:
        detail = usage.get(section)
        if not isinstance(detail, dict):
            return 0
        for key in keys:
            value = detail.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    prompt = number("prompt_tokens", "input_tokens")
    completion = number("completion_tokens", "output_tokens")
    cached = number("cached_tokens", "cache_read_input_tokens") or nested(
        "prompt_tokens_details", "cached_tokens"
    )
    reasoning = number("reasoning_tokens") or nested(
        "completion_tokens_details", "reasoning_tokens"
    )
    total = number("total_tokens") or (prompt + completion)
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "cached": cached,
        "reasoning": reasoning,
    }



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

        # Everything that reaches here is transient — a network error, or a
        # status that passed the _retryable() check above — so tag it as such
        # for the outer wait-and-retry ring. A fatal status already raised inside
        # the loop with retryable left False.
        if failure is None:
            failure = ProviderError("provider call failed", retryable=True)
        else:
            failure.retryable = True
        raise failure

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


class AnthropicProvider:
    """Adapter for the Anthropic Messages API.

    Kept as its own class rather than a flag on the OpenAI adapter because three
    things differ in ways that do not compose: authentication is a header of its
    own, ``max_tokens`` is required rather than optional, and tool use is expressed
    as content blocks inside a message instead of a sibling ``tool_calls`` array.
    Translating in both directions is the whole job of this class, so the engine and
    the tool loop keep speaking one internal shape.
    """

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
        max_tokens: int = ANTHROPIC_DEFAULT_MAX_TOKENS,
    ) -> None:
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._secret = secret
        self._headers = headers or {}
        self._client = client
        self._owns_client = client is None
        self.supports_tools = supports_tools
        self._max_tokens = max_tokens

    async def __aenter__(self) -> AnthropicProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.provider_timeout, connect=settings.provider_connect_timeout
                )
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
        if self._client is None:
            raise ProviderConfigError("provider used outside its async context")

        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            **self._headers,
        }
        if self._secret:
            headers["x-api-key"] = self._secret

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": temperature,
            "messages": _to_anthropic_messages(messages),
        }
        if system:
            body["system"] = system
        if tools and self.supports_tools:
            body["tools"] = [_to_anthropic_tool(tool) for tool in tools]

        failure: ProviderError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    f"{self.base_url}/messages", headers=headers, json=body
                )
            except httpx.HTTPError as exc:
                failure = ProviderError(f"provider request failed: {exc}")
            else:
                if response.status_code < 400:
                    return self._parse(response)
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

        # Everything that reaches here is transient — a network error, or a
        # status that passed the _retryable() check above — so tag it as such
        # for the outer wait-and-retry ring. A fatal status already raised inside
        # the loop with retryable left False.
        if failure is None:
            failure = ProviderError("provider call failed", retryable=True)
        else:
            failure.retryable = True
        raise failure

    def _parse(self, response: httpx.Response) -> Completion:
        try:
            data = response.json()
            blocks = data["content"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected provider response shape: {exc}") from exc
        if not isinstance(blocks, list):
            raise ProviderError("unexpected provider response shape: content is not a list")

        texts: list[str] = []
        calls: list[ToolCallRequest] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCallRequest(
                        id=str(block.get("id") or f"call_{index}"),
                        name=str(block["name"]),
                        arguments=json.dumps(block.get("input") or {}),
                    )
                )

        text = "\n".join(part for part in texts if part)
        if not text and not calls:
            raise ProviderError("provider returned an empty message")

        return Completion(
            text=text,
            model=data.get("model", self.model),
            usage=data.get("usage") or {},
            tool_calls=calls,
            finish_reason=data.get("stop_reason"),
        )


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Translate one OpenAI-shaped function schema into Anthropic's tool shape.

    The tool schemas live in ``tools.py`` in OpenAI's form because that is what most
    endpoints take; this is the only place that has to know the other spelling.
    """
    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate the internal conversation into Anthropic's block form.

    Two shape rules the Messages API enforces and the OpenAI form does not:
    a tool result is a *user* message carrying a ``tool_result`` block rather than
    its own ``tool`` role, and same-role turns must be merged rather than repeated.
    Both are handled here so no caller has to care which dialect it is talking to.
    """
    turns: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    for message in messages:
        if message.role == "tool":
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                ],
            )
            continue

        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            arguments = call.args()
            # `args()` returns a marker dict when the model's own JSON was
            # unparseable. Echoing that back is more honest than dropping the block:
            # the conversation stays well-formed and the model sees what it sent.
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": arguments,
                }
            )
        # A system message reaching here means a caller put one in the list rather
        # than the `system` parameter; Anthropic has no system role, so it is folded
        # into the user turn instead of being silently discarded.
        append("user" if message.role == "system" else message.role, blocks)

    if not turns:
        # The API rejects an empty conversation; an empty user turn is closer to the
        # caller's intent than a 400.
        return [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    return turns


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


def kind_spec(kind: str | None) -> dict[str, Any]:
    """The catalogue entry for a kind, defaulting to the OpenAI dialect.

    Profiles written before the kind column existed have no kind, and an operator can
    only ever pick from the catalogue, so an unknown value means the row predates the
    column rather than that it is wrong.
    """
    for entry in PROVIDER_KINDS:
        if entry["id"] == kind:
            return entry
    return PROVIDER_KINDS[0]


#: One adapter class per wire protocol. Adding a kind means adding a class, which is
#: exactly why this mapping is code and not configuration.
_ADAPTERS: dict[str, Any] = {
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(
    profile: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    *,
    model: str | None = None,
) -> Provider:
    """Build an adapter for one profile, optionally bound to a non-default model.

    ``model`` is what makes a profile more than a single model: the profile supplies
    the endpoint, the credentials and the dialect, and the caller says which model on
    that endpoint it wants. Falling back to ``profile["model"]`` keeps every existing
    caller — and every job created before per-agent assignment — working unchanged.
    """
    kind = profile.get("kind") or "openai_compatible"
    adapter = _ADAPTERS.get(kind)
    if adapter is None:
        raise ProviderConfigError(f"unsupported provider kind '{kind}'")

    chosen = (model or profile.get("model") or "").strip()
    if not chosen:
        raise ProviderConfigError(
            f"provider '{profile['id']}' has no model; add one in Settings"
        )

    return adapter(
        id=profile["id"],
        base_url=profile["base_url"],
        model=chosen,
        secret=resolve_secret(profile.get("secret_ref"), profile["id"]),
        headers=profile.get("headers") or {},
        client=client,
        # Defaults on for profiles predating the column.
        supports_tools=bool(profile.get("supports_tools", 1)),
    )


class ProviderPool:
    """The adapters one job needs, built once and shared.

    A job used to resolve exactly one provider for its whole run. Now each agent may
    name its own provider and model, so a phase asks the pool for what its owner
    should speak to and gets a cached adapter back — one per distinct
    ``(provider, model)`` pair, not one per phase.

    All of them share a single ``httpx.AsyncClient`` owned by the pool, so connection
    reuse survives across agents and there is exactly one thing to close. Adapters are
    constructed with that client, which means their own ``__aenter__``/``__aexit__``
    are never needed and they never close a connection another agent is using.
    """

    __slots__ = ("_db", "_default_provider_id", "_client", "_owns_client", "_profiles", "_adapters")

    def __init__(
        self,
        database: Database,
        *,
        default_provider_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._db = database
        self._default_provider_id = default_provider_id
        self._client = client
        self._owns_client = client is None
        self._profiles: dict[str | None, dict[str, Any]] = {}
        self._adapters: dict[tuple[str, str], Provider] = {}

    async def __aenter__(self) -> ProviderPool:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.provider_timeout, connect=settings.provider_connect_timeout
                )
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
        self._adapters.clear()

    async def profile(self, provider_id: str | None = None) -> dict[str, Any]:
        """The profile row for an id, cached for the life of the pool.

        Cached because a job with six phases would otherwise re-read and re-resolve
        the same secret six times. A profile disabled mid-job therefore keeps working
        until the job ends, which is the same guarantee the single-provider version
        gave.
        """
        key = provider_id or self._default_provider_id
        if key not in self._profiles:
            self._profiles[key] = await load_profile(self._db, key)
        return self._profiles[key]

    async def get(self, provider_id: str | None = None, model: str | None = None) -> Provider:
        """An adapter for one ``(provider, model)`` pair.

        A pinned provider that has since been disabled or deleted raises rather than
        quietly falling back to the job's default. Running an agent on a model the
        operator did not choose, without saying so, is the same class of mistake as
        running a command unsandboxed because the sandbox was missing.
        """
        profile = await self.profile(provider_id)
        chosen = (model or profile.get("model") or "").strip()
        key = (profile["id"], chosen)
        if key not in self._adapters:
            self._adapters[key] = build_provider(profile, self._client, model=chosen)
        return self._adapters[key]


async def discover_models(
    profile: dict[str, Any], client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Ask an endpoint which models it serves.

    This is the other half of the detection problem: knowing the dialect tells you how
    to call an endpoint, and this tells you what to call it with, so an operator does
    not have to paste model ids from documentation that may not match what their
    gateway actually proxies.

    Returns ``[{"model", "label"}]``. Raises ``ProviderError`` when the endpoint has no
    listing or refuses the request — the caller shows that reason and the operator adds
    models by hand, which every provider form still allows.
    """
    spec = kind_spec(profile.get("kind"))
    path = spec.get("models_path")
    if not path:
        raise ProviderError(f"{spec['label']} endpoints have no model listing to read")

    secret = resolve_secret(profile.get("secret_ref"), profile["id"])
    headers = dict(profile.get("headers") or {})
    if secret:
        if spec["id"] == "anthropic":
            headers["x-api-key"] = secret
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            headers["Authorization"] = f"Bearer {secret}"

    url = f"{str(profile['base_url']).rstrip('/')}{path}"
    owns = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=settings.provider_connect_timeout)
    )
    try:
        response = await http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await http.aclose()

    if response.status_code >= 400:
        raise ProviderError(
            f"{url} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{url} did not return JSON: {exc}") from exc

    # OpenAI wraps the list in `data`; a few self-hosted servers return a bare array.
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ProviderError(f"{url} returned an unexpected shape")

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            model, label = entry, None
        elif isinstance(entry, dict):
            model = entry.get("id") or entry.get("name") or entry.get("model")
            label = entry.get("display_name") or entry.get("label")
        else:
            continue
        model = str(model or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        found.append({"model": model, "label": str(label).strip() if label else None})

    found.sort(key=lambda item: item["model"])
    return found


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

    model = data.get("model") or "gpt-5.6-sol"
    async with database.transaction() as conn:
        await conn.execute(
            "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,enabled,created_at)"
            " values(?,?,?,?,?,?,?,1,unixepoch('subsec'))",
            (
                name,
                name.replace("_", " ").title(),
                "openai_compatible",
                base_url.rstrip("/"),
                model,
                "experimental_bearer_token" if "experimental_bearer_token" in section else None,
                json.dumps({"originator": "codex_cli_rs"}),
            ),
        )
        # The seeded default is also the profile's first selectable model, so the model
        # picker is never empty on a fresh install.
        await conn.execute(
            "insert into provider_models(provider_id,model,label,supports_tools,created_at)"
            " values(?,?,null,1,unixepoch('subsec'))",
            (name, model),
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
        raise ProviderError(f"provider call exceeded {limit:.0f}s", retryable=True) from exc


#: Told before each wait: (attempt-that-just-failed, seconds-until-retry, error).
OnWait = Callable[[int, float, ProviderError], Awaitable[None]]


def _provider_retry_delay(attempt: int) -> float:
    """Seconds to wait before re-attempting a call that failed transiently.

    Escalating and capped: a blip clears in the first short wait, while a longer
    outage settles into steady re-checks rather than a busy-loop. ``attempt`` is 1
    for the wait after the first failure, 2 after the second, and so on.
    """
    base = max(0.0, float(settings.provider_retry_base_delay))
    cap = max(base, float(settings.provider_retry_max_delay))
    return min(base * (2 ** (attempt - 1)), cap)


async def _sleep_or_cancelled(delay: float, cancel: asyncio.Event | None) -> None:
    """Wait ``delay`` seconds, but wake at once if ``cancel`` is set.

    If ``cancel`` is (or becomes) set, raise ``CancelledError`` so a stop unwinds
    the phase exactly as any other stop does, rather than the wait swallowing it.
    """
    if cancel is None:
        await asyncio.sleep(delay)
        return
    if not cancel.is_set():
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({waiter}, timeout=delay)
        finally:
            waiter.cancel()
    if cancel.is_set():
        raise asyncio.CancelledError()


async def complete_with_retry(
    provider: Provider,
    *,
    system: str,
    messages: list[Message],
    temperature: float = 0.2,
    timeout: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    cancel: asyncio.Event | None = None,
    on_wait: OnWait | None = None,
) -> Completion:
    """Wait out a provider outage instead of letting one blip kill a job.

    ``complete()`` already retries a few-second hiccup inside a single call. This
    is the ring beyond that: when a call still fails with a *retryable* error — a
    5xx, a rate limit, a dropped connection, a CDN/WAF error page, a timeout — the
    whole call is retried after an escalating wait, so a provider that is down for
    a minute or ten costs a job time rather than the job itself. A non-retryable
    error (a bad request, a missing key, an unusable profile) is raised at once:
    waiting cannot fix it, and looping on it would only hide it.

    Retrying here is safe the same way ``complete()``'s own retry is: a provider
    call has no side effects. The caller's conversation is untouched, so nothing
    already done in a tool loop is repeated — only the next turn's round-trip is
    re-tried. That is why this wraps the *call* and never the tool loop around it.

    The wait is interruptible: a set ``cancel`` (an operator stop, or shutdown)
    ends it at once with ``CancelledError``. ``on_wait(attempt, delay, error)`` —
    if given — is awaited before each wait so the caller can tell the operator
    what is happening; it is guarded, so a failing notice never ends the job.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await complete_with_timeout(
                provider,
                system=system,
                messages=messages,
                temperature=temperature,
                timeout=timeout,
                tools=tools,
            )
        except ProviderError as exc:
            exhausted = (
                settings.provider_retry_attempts > 0
                and attempt >= settings.provider_retry_attempts
            )
            if not settings.provider_retry_enabled or not exc.retryable or exhausted:
                raise
            delay = _provider_retry_delay(attempt)
            log.warning(
                "provider unavailable; waiting before another attempt",
                extra={
                    "provider": getattr(provider, "id", "?"),
                    "attempt": attempt,
                    "retry_in": delay,
                    "error": str(exc),
                },
            )
            if on_wait is not None:
                try:
                    await on_wait(attempt, delay, exc)
                except Exception:  # pragma: no cover - a notice must never end a job
                    log.warning("provider-retry wait notice failed", exc_info=True)
            await _sleep_or_cancelled(delay, cancel)
