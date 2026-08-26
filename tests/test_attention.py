"""The operator's inbox.

Every wait in this system was already durable and every one of them was per-job: an
approval on the Approvals tab, a question on the job's stream, a queued message in the
composer. The question an operator actually has is never "show me approvals", it is "is
anything waiting for me" — and with five jobs running, answering that meant opening five
job screens.

So the endpoint is cross-job and mixed, and these tests are mostly about the joins and
the ordering, because that is where an inbox goes wrong. An item without its job's task
is not actionable; an item whose job has since been stopped is worse than useless,
because answering it does nothing and the operator cannot tell. Both are asserted.

The counts are asserted separately from the arrays on purpose: a badge reads
``counts.blocking``, and a queued message deliberately does not raise it — nothing is
held up by something the operator has not sent yet.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx

from app.db import db
from tests.conftest import (
    DEFAULT_PLAN,
    ONE_PHASE,
    FakeProvider,
    says,
    tool,
    wait_for_approval,
    wait_for_job,
    wait_for_question,
)

QUESTION = "Which bucket should the nightly backups go to?"

GUIDANCE = "Keep the retention at 30 days."


def gating_plan() -> dict[str, Any]:
    plan = copy.deepcopy(DEFAULT_PLAN)
    plan["phases"][0]["requires_approval"] = True
    return plan


async def inbox(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/attention", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def gated_job(job, provider: FakeProvider, task: str) -> tuple[str, dict[str, Any]]:
    """A job parked on an approval gate.

    A parked job makes no provider calls, which is what lets a single shared fake serve
    several jobs in one test: the provider can be rescripted for the next job while this
    one waits.
    """
    provider.plan = gating_plan()
    job_id = await job(task)
    return job_id, await wait_for_approval(job_id)


async def asking_job(job, provider: FakeProvider, task: str) -> tuple[str, dict[str, Any]]:
    """A job parked on a question from one of its agents."""
    provider.plan = ONE_PHASE
    provider.tool_script = [
        tool("ask_operator", question=QUESTION, options=["Primary", "Archive"]),
        says("Using the primary bucket, as you said."),
    ]
    job_id = await job(task)
    return job_id, await wait_for_question(job_id)


async def test_the_inbox_gathers_every_kind_of_wait_across_jobs(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    gated_id, gate = await gated_job(job, provider, "Rotate the backup keys")
    queued = await client.post(f"/api/jobs/{gated_id}/messages", json={"content": GUIDANCE})
    assert queued.status_code == 201, queued.text
    asking_id, question = await asking_job(job, provider, "Ship the backup job")

    waiting = await inbox(client)

    assert waiting["counts"] == {
        "approvals": 1,
        "questions": 1,
        "messages": 1,
        # The two kinds that actually hold work up. A message the operator has not sent
        # yet holds up nothing, and a permanent badge for that is noise.
        "blocking": 2,
    }

    approval = waiting["approvals"][0]
    assert (approval["id"], approval["job_id"]) == (gate["id"], gated_id)
    assert approval["job_task"] == "Rotate the backup keys"
    assert approval["job_status"] == "blocked"
    assert (approval["phase_name"], approval["phase_seq"]) == ("Design it", 1)
    assert approval["auto"] is False, "an int here renders as a truthy 0 in the UI"

    ask = waiting["questions"][0]
    assert (ask["id"], ask["job_id"]) == (question["id"], asking_id)
    assert ask["question"] == QUESTION
    assert ask["job_task"] == "Ship the backup job"
    assert ask["phase_name"] == "Do the work", "which phase is stuck is half the context"
    # Parsed, not the stored JSON blob: the inbox renders these as buttons.
    assert [entry["value"] for entry in ask["options"]] == ["primary", "archive"]
    assert ask["allow_free_text"] is True

    message = waiting["messages"][0]
    assert (message["job_id"], message["content"]) == (gated_id, GUIDANCE)
    assert message["delivery"] == "boundary", "the inbox has to offer 'send it now'"
    assert message["job_task"] == "Rotate the backup keys"

    # Answer and approve, so nothing is left parked for the teardown to stop.
    assert (
        await client.post(
            f"/api/jobs/{asking_id}/questions/{question['id']}/answer",
            json={"chosen": "primary"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/jobs/{gated_id}/approvals/{gate['id']}", json={"decision": "approved"}
        )
    ).status_code == 200
    assert await wait_for_job(asking_id, "complete") == "complete"
    assert await wait_for_job(gated_id, "complete") == "complete"


async def test_the_inbox_is_a_queue_so_the_oldest_wait_comes_first(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Oldest first, unlike the job list.

    This is a work queue, and the gate that has been holding a job up for twenty minutes
    matters more than the one raised four seconds ago.
    """
    first_id, first_gate = await gated_job(job, provider, "The one that has been waiting")
    second_id, second_gate = await gated_job(job, provider, "The one just raised")

    waiting = await inbox(client)
    assert [row["job_id"] for row in waiting["approvals"]] == [first_id, second_id]
    assert waiting["approvals"][0]["created_at"] <= waiting["approvals"][1]["created_at"]

    # The limit keeps the oldest rather than whatever the database happened to scan
    # first, which is the only truncation that is safe for a queue.
    capped = await inbox(client, limit=1)
    assert [row["job_id"] for row in capped["approvals"]] == [first_id]
    assert capped["counts"]["approvals"] == 1, "the count describes what was returned"
    assert (await client.get("/api/attention", params={"limit": 0})).status_code == 422

    for job_id, gate in ((first_id, first_gate), (second_id, second_gate)):
        await client.post(f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "rejected"})
        assert await wait_for_job(job_id, "complete") == "complete"


