"""WebSocket transport for the UI.

The UI pushes rather than polls, which is what PLAN.md §2 asks for and what the v1
browser never did — it fetched once on open, so progress only appeared if you
navigated away and back.

Framing only: replay, cursor tracking and live follow all live in
``app.streams.job_event_stream``, shared with the SSE endpoint so the two
transports cannot drift apart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

from app.deps import db, events
from app.logging_setup import get_logger
from app.streams import job_event_stream

log = get_logger("agent_hub.api.stream")

router = APIRouter(tags=["stream"])

#: Application close codes (4000-4999 is the private range).
CLOSE_NOT_FOUND = 4404
CLOSE_SERVER_ERROR = 4500

KEEPALIVE_SECONDS = 20.0


def _cursor(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except ValueError:
        return 0


async def _forward(websocket: WebSocket, job_id: str, cursor: int) -> None:
    async for event in job_event_stream(events, job_id, after=cursor, keepalive=KEEPALIVE_SECONDS):
        if event is None:
            await websocket.send_json({"type": "ping"})
            continue
        await websocket.send_json({"type": "event", "event": event.to_dict()})
    await websocket.send_json({"type": "end"})


async def _watch_client(websocket: WebSocket) -> None:
    """Return as soon as the client goes away.

    Without a concurrent read the disconnect is only noticed on the next send,
    which on an idle job could be a whole keepalive interval later — long enough
    for a closed tab to keep a subscription and a database reader alive.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/ws/jobs/{job_id}")
async def job_socket(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()

    row = await db.fetch_one("select id,status from jobs where id=?", (job_id,))
    if row is None:
        await websocket.send_json({"type": "error", "detail": "job not found"})
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    cursor = _cursor(websocket.query_params.get("after"))
    await websocket.send_json(
        {"type": "hello", "job_id": job_id, "status": row["status"], "after": cursor}
    )

    forward = asyncio.create_task(_forward(websocket, job_id, cursor), name=f"ws-send:{job_id}")
    watch = asyncio.create_task(_watch_client(websocket), name=f"ws-recv:{job_id}")
    try:
        done, pending = await asyncio.wait(
            {forward, watch}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            error = task.exception()
            if error is not None and task is forward:
                log.warning(
                    "event forwarding failed", extra={"job_id": job_id, "error": str(error)}
                )
    except asyncio.CancelledError:
        forward.cancel()
        watch.cancel()
        raise
    finally:
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close()


def stream_stats() -> dict[str, Any]:
    """Subscriber counts, surfaced by /api/health."""
    return {"subscribers": events.broker.subscriber_count()}
