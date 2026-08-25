"""Provider profile and team template endpoints.

Two things that existed in v1 as tables and endpoints but changed nothing:
``provider_profiles`` was ignored in favour of reading ``~/.codex/config.toml`` on
every run, and ``team_templates`` was ignored in favour of a hardcoded role list.
Both are now the actual source of configuration, so these endpoints matter.

Secret values never cross this boundary. A profile stores a ``secret_ref`` naming
an environment variable; the value is resolved server-side at call time and is
never written to the database or returned by the API.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path
from sqlite3 import IntegrityError

from app.deps import db
from app.logging_setup import get_logger
from app.models import ProviderUpsert, TemplateCreate
from app.orchestrator.providers import ProviderConfigError, resolve_secret
from app.orchestrator.roles import TeamError, load_team

log = get_logger("agent_hub.api.config")

router = APIRouter(prefix="/api", tags=["config"])

ProviderId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]


def _provider_dict(row: Any) -> dict[str, Any]:
    """Shape a profile for the API — reference only, never the secret itself.

    ``secret_ok`` answers the question the Settings screen actually has ("will a job
    using this profile authenticate?") without exposing the value: a misconfigured
    ``secret_ref`` is otherwise only discoverable by watching a job fail.
    """
    profile = dict(row)
    try:
        profile["headers"] = json.loads(profile.get("headers") or "{}")
    except json.JSONDecodeError:
        profile["headers"] = {}
    profile["enabled"] = bool(profile.get("enabled"))

    if profile.get("secret_ref"):
        try:
            profile["secret_ok"] = bool(resolve_secret(profile["secret_ref"], profile["id"]))
        except ProviderConfigError:
            profile["secret_ok"] = False
    else:
        profile["secret_ok"] = None  # no secret required
    return profile


@router.get("/providers")
async def list_providers() -> list[dict[str, Any]]:
    rows = await db.fetch_all("select * from provider_profiles order by rowid")
    return [_provider_dict(row) for row in rows]


@router.put("/providers/{provider_id}")
async def upsert_provider(provider_id: ProviderId, payload: ProviderUpsert) -> dict[str, Any]:
    if payload.id != provider_id:
        raise HTTPException(status_code=400, detail="id in the body must match the path")

    await db.execute(
        "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,enabled,"
        "created_at) values(?,?,?,?,?,?,?,?,unixepoch('subsec'))"
        " on conflict(id) do update set"
        "  label=excluded.label,kind=excluded.kind,base_url=excluded.base_url,"
        "  model=excluded.model,secret_ref=excluded.secret_ref,headers=excluded.headers,"
        "  enabled=excluded.enabled",
        (
            payload.id,
            payload.label,
            payload.kind,
            payload.base_url,
            payload.model,
            payload.secret_ref,
            json.dumps(payload.headers),
            int(payload.enabled),
        ),
    )
    row = await db.fetch_one("select * from provider_profiles where id=?", (payload.id,))
    if row is None:  # pragma: no cover - written immediately above
        raise HTTPException(status_code=500, detail="provider could not be read back")
    log.info("provider profile saved", extra={"provider": payload.id, "enabled": payload.enabled})
    return _provider_dict(row)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: ProviderId) -> dict[str, Any]:
    """Delete a profile, or disable it if jobs still reference it.

    Deleting a referenced profile would either break the foreign key or orphan the
    audit trail of which provider ran a job, so history wins and the profile is
    disabled instead. The response says which happened.
    """
    if not await db.exists("select 1 from provider_profiles where id=?", (provider_id,)):
        raise HTTPException(status_code=404, detail="provider not found")

    in_use = await db.fetch_value(
        "select count(*) from jobs where provider_id=?", (provider_id,), default=0
    )
    if in_use:
        await db.execute("update provider_profiles set enabled=0 where id=?", (provider_id,))
        return {"id": provider_id, "deleted": False, "disabled": True, "jobs": int(in_use)}

    try:
        await db.execute("delete from provider_profiles where id=?", (provider_id,))
    except IntegrityError:
        await db.execute("update provider_profiles set enabled=0 where id=?", (provider_id,))
        return {"id": provider_id, "deleted": False, "disabled": True, "jobs": int(in_use)}
    return {"id": provider_id, "deleted": True, "disabled": False, "jobs": 0}


# --------------------------------------------------------------------------- teams


def _template_dict(row: Any) -> dict[str, Any]:
    template = dict(row)
    try:
        template["roles"] = json.loads(template.get("roles") or "[]")
    except json.JSONDecodeError:
        template["roles"] = []
    template["is_default"] = bool(template.get("is_default"))
    return template


@router.get("/teams")
async def list_teams() -> list[dict[str, Any]]:
    rows = await db.fetch_all("select * from team_templates order by is_default desc, id")
    return [_template_dict(row) for row in rows]


@router.get("/teams/{team_id}")
async def get_team(team_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    row = await db.fetch_one("select * from team_templates where id=?", (team_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="team template not found")
    return _template_dict(row)


@router.post("/teams", status_code=201)
async def create_team(payload: TemplateCreate) -> dict[str, Any]:
    """Add a team template.

    Templates are append-only: an existing one may be referenced by jobs whose
    phases name its roles, and rewriting it would retroactively change what those
    jobs ran with.
    """
    roles = [role.model_dump() for role in payload.roles]
    team_id = await db.insert(
        "insert into team_templates(name,version,roles,is_default,created_at)"
        " values(?,1,?,0,unixepoch('subsec'))",
        (payload.name, json.dumps(roles)),
    )
    try:
        # Round-trips through the same parser the engine uses, so an unusable
        # template fails here rather than at the first job that selects it.
        await load_team(db, team_id)
    except TeamError as exc:
        await db.execute("delete from team_templates where id=?", (team_id,))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = await db.fetch_one("select * from team_templates where id=?", (team_id,))
    if row is None:  # pragma: no cover - written immediately above
        raise HTTPException(status_code=500, detail="team template could not be read back")
    log.info("team template created", extra={"team_id": team_id, "roles": len(roles)})
    return _template_dict(row)


@router.post("/teams/{team_id}/default")
async def set_default_team(team_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    if not await db.exists("select 1 from team_templates where id=?", (team_id,)):
        raise HTTPException(status_code=404, detail="team template not found")
    async with db.transaction() as conn:
        await conn.execute("update team_templates set is_default=0 where is_default=1")
        await conn.execute("update team_templates set is_default=1 where id=?", (team_id,))
    row = await db.fetch_one("select * from team_templates where id=?", (team_id,))
    return _template_dict(row)
