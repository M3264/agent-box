"""Web Push: the opt-in surface, the fan-out, and the two gate hooks that trigger it.

Nothing here touches the network. The blocking sender (`push._send_one`) is
monkeypatched, so `pywebpush`/`py_vapid` never load during the run — which is also what
keeps their DeprecationWarnings clear of pytest's ``error`` filter. The VAPID keypair,
when a test needs one, is generated into the per-test ``tmp_path`` rather than the real
``data/`` directory, because ``settings.vapid_file`` hangs off the temp DB path.

The suite pins ``AGENT_HUB_PUSH=0`` (see conftest), so every test starts with push
unconfigured — the real lifespan resets it on each startup. A test that wants it on
opts in explicitly with ``push.setup(vapid_file=..., enabled=True)``.
"""

from __future__ import annotations

import pytest

from app import push
from app.db import db
from app.events import events
from app.orchestrator import approvals, questions


# ------------------------------------------------------------------- pure payload


def test_compose_names_the_event_and_task() -> None:
    """The body a device shows must say what is needed and on which job.

    This is the whole point of the feature over a bare "a job needs you": the lock
    screen carries enough to decide whether to get up and look.
    """
    approval = push._compose(
        "j1", kind="approval", agent="tester", summary="rm -rf build", task="Ship the page"
    )
    assert approval["title"] == "Approval needed"
    assert "tester" in approval["body"]
    assert "rm -rf build" in approval["body"]
    assert "Ship the page" in approval["body"]
    assert approval["hash"] == "#/jobs/j1"
    assert approval["tag"] == push.TAG

    question = push._compose("j2", kind="question", agent="coder", summary="Which DB?", task="")
    assert question["title"] == "coder is asking"
    # No task on this one, so the body is the question alone — no dangling separator.
    assert question["body"] == "Which DB?"
    assert question["hash"] == "#/jobs/j2"


def test_compose_clips_a_runaway_summary() -> None:
    """A model can write a paragraph where a sentence was asked for; the pop-up can't."""
    payload = push._compose(
        "j3", kind="question", agent="coder", summary="word " * 200, task="t " * 200
    )
    assert len(payload["body"]) < 200


# --------------------------------------------------------------- subscribe surface


async def test_subscribe_upserts_on_endpoint(client) -> None:
    """A browser rotates its subscription and re-posts; that must be one row, not two.

    Two rows for one browser would fire the same notification twice, which is exactly
    the "aggressive ads" behaviour this feature was told not to be.
    """
    endpoint = "https://push.example.test/aaa"
    first = await client.post(
        "/api/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "pub", "auth": "sec"}},
    )
    assert first.status_code == 201, first.text
    assert first.json()["subscribers"] == 1

    # Same endpoint, fresh keys → still one row, keys replaced.
    again = await client.post(
        "/api/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": "pub2", "auth": "sec2"}},
    )
    assert again.status_code == 201
    assert again.json()["subscribers"] == 1
    row = await db.fetch_one(
        "select p256dh, auth from push_subscriptions where endpoint=?", (endpoint,)
    )
    assert row["p256dh"] == "pub2"
    assert row["auth"] == "sec2"

    removed = await client.post("/api/push/unsubscribe", json={"endpoint": endpoint})
    assert removed.status_code == 200
    assert removed.json()["subscribers"] == 0
    # Unticking a browser that is already gone is not an error.
    again_removed = await client.post("/api/push/unsubscribe", json={"endpoint": endpoint})
    assert again_removed.status_code == 200


async def test_push_info_reports_key_only_when_configured(client, tmp_path) -> None:
    """`GET /api/push` hands the browser the public key, and never the private one."""
    off = (await client.get("/api/push")).json()
    assert off["configured"] is False
    assert off["key"] is None

    await push.setup(vapid_file=tmp_path / "vapid.json", enabled=True)
    on = (await client.get("/api/push")).json()
    assert on["configured"] is True
    assert isinstance(on["key"], str) and on["key"]
    # The served key is the public applicationServerKey — the PEM stays server-side.
    assert "PRIVATE KEY" not in on["key"]
    assert on["key"] == push.public_key()
    push.reset()


# ------------------------------------------------------------------------ fan-out


