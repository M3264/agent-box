"""Async SQLite access.

Replaces the previous ``db()`` helper, which opened a fresh sqlite3 connection on
every call, never closed it, and ran every query synchronously on the event loop.

Design notes:
- A small pool of aiosqlite connections. Each aiosqlite connection owns a thread,
  so the pool bounds thread count while removing head-of-line blocking between
  independent requests.
- WAL is enabled once at startup (it is a persistent database property). Readers
  then never block the writer, which is what lets the event streams read while a
  job is writing.
- ``foreign_keys`` and ``busy_timeout`` are per-connection in SQLite, so they are
  applied to every pooled connection, not just the first.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import settings
from app.logging_setup import get_logger

log = get_logger("agent_hub.db")

Params = Sequence[Any] | dict[str, Any]


class Database:
    """A pool of aiosqlite connections over a single database file."""

    def __init__(
        self,
        path: Path | None = None,
        pool_size: int | None = None,
        busy_timeout_ms: int | None = None,
    ) -> None:
        self.path = path or settings.db_path
        self.pool_size = max(1, pool_size if pool_size is not None else settings.db_pool_size)
        self.busy_timeout_ms = busy_timeout_ms if busy_timeout_ms is not None else settings.db_busy_timeout_ms
        self._pool: asyncio.Queue[aiosqlite.Connection] | None = None
        self._all: list[aiosqlite.Connection] = []
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _guard(self) -> asyncio.Lock:
        """A lock bound to the running loop.

        asyncio primitives bind permanently to the first loop that awaits them, and
        this object is a module-level singleton. Creating the lock on demand — and
        rebinding it if the loop has changed — keeps one ``Database`` reusable
        across a test suite or an embedded runner instead of raising "bound to a
        different event loop" on the second connect.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        async with self._guard():
            if self._pool is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)

            # journal_mode is persistent, so set it once on a dedicated connection
            # before the pool opens. Doing it per-connection is harmless but noisy.
            bootstrap = await aiosqlite.connect(self.path)
            try:
                await bootstrap.execute("pragma journal_mode=WAL")
                await bootstrap.commit()
            finally:
                await bootstrap.close()

            pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
            for _ in range(self.pool_size):
                conn = await self._open()
                self._all.append(conn)
                pool.put_nowait(conn)
            self._pool = pool
            log.info("database ready", extra={"path": str(self.path), "pool_size": self.pool_size})

    async def _open(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"pragma busy_timeout={int(self.busy_timeout_ms)}")
        await conn.execute("pragma foreign_keys=ON")
        await conn.execute("pragma synchronous=NORMAL")
        await conn.commit()
        return conn

    async def close(self) -> None:
        async with self._guard():
            self._pool = None
            connections, self._all = self._all, []
        for conn in connections:
            try:
                await conn.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                log.warning("failed to close connection", exc_info=True)

    # ------------------------------------------------------------------- access

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._pool is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            # The pool outlives individual checkouts unless close() ran meanwhile.
            if self._pool is not None:
                self._pool.put_nowait(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a unit of work atomically, committing on success."""
        async with self.acquire() as conn:
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    # -------------------------------------------------------------- convenience

    async def fetch_all(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async with self.acquire() as conn:
            async with conn.execute(sql, params) as cursor:
                return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async with self.acquire() as conn:
            async with conn.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetch_value(self, sql: str, params: Params = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        return default if row is None else row[0]

    async def exists(self, sql: str, params: Params = ()) -> bool:
        return await self.fetch_one(sql, params) is not None

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Execute one statement, commit, and return ``rowcount``."""
        async with self.transaction() as conn:
            async with conn.execute(sql, params) as cursor:
                return cursor.rowcount

    async def insert(self, sql: str, params: Params = ()) -> int:
        """Execute an INSERT, commit, and return the new rowid."""
        async with self.transaction() as conn:
            async with conn.execute(sql, params) as cursor:
                return int(cursor.lastrowid or 0)

    async def execute_many(self, sql: str, rows: Iterable[Params]) -> None:
        async with self.transaction() as conn:
            await conn.executemany(sql, list(rows))


db = Database()
