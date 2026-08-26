"""Token accounting.

Every provider response already carried a ``usage`` object and every one of them was
parsed and thrown away, so a job that cost four million tokens looked exactly like one
that cost four thousand. This module is the ledger.

Two ideas, and the split between them is the whole design:

- :class:`UsageMeter` writes. One row per provider call into ``token_usage``, plus the
  running totals on ``jobs`` and ``phases``, in a single transaction — a snapshot can
  never show a total that disagrees with the calls behind it.
- :class:`MeteredProvider` is a transparent wrapper around any provider. The engine and
  the tool loop keep calling ``complete()`` exactly as before, which is why metering
  covers the tool loop's turns without the tool loop knowing this file exists.

Accounting never fails a phase. A job whose ledger write breaks should still finish and
report its work; the numbers are valuable, but they are not the work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.logging_setup import get_logger
from app.orchestrator.providers import Completion, Message, Provider, normalize_usage

log = get_logger("agent_hub.usage")

#: What a call was for. Free text in the column, but these are the values written.
PURPOSES = ("plan", "work", "synthesis")


@dataclass(slots=True)
class UsageMeter:
    """Records what each provider call cost, against one job."""

    db: Database
    job_id: str

    async def record(
        self,
        *,
        completion: Completion,
        provider_id: str,
        requested_model: str,
        phase_id: int | None = None,
        agent: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, int]:
        """Write one call to the ledger and add it to the job and phase totals.

        ``completion.model`` rather than ``requested_model`` is stored when the
        endpoint reported one: a gateway that silently substitutes a model should be
        visible in the ledger instead of hidden by what was asked for.
        """
        counts = normalize_usage(completion.usage)
        model = (completion.model or requested_model or "").strip() or None

        # A provider that reports nothing still gets a row. The call happened, and a
        # gap in the ledger would read as "no calls" rather than "no numbers".
        try:
            async with self.db.transaction() as conn:
                await conn.execute(
                    "insert into token_usage(job_id,phase_id,agent,provider_id,model,purpose,"
                    "prompt_tokens,completion_tokens,total_tokens,cached_tokens,reasoning_tokens,"
                    "created_at) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.job_id,
                        phase_id,
                        agent,
                        provider_id,
                        model,
                        purpose,
                        counts["prompt"],
                        counts["completion"],
                        counts["total"],
                        counts["cached"],
                        counts["reasoning"],
                        time.time(),
                    ),
                )
                await conn.execute(
                    "update jobs set prompt_tokens=prompt_tokens+?, "
                    "completion_tokens=completion_tokens+?, total_tokens=total_tokens+?, "
                    "provider_calls=provider_calls+1 where id=?",
                    (counts["prompt"], counts["completion"], counts["total"], self.job_id),
                )
                if phase_id is not None:
                    await conn.execute(
                        "update phases set prompt_tokens=prompt_tokens+?, "
                        "completion_tokens=completion_tokens+?, total_tokens=total_tokens+? "
                        "where id=?",
                        (counts["prompt"], counts["completion"], counts["total"], phase_id),
                    )
        except Exception:  # pragma: no cover - accounting must not fail a phase
            log.warning(
                "could not record token usage",
                extra={"job": self.job_id, "phase": phase_id, "agent": agent},
                exc_info=True,
            )

        return counts


class MeteredProvider:
    """A provider that writes every call it makes to the ledger.

    Deliberately a wrapper rather than a hook inside each adapter: there are two
    adapters now and there will be more, and metering that lives in one of them is
    metering that silently stops when an operator switches dialect.
    """

    __slots__ = ("_inner", "_meter", "_phase_id", "_agent", "_purpose", "id", "model")

    def __init__(
        self,
        inner: Provider,
        meter: UsageMeter,
        *,
        phase_id: int | None = None,
        agent: str | None = None,
        purpose: str | None = None,
    ) -> None:
        self._inner = inner
        self._meter = meter
        self._phase_id = phase_id
        self._agent = agent
        self._purpose = purpose
        # Mirrored, not proxied via __getattr__, because the Provider protocol
        # declares them and callers read them for logging and event payloads.
        self.id = inner.id
        self.model = inner.model

    @property
    def supports_tools(self) -> bool:
        return bool(getattr(self._inner, "supports_tools", True))

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        completion = await self._inner.complete(
            system=system,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )
        await self._meter.record(
            completion=completion,
            provider_id=self.id,
            requested_model=self.model,
            phase_id=self._phase_id,
            agent=self._agent,
            purpose=self._purpose,
        )
        return completion


async def job_usage(db: Database, job_id: str) -> dict[str, Any]:
    """The usage summary the API puts on a job snapshot.

    ``totals`` comes off the job row rather than being summed here so the cheap path
    stays cheap; ``by_agent`` and ``by_model`` are aggregates over the ledger, which is
    where "which agent burned the budget" is actually answerable.
    """
    row = await db.fetch_one(
        "select prompt_tokens,completion_tokens,total_tokens,provider_calls from jobs where id=?",
        (job_id,),
    )
    totals = {
        "prompt": int(row["prompt_tokens"]) if row else 0,
        "completion": int(row["completion_tokens"]) if row else 0,
        "total": int(row["total_tokens"]) if row else 0,
        "calls": int(row["provider_calls"]) if row else 0,
    }

    by_agent = [
        {
            "agent": entry["agent"],
            "calls": int(entry["calls"]),
            "prompt": int(entry["prompt_tokens"]),
            "completion": int(entry["completion_tokens"]),
            "total": int(entry["total_tokens"]),
        }
        for entry in await db.fetch_all(
            "select agent, count(*) as calls, sum(prompt_tokens) as prompt_tokens,"
            " sum(completion_tokens) as completion_tokens, sum(total_tokens) as total_tokens"
            " from token_usage where job_id=? group by agent order by total_tokens desc",
            (job_id,),
        )
    ]

    by_model = [
        {
            "provider_id": entry["provider_id"],
            "model": entry["model"],
            "calls": int(entry["calls"]),
            "total": int(entry["total_tokens"]),
        }
        for entry in await db.fetch_all(
            "select provider_id, model, count(*) as calls, sum(total_tokens) as total_tokens"
            " from token_usage where job_id=? group by provider_id, model"
            " order by total_tokens desc",
            (job_id,),
        )
    ]

    return {"totals": totals, "by_agent": by_agent, "by_model": by_model}