async def test_fanout_sends_once_each_and_reconciles_rows(monkeypatch, tmp_path) -> None:
    """One send per subscription, and the row reflects what the push service said.

    A dead endpoint (410) is dropped so it is never tried again; a transient failure
    keeps the row but bumps ``failures`` — one flaky send is not grounds to forget a
    browser. A clean send stamps ``last_ok`` and zeroes the counter.
    """
    await push.setup(vapid_file=tmp_path / "vapid.json", enabled=True)

    async def add(sub_id: str, endpoint: str) -> None:
        await db.execute(
            "insert into push_subscriptions(id,endpoint,p256dh,auth,failures,created_at)"
            " values(?,?,?,?,?,unixepoch('subsec'))",
            (sub_id, endpoint, "pub", "sec", 3),
        )

    await add("ok", "https://push.example.test/ok")
    await add("gone", "https://push.example.test/gone")
    await add("flaky", "https://push.example.test/flaky")

    sent: list[tuple[str, dict]] = []

    def fake_send_one(sub, payload) -> None:
        sent.append((sub["endpoint"], payload))
        if sub["id"] == "gone":
            raise push._Gone()
        if sub["id"] == "flaky":
            raise RuntimeError("connection reset")

    monkeypatch.setattr(push, "_send_one", fake_send_one)

    await push._fanout("job-xyz", kind="question", agent="coder", summary="Which database?")

    # Every subscription was attempted exactly once.
    assert len(sent) == 3
    _, payload = sent[0]
    assert payload["hash"] == "#/jobs/job-xyz"
    assert payload["title"] == "coder is asking"
    assert "Which database?" in payload["body"]

    # 410 → pruned.
    assert (
        await db.fetch_value(
            "select count(*) from push_subscriptions where id='gone'", default=0
        )
        == 0
    )
    # Clean send → failures reset to 0 and last_ok stamped.
    ok = await db.fetch_one("select failures, last_ok from push_subscriptions where id='ok'")
    assert ok["failures"] == 0
    assert ok["last_ok"] is not None
    # Transient → kept, and the failure counter climbed off its starting 3.
    flaky = await db.fetch_value(
        "select failures from push_subscriptions where id='flaky'", default=None
    )
    assert flaky == 4

    push.reset()


async def test_send_test_returns_count_and_is_awaited(monkeypatch, tmp_path) -> None:
    """The test button must report reaching the wire, so it is awaited, not scheduled."""
    await push.setup(vapid_file=tmp_path / "vapid.json", enabled=True)
    await db.execute(
        "insert into push_subscriptions(id,endpoint,p256dh,auth,created_at)"
        " values('t','https://push.example.test/t','pub','sec',unixepoch('subsec'))"
    )
    seen: list[dict] = []
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: seen.append(payload))

    sent = await push.send_test()
    assert sent == 1
    assert seen and seen[0]["tag"] == push.TAG
    push.reset()


# --------------------------------------------------------------- the two gate hooks


async def test_pending_approval_notifies_but_auto_does_not(monkeypatch, job) -> None:
    """A gate the operator must decide is a rise in blocking; an auto-approval is not.

    The split is exactly the resulting status: ``pending`` fires the push, ``approved``
    (yolo mode) blocks nobody and must stay silent. Assertions filter on the action
    text so a plan-level gate the engine might raise on its own cannot skew the count.
    """
    calls: list[dict] = []
    monkeypatch.setattr(push, "notify", lambda job_id, **kw: calls.append({"job_id": job_id, **kw}))

    job_id = await job()

    await approvals.request(
        db, events, job_id=job_id, action="rm -rf /srv", agent="tester", auto_approve=False
    )
    pending = [c for c in calls if c["summary"] == "rm -rf /srv"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "approval"
    assert pending[0]["job_id"] == job_id

    await approvals.request(
        db, events, job_id=job_id, action="ls -la", agent="tester", auto_approve=True
    )
    assert not [c for c in calls if c["summary"] == "ls -la"]  # the auto-approval was silent


async def test_question_notifies(monkeypatch, job) -> None:
    """A new question is the other rise in blocking, and pushes the same way a gate does."""
    calls: list[dict] = []
    monkeypatch.setattr(push, "notify", lambda job_id, **kw: calls.append({"job_id": job_id, **kw}))

    job_id = await job()
    await questions.request(
        db, events, job_id=job_id, agent="coder", question="Postgres or SQLite?"
    )

    mine = [c for c in calls if c["summary"] == "Postgres or SQLite?"]
    assert len(mine) == 1
    assert mine[0]["kind"] == "question"
    assert mine[0]["job_id"] == job_id


async def test_notify_is_a_noop_when_unconfigured() -> None:
    """Unconfigured, `notify` must schedule nothing — so the whole suite can ignore it.

    This is why the 60-odd unrelated tests, which never configure push, are unaffected
    by the hooks now living in the approval and question paths.
    """
    assert not push.configured()
    before = len(push._tasks)
    push.notify("whatever", kind="question", agent="a", summary="s")
    assert len(push._tasks) == before
