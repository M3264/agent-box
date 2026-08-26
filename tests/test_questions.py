"""Agents asking the operator, and the answers that unblock them.

The gap this closes: an agent that did not know something only the operator knew had
exactly one move, which was to guess and mention the guess in its output. So these
tests are less about the table than about the loop around it — the job goes
``blocked``, the phase stays ``active``, and the answer arrives in the *next* prompt
as words the model can act on.

Four resolutions exist and all four have to be survivable, because none of them is a
programming error: answered, answered-with-a-note-as-well, timed out, and closed
without an answer (a stop, or a restart). Only the last two are interesting, and both
are asserted here to end with a job that finished rather than one that hung.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.db import db
from app.deps import engine
from app.orchestrator.questions import MAX_LABEL, MAX_OPTIONS, normalise_options
from tests.conftest import (
    ONE_PHASE,
    FakeProvider,
    event_kinds,
    phase_rows,
    says,
    tool,
    tune,
    wait_for_job,
    wait_for_question,
    wait_until,
)

QUESTION = "Should the status page write to Postgres or SQLite?"


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(plan=ONE_PHASE)


async def question_rows(job_id: str) -> list[dict]:
    rows = await db.fetch_all(
        "select * from questions where job_id=? order by created_at, rowid", (job_id,)
    )
    return [dict(row) for row in rows]


async def tool_row(job_id: str) -> dict:
    rows = await db.fetch_all("select * from tool_calls where job_id=? order by rowid", (job_id,))
    assert len(rows) == 1, f"expected one tool call, got {[dict(r)['tool'] for r in rows]}"
    return dict(rows[0])


async def statuses(job_id: str) -> list[str]:
    return [payload["status"] for payload in await event_kinds(job_id, "status")]


def work_phase(rows: list[dict]) -> dict:
    """The single work phase. Seq 1, because seq 0 is planning and the last is synthesis."""
    return next(row for row in rows if row["seq"] == 1)


# ------------------------------------------------------------------- the happy path


async def test_a_question_blocks_the_job_and_its_answer_reaches_the_next_turn(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    provider.tool_script = [
        tool(
            "ask_operator",
            question=QUESTION,
            detail="Both work for the acceptance criteria, so it is your call.",
            options=["Postgres", "SQLite"],
        ),
        says("Using Postgres, as you asked."),
    ]

    job_id = await job("Ship a status page")
    row = await wait_for_question(job_id)

    assert row["agent"] == "coder"
    assert row["question"] == QUESTION
    assert "your call" in row["detail"]
    # Values are derived from the labels here, not sent by the model, so "which option
    # did they pick" survives an agent that rephrases its own buttons next turn.
    assert json.loads(row["options"]) == [
        {"value": "postgres", "label": "Postgres", "detail": ""},
        {"value": "sqlite", "label": "SQLite", "detail": ""},
    ]

    # A question mirrors the command gate exactly: the *job* is blocked, and the phase
    # is still the active one, because it is still the phase that will finish the work.
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "blocked"
    assert work_phase(await phase_rows(job_id))["status"] == "active"

    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()
    assert [entry["question"] for entry in snapshot["questions"]] == [QUESTION]
    assert snapshot["questions"][0]["options"][0]["value"] == "postgres"
    assert snapshot["questions"][0]["allow_free_text"] is True
    listed = next(
        entry for entry in (await client.get("/api/jobs")).json() if entry["id"] == job_id
    )
    assert listed["pending_questions"] == 1, "the job list must show a job that is waiting"

    answered = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={"chosen": "postgres"}
    )
    assert answered.status_code == 200, answered.text
    # The label, not the slug: an answer is read by a model, and "postgres" is a value
    # the operator never saw.
    assert answered.json()["answer"] == "Postgres"
    assert answered.json()["chosen"] == "postgres"

    assert await wait_for_job(job_id, "complete") == "complete"

    handed_back = provider.prompts_containing("The operator answered")
    assert handed_back, f"the answer never reached the model: {provider.prompts}"
    assert "Postgres" in handed_back[0]
    assert "do not ask again" in handed_back[0], "an answered question must not be re-asked"

    call = await tool_row(job_id)
    assert (call["tool"], call["status"]) == ("ask_operator", "ok")
    assert call["stdout"] == "Postgres"
    assert call["turn"] == 1 and call["agent"] == "coder"
    assert call["finished_at"] >= call["started_at"]

    # `questions.request` and `questions.answer` already narrate this into the stream
    # with the options and the answer attached; a `$ Should the status page…` command
    # event would render the same moment a second time, worse.
    assert await event_kinds(job_id, "tool_call") == []
    assert [payload["status"] for payload in await event_kinds(job_id, "question")] == [
        "pending",
        "answered",
    ]

    announced = await statuses(job_id)
    assert "blocked" in announced, f"nothing told an open stream the job was waiting: {announced}"
    assert announced[announced.index("blocked") + 1] == "running", (
        "an answered question must put the job back to work, not leave it reading blocked"
    )
    assert announced[-1] == "complete"


async def test_an_option_and_a_note_are_both_handed_back(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """"The second one, but only for staging" is a real answer and loses nothing here."""
    provider.tool_script = [
        tool(
            "ask_operator",
            question=QUESTION,
            options=[
                {"label": "Postgres", "detail": "Needs a container."},
                {"label": "SQLite", "detail": "Zero setup."},
            ],
        ),
        says("SQLite for staging, then."),
    ]

    job_id = await job()
    row = await wait_for_question(job_id)
    assert json.loads(row["options"])[1]["detail"] == "Zero setup."

    unknown = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={"chosen": "mysql"}
    )
    assert unknown.status_code == 409, "an option the agent never offered is not an answer"
    assert (
        await client.post(f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={})
    ).status_code == 422, "an empty answer is a client bug, not an answer"

    note = "SQLite, but only for the staging bucket."
    accepted = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer",
        json={"chosen": "sqlite", "text": note},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["answer"] == note and accepted.json()["chosen"] == "sqlite"

    assert await wait_for_job(job_id, "complete") == "complete"

    handed_back = provider.prompts_containing("The operator chose")
    assert handed_back, provider.prompts
    assert 'chose "SQLite"' in handed_back[0], "the model reasons about its own label"
    assert note in handed_back[0], "the free text must not be dropped in favour of the label"

    again = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={"text": "changed my mind"}
    )
    assert again.status_code == 409, "an answered question is closed; send a message instead"


async def test_a_question_with_nothing_in_it_is_an_error_the_agent_can_recover_from(
    job, provider: FakeProvider
) -> None:
    """A malformed call must not park a job on a question nobody can see."""
    provider.tool_script = [
        tool("ask_operator", question="   ", options=["a", "b"]),
        says("Asked badly, recovered, carried on."),
    ]

    job_id = await job()
    assert await wait_for_job(job_id, "complete") == "complete"

    assert await question_rows(job_id) == []
    call = await tool_row(job_id)
    assert (call["tool"], call["status"]) == ("ask_operator", "error")
    assert provider.prompts_containing("No question was supplied"), provider.prompts
    assert "blocked" not in await statuses(job_id)


# ------------------------------------------------------------------ the other three


async def test_an_unanswered_question_times_out_and_says_so_in_the_prompt(
    monkeypatch: pytest.MonkeyPatch, job, provider: FakeProvider
) -> None:
    """A job that hangs for a day because nobody was watching is the worse failure.

    The timeout is a real resolution rather than an error: the phase carries on, and
    what the model is told is that it should proceed on an assumption *and state which
    one*, so the guess is visible in the output instead of buried in it.
    """
    tune(monkeypatch, question_timeout=1)
    provider.tool_script = [
        tool("ask_operator", question=QUESTION),
        says("Nobody answered, so I assumed Postgres and said so."),
    ]

    job_id = await job()
    await wait_for_question(job_id)
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await question_rows(job_id)
    assert [row["status"] for row in rows] == ["timeout"]
    assert rows[0]["answered_at"], "a timed-out question must be closed out, not left open"
    assert (await tool_row(job_id))["status"] == "timeout"

    warned = provider.prompts_containing("did not answer in time")
    assert warned, provider.prompts
    assert "state clearly in your output which assumption" in warned[0]
    assert [payload["status"] for payload in await event_kinds(job_id, "question")] == [
        "pending",
        "timeout",
    ]


async def test_stopping_a_job_closes_the_question_it_was_waiting_on(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Otherwise the inbox keeps a question no agent is listening for any more.

    Also the case where the block/restore pair can lose to a stop: the wait writes
    'blocked' on the way in and puts the previous status back on the way out, and that
    restore runs *during* cancellation — after `stop` has already written 'stopped'.
    """
    provider.tool_script = [tool("ask_operator", question=QUESTION, options=["Yes", "No"])]

    job_id = await job()
    row = await wait_for_question(job_id)

    stopped = await client.post(f"/api/jobs/{job_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert await wait_for_job(job_id, "stopped") == "stopped"
    await wait_until(lambda: not engine.is_running(job_id))
    assert await db.fetch_value("select status from jobs where id=?", (job_id,)) == "stopped"
    assert (await statuses(job_id))[-1] == "stopped", "a stopped job announced it came back"

    closed = (await question_rows(job_id))[0]
    assert closed["status"] == "cancelled"
    assert "stopped" in closed["answer"]

    late = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={"text": "too late"}
    )
    assert late.status_code == 409, "an answer to a stopped job's question goes nowhere"


