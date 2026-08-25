"""FastAPI application: wiring, lifespan, health, and static hosting.

The lifespan is the part worth reading. v1 had none of it: it opened a connection
per query, patched the schema with try/except ALTER TABLE, relaunched interrupted
jobs from phase one, and had no shutdown path at all — `systemctl stop` waited on
background tasks that could never finish and escalated to SIGKILL after 90s.

Startup order here is load-bearing:

1. logging, so migration and recovery output is structured
2. directories and the connection pool
3. migrations, before anything reads a table
4. provider bootstrap, so a fresh database is usable without manual setup
5. recovery *last*, because resumed jobs immediately query the schema they need

Shutdown is the reverse: cancel in-flight jobs within a grace period well under
systemd's stop timeout, park them as resumable, then close the pool.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.api import approvals as approvals_api
from app.api import config as config_api
from app.api import jobs as jobs_api
from app.api import stream as stream_api
from app.config import settings
from app.db import db
from app.logging_setup import configure, get_logger
from app.migrations import applied_versions, migrate
from app.models import Health
from app.orchestrator.providers import bootstrap_profiles
from app.deps import engine

log = get_logger("agent_hub.main")

VERSION = "2.0.0"

#: Paths that must never fall through to the SPA — a typo under these should be a
#: 404, not an HTML page that a fetch() then fails to parse as JSON.
API_PREFIXES = ("api/", "ws/")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure()
    log.info("starting agent hub", extra={"version": VERSION, "db": str(settings.db_path)})

    settings.ensure_dirs()
    await db.connect()
    applied = await migrate(db)
    if applied:
        log.info("applied migrations", extra={"versions": applied})
    await bootstrap_profiles(db)

    resumed = await engine.recover()
    log.info("startup complete", extra={"resumed_jobs": len(resumed)})
    try:
        yield
    finally:
        log.info("shutting down", extra={"active_jobs": engine.active_count})
        await engine.shutdown(settings.shutdown_grace)
        await db.close()
        log.info("shutdown complete")


app = FastAPI(
    title="Agent Hub",
    version=VERSION,
    description="Persistent multi-agent control plane.",
    lifespan=lifespan,
)

app.include_router(jobs_api.router)
app.include_router(approvals_api.router)
app.include_router(config_api.router)
app.include_router(stream_api.router)


@app.get("/api/health", response_model=Health, tags=["meta"])
async def health() -> Health:
    """Liveness plus enough state to tell a healthy process from a wedged one."""
    detail: dict[str, object] = {
        "subscribers": stream_api.stream_stats()["subscribers"],
        "db_path": str(settings.db_path),
    }
    try:
        versions = await applied_versions(db)
        schema_version = max(versions) if versions else 0
        detail["jobs"] = int(await db.fetch_value("select count(*) from jobs", default=0))
        detail["pending_approvals"] = int(
            await db.fetch_value(
                "select count(*) from approvals where status='pending'", default=0
            )
        )
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - health must answer, not raise
        log.error("health check failed", exc_info=True)
        schema_version = 0
        status = "degraded"
        detail["error"] = str(exc)

    return Health(
        status=status,
        version=VERSION,
        schema_version=schema_version,
        active_jobs=engine.active_count,
        detail=detail,
    )


# ------------------------------------------------------------------ static hosting


def _index() -> Response:
    """Serve the built SPA, or a readable message when it has not been built yet."""
    index = settings.static_dir / "index.html"
    if index.exists():
        # The shell must not be cached: it names hashed asset files that change on
        # every build.
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        status_code=503,
        content={
            "detail": "frontend is not built",
            "hint": "run `npm --prefix web install && npm --prefix web run build`",
        },
    )


@app.get("/", include_in_schema=False)
async def index() -> Response:
    return _index()


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(request: Request, full_path: str) -> Response:
    """Serve built assets, and the SPA shell for client-side routes.

    Registered last so it never shadows the API. Assets are matched explicitly
    rather than by mounting a StaticFiles app, because nginx exposes this service
    at both ``/`` and ``/hub/`` (stripping the prefix), so the same request path can
    arrive under either mount and must resolve identically.
    """
    if full_path.startswith(API_PREFIXES):
        return JSONResponse(status_code=404, content={"detail": "not found"})

    candidate = (settings.static_dir / full_path).resolve()
    static_root = settings.static_dir.resolve()
    # Containment check: full_path is attacker-controlled, and `..` segments would
    # otherwise read outside the static directory.
    if candidate.is_file() and candidate.is_relative_to(static_root):
        headers = (
            {"Cache-Control": "public, max-age=31536000, immutable"}
            if full_path.startswith("assets/")
            else {"Cache-Control": "no-store"}
        )
        return FileResponse(candidate, headers=headers)

    return _index()
