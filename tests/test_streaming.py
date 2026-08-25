"""Cursor replay over both transports.

PLAN.md §8 wants a closed tab to reopen with no gap and no duplicates. v1 shipped
both a WebSocket and an SSE endpoint and the browser used neither, so replay was
never exercised. The WS handler also read history *before* registering its queue,
losing anything recorded in between — ``job_event_stream`` subscribes first, and
these tests pin that ordering down by streaming a job that is still running.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.main import app
from app.streams import TERMINAL_EVENT_GRACE, TERMINAL_STATUSES
from tests.conftest import FakeProvider, wait_for_job
from tests.wsclient import ASGIWebSocket


async def sse_ids(client: httpx.AsyncClient, url: str, **kwargs: Any) -> list[int]:
    """Read an SSE response to completion, returning the event ids it carried."""
    ids: list[int] = []
    async with client.stream("GET", url, timeout=10.0, **kwargs) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no", "nginx would buffer the stream"
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            elif line.startswith("data: "):
                # Every data frame must be parseable on its own.
                assert json.loads(line[6:])["id"] == ids[-1]
    return ids


def ws_event_ids(frames: list[dict[str, Any]]) -> list[int]:
    return [frame["event"]["id"] for frame in frames if frame["type"] == "event"]


async def test_sse_replays_from_a_cursor(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    assert len(history) > 5
    cursor = history[3]["id"]

    ids = await sse_ids(client, f"/api/jobs/{job_id}/events?after={cursor}")
    assert ids == [event["id"] for event in history if event["id"] > cursor]
    assert ids, "the stream must not close before replaying"


async def test_sse_honours_last_event_id(client: httpx.AsyncClient, job) -> None:
    """The CLI's reconnect loop sends the header, not the query parameter."""
    job_id = await job()
    await wait_for_job(job_id, "complete")

    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    cursor = history[-3]["id"]

    ids = await sse_ids(
        client, f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(cursor)}
    )
    assert ids == [event["id"] for event in history[-2:]]


async def test_sse_ignores_a_junk_cursor(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    ids = await sse_ids(client, f"/api/jobs/{job_id}/events?after=not-a-number")
    assert ids == [event["id"] for event in history], "a bad cursor replays from the start"


async def test_ws_replays_from_a_cursor_and_ends(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")
    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    cursor = history[2]["id"]

    async with ASGIWebSocket(app, f"/ws/jobs/{job_id}?after={cursor}") as socket:
        hello = await socket.receive_json()
        assert hello == {
            "type": "hello",
            "job_id": job_id,
            "status": "complete",
            "after": cursor,
        }
        frames = await socket.collect_until(lambda frame: frame["type"] == "end")

    assert ws_event_ids(frames) == [event["id"] for event in history if event["id"] > cursor]


async def test_ws_follows_a_live_job_without_gaps_or_duplicates(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """Connect mid-job — the case the v1 subscribe-after-history race broke.

    The socket opens while planning is parked, so events are still being recorded
    during replay. Every id must arrive exactly once, in order, up to the terminal
    status event.
    """
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    async with ASGIWebSocket(app, f"/ws/jobs/{job_id}") as socket:
        assert (await socket.receive_json())["type"] == "hello"
        provider.open_gate()
        frames = await socket.collect_until(
            lambda frame: frame["type"] == "end", timeout=10.0
        )

    ids = ws_event_ids(frames)
    assert ids == sorted(set(ids)), f"ids must be unique and increasing: {ids}"

    history = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    assert ids == [event["id"] for event in history], "the live stream missed events"

    last = next(frame for frame in reversed(frames) if frame["type"] == "event")["event"]
    assert last["kind"] == "status"
    assert last["payload"]["status"] in TERMINAL_STATUSES


async def test_ws_stream_closes_when_a_job_is_stopped(
    client: httpx.AsyncClient, job, provider: FakeProvider
) -> None:
    """A stop must close live streams, not leave browsers hanging on a dead job."""
    provider.gate = asyncio.Event()
    provider.gate_after = 0
    job_id = await job()
    await provider.wait_until_blocked()

    async with ASGIWebSocket(app, f"/ws/jobs/{job_id}") as socket:
        assert (await socket.receive_json())["type"] == "hello"
        await client.post(f"/api/jobs/{job_id}/stop")
        frames = await socket.collect_until(lambda frame: frame["type"] == "end", timeout=10.0)

    statuses = [
        frame["event"]["payload"].get("status")
        for frame in frames
        if frame["type"] == "event" and frame["event"]["kind"] == "status"
    ]
    assert statuses[-1] == "stopped"
    provider.open_gate()


async def test_ws_rejects_an_unknown_job() -> None:
    async with ASGIWebSocket(app, "/ws/jobs/doesnotexist") as socket:
        assert await socket.receive_json() == {"type": "error", "detail": "job not found"}
        with pytest.raises(ConnectionError):
            await socket.receive_json()
        assert socket.closed_code == 4404


async def test_ws_closes_at_once_when_reopened_past_a_finished_job(
    client: httpx.AsyncClient, job
) -> None:
    """Reconnecting with the cursor a client held at the end must not hang.

    Nothing is left to replay, so the terminal event cannot be *delivered* again —
    but it is durable behind the cursor, which is proof that no further event can
    arrive. Waiting out ``TERMINAL_EVENT_GRACE`` here left the UI showing a live
    indicator on a finished job for 15 seconds after reopening it.
    """
    job_id = await job()
    await wait_for_job(job_id, "complete")
    snapshot = (await client.get(f"/api/jobs/{job_id}")).json()

    loop = asyncio.get_running_loop()
    started = loop.time()
    async with ASGIWebSocket(app, f"/ws/jobs/{job_id}?after={snapshot['cursor']}") as socket:
        assert (await socket.receive_json())["type"] == "hello"
        frames = await socket.collect_until(lambda frame: frame["type"] == "end")
    elapsed = loop.time() - started

    assert ws_event_ids(frames) == [], "there was nothing left to replay"
    assert elapsed < TERMINAL_EVENT_GRACE / 3, (
        f"closing took {elapsed:.1f}s; it should not wait out the grace window"
    )


async def test_history_paginates_by_cursor(client: httpx.AsyncClient, job) -> None:
    job_id = await job()
    await wait_for_job(job_id, "complete")

    everything = (await client.get(f"/api/jobs/{job_id}/events/history")).json()
    assert len(everything) > 4

    pages: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = (
            await client.get(f"/api/jobs/{job_id}/events/history?after={cursor}&limit=3")
        ).json()
        if not page:
            break
        pages.extend(page)
        cursor = page[-1]["id"]

    assert [event["id"] for event in pages] == [event["id"] for event in everything]
    assert (await client.get(f"/api/jobs/{job_id}/events/history?after=0&limit=0")).status_code == 422


async def test_streams_leave_no_subscribers_behind(client: httpx.AsyncClient, job) -> None:
    """The v1 cleanup block could pop a job's whole subscriber set on any error."""
    from app.events import broker

    job_id = await job()
    await wait_for_job(job_id, "complete")

    async with ASGIWebSocket(app, f"/ws/jobs/{job_id}") as socket:
        await socket.receive_json()
        await socket.collect_until(lambda frame: frame["type"] == "end")
    await sse_ids(client, f"/api/jobs/{job_id}/events")

    assert broker.subscriber_count() == 0