async def test_a_question_open_at_a_restart_is_cancelled_and_the_phase_re_runs(
    job, provider: FakeProvider
) -> None:
    """The one place a question is less durable than the row recording it.

    The answer would be delivered into a tool-loop conversation that died with the
    process, so there is nothing left to unblock. Cancelling and re-running the phase is
    honest; an answered question nobody consumed would not be.
    """
    provider.tool_script = [tool("ask_operator", question=QUESTION, options=["Yes", "No"])]

    job_id = await job()
    await wait_for_question(job_id)

    await engine.shutdown(grace=2)
    assert not engine.is_running(job_id)

    provider.tool_script = [says("Asked again was not needed; I checked the repo instead.")]
    assert job_id in await engine.recover()
    assert await wait_for_job(job_id, "complete") == "complete"

    rows = await question_rows(job_id)
    assert [row["status"] for row in rows] == ["cancelled"], "a question survived its own loop"
    assert "restarted" in rows[0]["answer"]
    assert (await tool_row(job_id))["status"] == "interrupted"

    phase = work_phase(await phase_rows(job_id))
    assert phase["attempts"] == 2 and phase["status"] == "complete"

    warned = provider.prompts_containing("previous attempt at this phase was interrupted")
    assert warned, "the re-run was not told what was in flight"
    assert "ask again if you still need it" in warned[0], (
        "a question is not a command; the notice must not imply the workspace changed"
    )
    assert "workspace may already reflect it" not in warned[0]


