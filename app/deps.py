"""Shared singletons and route dependencies.

Kept in one place so routers never import each other and tests can point the
database at a temporary file by setting ``AGENT_HUB_DB`` before import.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.db import db
from app.events import broker, events
from app.orchestrator.engine import JobEngine

engine = JobEngine(db, events)

__all__ = ["broker", "db", "engine", "events", "require_job"]


async def require_job(job_id: str) -> dict[str, Any]:
    """Fetch a job or raise 404.

    Validating up front matters for the streaming endpoints in particular: v1's
    SSE handler would happily hold a connection open forever for a typo'd id.
    """
    row = await db.fetch_one("select * from jobs where id=?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return dict(row)
