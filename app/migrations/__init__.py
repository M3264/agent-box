"""Versioned schema migrations.

Replaces the previous try/except ALTER TABLE block, which could not tell an
already-applied change from a genuine error and left no record of schema state.

Migrations are ``NNN_name.sql`` files in this package, applied in filename order
inside a transaction, with the applied version recorded in ``schema_version``.
Re-running is a no-op, which is what the idempotency test asserts.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.db import Database
from app.logging_setup import get_logger

log = get_logger("agent_hub.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent
_FILENAME = re.compile(r"^(\d+)_.+\.sql$")


def discover() -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs sorted by version."""
    found: dict[int, Path] = {}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _FILENAME.match(path.name)
        if not match:
            log.warning("ignoring unversioned migration file", extra={"file": path.name})
            continue
        version = int(match.group(1))
        if version in found:
            raise RuntimeError(f"duplicate migration version {version}: {found[version].name} and {path.name}")
        found[version] = path
    return sorted(found.items())


async def applied_versions(database: Database) -> set[int]:
    async with database.acquire() as conn:
        await conn.execute(
            "create table if not exists schema_version("
            "  version integer primary key,"
            "  name text not null,"
            "  applied_at real not null"
            ")"
        )
        await conn.commit()
        async with conn.execute("select version from schema_version") as cursor:
            return {int(row[0]) for row in await cursor.fetchall()}


async def migrate(database: Database) -> list[int]:
    """Apply every pending migration. Returns the versions applied this call."""
    done = await applied_versions(database)
    newly_applied: list[int] = []

    for version, path in discover():
        if version in done:
            continue
        sql = path.read_text()
        async with database.acquire() as conn:
            try:
                # executescript() commits any open transaction first, so the
                # version row is written in a follow-up statement and committed
                # together with it.
                await conn.executescript(sql)
                await conn.execute(
                    "insert into schema_version(version,name,applied_at) values(?,?,unixepoch('subsec'))",
                    (version, path.name),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                log.error("migration failed", extra={"version": version, "file": path.name})
                raise
        newly_applied.append(version)
        log.info("migration applied", extra={"version": version, "file": path.name})

    if not newly_applied:
        log.info("schema up to date", extra={"version": max(done) if done else 0})
    return newly_applied