# ------------------------------------------------------------------------- options


def test_options_are_accepted_in_every_shape_a_model_might_send() -> None:
    """Three spellings of the same intent, none of them worth arguing with.

    A schema error here costs a turn and teaches the model half a lesson; accepting all
    three costs one function.
    """
    assert normalise_options(["Rewrite it", "Patch it"]) == [
        {"value": "rewrite-it", "label": "Rewrite it", "detail": ""},
        {"value": "patch-it", "label": "Patch it", "detail": ""},
    ]
    assert normalise_options([{"label": "Rewrite it", "description": "Slower."}]) == [
        {"value": "rewrite-it", "label": "Rewrite it", "detail": "Slower."}
    ]
    assert normalise_options({"Rewrite it": "Slower."}) == [
        {"value": "rewrite-it", "label": "Rewrite it", "detail": "Slower."}
    ]
    assert normalise_options(None) == [] and normalise_options("Rewrite it") == []


def test_option_values_are_unique_and_always_usable() -> None:
    """`chosen` is matched against these, so a duplicate or empty value loses an answer."""
    assert [entry["value"] for entry in normalise_options(["Do it", "Do it", "Do it"])] == [
        "do-it",
        "do-it-2",
        "do-it-3",
    ]
    # A label that slugifies to nothing still needs a value, or the option is unpickable.
    assert [entry["value"] for entry in normalise_options(["再実行", "🚀"])] == [
        "option-1",
        "option-2",
    ]
    assert normalise_options([" ", ""]) == [], "a blank label is not an option"

    capped = normalise_options([f"Option {index}" for index in range(MAX_OPTIONS + 5)])
    assert len(capped) == MAX_OPTIONS, "past this it is asking the operator to do its job"
    assert len(normalise_options(["x" * (MAX_LABEL + 50)])[0]["label"]) == MAX_LABEL


async def test_an_agent_that_offers_no_options_always_gets_free_text(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Otherwise a model can ask a question that cannot be answered at all."""
    provider.tool_script = [
        tool("ask_operator", question=QUESTION, allow_free_text=False),
        says("Answered in prose."),
    ]

    job_id = await job()
    row = await wait_for_question(job_id)
    assert row["allow_free_text"] == 1, "an open question with no options must accept text"

    answered = await client.post(
        f"/api/jobs/{job_id}/questions/{row['id']}/answer", json={"text": "Postgres, please."}
    )
    assert answered.status_code == 200, answered.text
    assert await wait_for_job(job_id, "complete") == "complete"
    assert provider.prompts_containing("Postgres, please."), provider.prompts
