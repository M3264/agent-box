"""A minimal in-process WebSocket client.

Starlette's ``TestClient`` runs the app in its own event loop and thread, which
would rebind the module-level singletons the app shares with the rest of the
suite. Speaking ASGI directly keeps the socket on the same loop as everything
else, at the cost of about thirty lines.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit


class ASGIWebSocket:
    """Drive a WebSocket endpoint over the raw ASGI interface."""

    def __init__(self, app: Any, url: str) -> None:
        parts = urlsplit(url)
        self.app = app
        self.path = parts.path
        self.query = parts.query.encode()
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.closed_code: int | None = None

    async def __aenter__(self) -> ASGIWebSocket:
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query,
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(
            self.app(scope, self._inbound.get, self._outbound.put), name="ws-app"
        )
        await self._inbound.put({"type": "websocket.connect"})
        first = await self.receive_raw()
        assert first["type"] == "websocket.accept", first
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def receive_raw(self, timeout: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._outbound.get(), timeout)

    async def receive_json(self, timeout: float = 5.0) -> dict[str, Any]:
        """Next text frame as JSON, or raise if the server closed instead."""
        message = await self.receive_raw(timeout)
        if message["type"] == "websocket.close":
            self.closed_code = message.get("code", 1000)
            raise ConnectionError(f"server closed the socket: {self.closed_code}")
        return json.loads(message["text"])

    async def collect_until(
        self, predicate, *, timeout: float = 5.0, skip_pings: bool = True
    ) -> list[dict[str, Any]]:
        """Read frames until ``predicate`` matches one, returning everything seen."""
        seen: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            frame = await self.receive_json(timeout=max(0.05, deadline - loop.time()))
            if skip_pings and frame.get("type") == "ping":
                continue
            seen.append(frame)
            if predicate(frame):
                return seen
        raise AssertionError(f"predicate never matched; saw {seen!r}")

    async def close(self) -> None:
        await self._inbound.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