async def test_resolving_a_wait_takes_it_out_of_the_inbox(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Each of the three leaves for a different reason, and all three have to.

    An approval is decided, a message is delivered or withdrawn — and delivery happens
    without the operator doing anything, which is exactly why the inbox has to be derived
    from the rows rather than dismissed by hand.
    """
    job_id, gate = await gated_job(job, provider, "Rotate the backup keys")
    kept = await client.post(f"/api/jobs/{job_id}/messages", json={"content": GUIDANCE})
    withdrawn = await client.post(
        f"/api/jobs/{job_id}/messages", json={"content": "Ignore that, use MySQL."}
    )
    assert (await inbox(client))["counts"]["messages"] == 2

    cancelled = await client.delete(f"/api/jobs/{job_id}/messages/{withdrawn.json()['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert [row["id"] for row in (await inbox(client))["messages"]] == [kept.json()["id"]], (
        "a withdrawn message is still in the table and must not still be in the inbox"
    )

    await client.post(f"/api/jobs/{job_id}/approvals/{gate['id']}", json={"decision": "approved"})
    assert await wait_for_job(job_id, "complete") == "complete"

    empty = await inbox(client)
    assert empty["counts"] == {"approvals": 0, "questions": 0, "messages": 0, "blocking": 0}
    assert await db.fetch_value(
        "select consumed_at from job_messages where id=?", (kept.json()["id"],)
    ), "the kept message left the inbox by being delivered, which is the point"


async def test_a_stopped_job_leaves_nothing_behind_in_the_inbox(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """An item nobody is listening for is worse than no item.

    Answering it does nothing and from the inbox there is no way to tell. Stopping closes
    the gate and the question out itself, but a queued message is the case that cannot be
    handled that way: it was not withdrawn, so stamping it cancelled would put words in
    the operator's mouth. It is simply undeliverable now, and the inbox is derived from
    the job's status rather than from a flag on the row.
    """
    gated_id, _ = await gated_job(job, provider, "Rotate the backup keys")
    await client.post(f"/api/jobs/{gated_id}/messages", json={"content": GUIDANCE})
    asking_id, _ = await asking_job(job, provider, "Ship the backup job")
    assert (await inbox(client))["counts"]["blocking"] == 2

    for job_id in (gated_id, asking_id):
        assert (await client.post(f"/api/jobs/{job_id}/stop")).status_code == 200
        assert await wait_for_job(job_id, "stopped") == "stopped"

    waiting = await inbox(client)
    assert waiting["counts"] == {"approvals": 0, "questions": 0, "messages": 0, "blocking": 0}
    row = await db.fetch_one("select * from job_messages where job_id=?", (gated_id,))
    assert row["cancelled_at"] is None and row["consumed_at"] is None, (
        "the message is history now: neither withdrawn nor delivered"
    )


async def test_an_empty_inbox_is_zeros_rather_than_missing_keys(
    client: httpx.AsyncClient,
) -> None:
    """The badge and the three lists render before any job exists."""
    assert await inbox(client) == {
        "approvals": [],
        "questions": [],
        "messages": [],
        "counts": {"approvals": 0, "questions": 0, "messages": 0, "blocking": 0},
    }
