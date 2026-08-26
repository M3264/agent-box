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
report its work; the numbers are valuable, but they are not the work. The one exception
is deliberate: a job with a token budget is *stopped* by this module when it exhausts it,
which is the whole point of a budget as opposed to a report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.db import Database
from app.logging_setup import get_logger
from app.orchestrator.providers import Completion, Message, Provider, normalize_usage

log = get_logger("agent_hub.usage")

#: What a call was for. Free text in the column, but these are the values written.
PURPOSES = ("plan", "work", "synthesis")

#: Prices are quoted per million tokens, which is how every endpoint publishes them.
PRICE_UNIT = 1_000_000


class BudgetExceeded(Exception):
    """Raised instead of making a provider call that would exceed the job's budget.

    Raised by :class:`MeteredProvider`, which is the one place every call in the system
    passes through — the planner, the specialists, each turn of the tool loop, and the
    synthesis. Checking here rather than in the engine's phase loop is what makes the cap
    mean something: a single phase can make a dozen calls, and a budget only enforced
    between phases is a budget a runaway phase never notices.
    """


def resolve_budget(row_value: Any) -> int:
    """The cap for a job: its own if it has one, otherwise the server default.

    Null on the row means "whatever the default is when it runs", and 0 means "no cap" —
    which is why this cannot be a simple ``or``: an explicit 0 must survive a non-zero
    default.
    """
    if row_value is None:
        return max(0, int(settings.token_budget_default))
    return max(0, int(row_value))


@dataclass(slots=True)
class UsageMeter:
    """Records what each provider call cost, against one job."""

    db: Database
    job_id: str
    #: Total tokens this job may spend across every round. 0 means no cap.
    budget: int = 0
    #: What it has spent already, including previous rounds. Kept in step with the job
    #: row on every write so the check below never needs a query of its own.
    spent: int = field(default=0)

    @property
    def over_budget(self) -> bool:
        return bool(self.budget) and self.spent >= self.budget

    def budget_error(self) -> BudgetExceeded:
        return BudgetExceeded(
            f"token budget exhausted: {self.spent:,} of {self.budget:,} tokens used. "
            "Raise the budget on the job to carry on."
        )

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

        # Counted even if the write above failed. The tokens were spent either way, and a
        # broken ledger is not a reason to let a capped job keep spending.
        self.spent += counts["total"]
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
        # Checked before the call, not after: a cap that only notices once the money is
        # spent is a receipt. The last call before a breach is therefore allowed to
        # overshoot — the alternative would be predicting a response's token count, which
        # nothing can do, and refusing calls on a guess would stop jobs that were fine.
        if self._meter.over_budget:
            log.info(
                "refusing a provider call over budget",
                extra={
                    "job_id": self._meter.job_id,
                    "spent": self._meter.spent,
                    "budget": self._meter.budget,
                    "agent": self._agent,
                },
            )
            raise self._meter.budget_error()

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


async def model_prices(db: Database) -> dict[tuple[str, str], tuple[float | None, float | None]]:
    """``(provider_id, model) -> (price_in, price_out)`` per million tokens.

    Read at presentation time rather than stamped onto each ledger row at call time.
    That is a deliberate trade: correcting a price re-prices history, which is the
    behaviour you want from a tool whose real question is "what is this costing me" —
    and the alternative, a frozen price per row, mostly preserves an old typo.
    """
    rows = await db.fetch_all(
        "select provider_id, model, price_in, price_out from provider_models"
        " where price_in is not null or price_out is not null"
    )
    return {
        (str(row["provider_id"]), str(row["model"])): (row["price_in"], row["price_out"])
        for row in rows
    }


def estimate_cost(
    prompt: int, completion: int, price_in: float | None, price_out: float | None
) -> float | None:
    """Cost of one bucket of tokens, or None when the model has no price on file.

    Unknown is not zero. A model nobody has priced must read as unpriced in the UI,
    because folding it in at zero would quietly under-report the total — the one number
    an operator is most likely to trust without checking.

    Cached prompt tokens are billed at the input price here. Discounts for them vary per
    endpoint and are not modelled, which makes this an over-estimate rather than an
    under-estimate on providers that discount them — the safer direction to be wrong in.
    """
    if price_in is None and price_out is None:
        return None
    total = 0.0
    if price_in is not None:
        total += prompt * price_in / PRICE_UNIT
    if price_out is not None:
        total += completion * price_out / PRICE_UNIT
    return round(total, 6)


async def job_usage(db: Database, job_id: str) -> dict[str, Any]:
    """The usage summary the API puts on a job snapshot.

    ``totals`` comes off the job row rather than being summed here so the cheap path
    stays cheap; ``by_agent`` and ``by_model`` are aggregates over the ledger, which is
    where "which agent burned the budget" is actually answerable.
    """
    row = await db.fetch_one(
        "select prompt_tokens,completion_tokens,total_tokens,provider_calls,token_budget"
        " from jobs where id=?",
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

    prices = await model_prices(db)
    by_model: list[dict[str, Any]] = []
    cost = 0.0
    priced_any = False
    unpriced_tokens = 0
    for entry in await db.fetch_all(
        "select provider_id, model, count(*) as calls, sum(prompt_tokens) as prompt_tokens,"
        " sum(completion_tokens) as completion_tokens, sum(total_tokens) as total_tokens"
        " from token_usage where job_id=? group by provider_id, model"
        " order by total_tokens desc",
        (job_id,),
    ):
        prompt = int(entry["prompt_tokens"] or 0)
        completion = int(entry["completion_tokens"] or 0)
        price_in, price_out = prices.get(
            (str(entry["provider_id"]), str(entry["model"])), (None, None)
        )
        estimate = estimate_cost(prompt, completion, price_in, price_out)
        if estimate is None:
            unpriced_tokens += int(entry["total_tokens"] or 0)
        else:
            priced_any = True
            cost += estimate
        by_model.append(
            {
                "provider_id": entry["provider_id"],
                "model": entry["model"],
                "calls": int(entry["calls"]),
                "prompt": prompt,
                "completion": completion,
                "total": int(entry["total_tokens"] or 0),
                "cost": estimate,
            }
        )

    limit = resolve_budget(row["token_budget"] if row else None)
    return {
        "totals": totals,
        "by_agent": by_agent,
        "by_model": by_model,
        # None rather than 0 when nothing is priced, so the UI can say "no prices on
        # file" instead of claiming a job was free.
        "cost": round(cost, 6) if priced_any else None,
        "unpriced_tokens": unpriced_tokens,
        "budget": {
            "limit": limit,
            "used": totals["total"],
            "remaining": max(0, limit - totals["total"]) if limit else None,
        },
    }
