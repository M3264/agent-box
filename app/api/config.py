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
from app.config import settings
from app.orchestrator.providers import (
    PROVIDER_KINDS,
    PROVIDER_TEMPLATES,
    ProviderConfigError,
    ProviderError,
    discover_models,
    resolve_secret,
)
from app.orchestrator.roles import TeamError, load_team
from app.orchestrator.sandbox import default_kind, status as sandbox_status

log = get_logger("agent_hub.api.config")

router = APIRouter(prefix="/api", tags=["config"])

ProviderId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]


async def _models_for(provider_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select model,label,supports_tools from provider_models where provider_id=? order by model",
        (provider_id,),
    )
    return [
        {
            "model": row["model"],
            "label": row["label"],
            "supports_tools": bool(row["supports_tools"]),
        }
        for row in rows
    ]


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
    profile["supports_tools"] = bool(profile.get("supports_tools", 1))

    if profile.get("secret_ref"):
        try:
            profile["secret_ok"] = bool(resolve_secret(profile["secret_ref"], profile["id"]))
        except ProviderConfigError:
            profile["secret_ok"] = False
    else:
        profile["secret_ok"] = None  # no secret required
    return profile


@router.get("/provider-kinds")
async def list_provider_kinds() -> list[dict[str, Any]]:
    """The wire protocols this build can speak.

    Exposed so the provider form can *ask* which API an endpoint speaks instead of
    leaving the operator to find out from a failed job. There is one adapter class per
    entry, so this list is short and only grows with a deploy.
    """
    return [dict(kind) for kind in PROVIDER_KINDS]


@router.get("/provider-templates")
async def list_provider_templates() -> list[dict[str, Any]]:
    """Presets that fill in a new profile for a known vendor.

    Distinct from kinds: a template is codeless, so there are many, and picking one
    answers base URL, dialect, secret name and a starting model list in one click.
    """
    return [dict(template) for template in PROVIDER_TEMPLATES]


@router.get("/providers")
async def list_providers() -> list[dict[str, Any]]:
    rows = await db.fetch_all("select * from provider_profiles order by rowid")
    profiles = [_provider_dict(row) for row in rows]
    for profile in profiles:
        profile["models"] = await _models_for(profile["id"])
    return profiles


@router.put("/providers/{provider_id}")
async def upsert_provider(provider_id: ProviderId, payload: ProviderUpsert) -> dict[str, Any]:
    if payload.id != provider_id:
        raise HTTPException(status_code=400, detail="id in the body must match the path")

    # The profile row and its model list are written together: a profile whose default
    # model is not in its own list would be selectable in the UI and unusable in a job.
    async with db.transaction() as conn:
        await conn.execute(
            "insert into provider_profiles(id,label,kind,base_url,model,secret_ref,headers,enabled,"
            "supports_tools,created_at) values(?,?,?,?,?,?,?,?,?,unixepoch('subsec'))"
            " on conflict(id) do update set"
            "  label=excluded.label,kind=excluded.kind,base_url=excluded.base_url,"
            "  model=excluded.model,secret_ref=excluded.secret_ref,headers=excluded.headers,"
            "  enabled=excluded.enabled,supports_tools=excluded.supports_tools",
            (
                payload.id,
                payload.label,
                payload.kind,
                payload.base_url,
                payload.model,
                payload.secret_ref,
                json.dumps(payload.headers),
                int(payload.enabled),
                int(payload.supports_tools),
            ),
        )
        keep = [entry.model for entry in payload.models]
        placeholders = ",".join("?" for _ in keep)
        # Removed models are deleted rather than kept as history: `job_agent_providers`
        # stores the model as plain text with no foreign key precisely so an old job
        # keeps naming what it ran, even after the operator prunes the list.
        await conn.execute(
            f"delete from provider_models where provider_id=? and model not in ({placeholders})"
            if keep
            else "delete from provider_models where provider_id=?",
            (payload.id, *keep),
        )
        for entry in payload.models:
            await conn.execute(
                "insert into provider_models(provider_id,model,label,supports_tools,created_at)"
                " values(?,?,?,?,unixepoch('subsec'))"
                " on conflict(provider_id,model) do update set"
                "  label=excluded.label,supports_tools=excluded.supports_tools",
                (payload.id, entry.model, entry.label, int(entry.supports_tools)),
            )

    row = await db.fetch_one("select * from provider_profiles where id=?", (payload.id,))
    if row is None:  # pragma: no cover - written immediately above
        raise HTTPException(status_code=500, detail="provider could not be read back")
    log.info(
        "provider profile saved",
        extra={"provider": payload.id, "enabled": payload.enabled, "models": len(payload.models)},
    )
    profile = _provider_dict(row)
    profile["models"] = await _models_for(payload.id)
    return profile


@router.post("/providers/{provider_id}/models/discover")
async def discover_provider_models(provider_id: ProviderId) -> dict[str, Any]:
    """Ask the endpoint what it serves, and report it without saving anything.

    Read-only on purpose. A gateway may list hundreds of models, and quietly writing
    all of them into the profile would turn the model picker into a haystack — the
    operator chooses from this and saves what they want.
    """
    row = await db.fetch_one("select * from provider_profiles where id=?", (provider_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    profile = _provider_dict(row)
    try:
        found = await discover_models(profile)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:
        # 502, not 500: the endpoint answered badly or not at all, and the operator can
        # still type model ids in by hand.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    known = {entry["model"] for entry in await _models_for(provider_id)}
    return {
        "provider_id": provider_id,
        "count": len(found),
        "models": [{**entry, "known": entry["model"] in known} for entry in found],
    }


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: ProviderId) -> dict[str, Any]:
    """Delete a profile, or disable it if jobs still reference it.

    Deleting a referenced profile would either break the foreign key or orphan the
    audit trail of which provider ran a job, so history wins and the profile is
    disabled instead. The response says which happened.
    """
    if not await db.exists("select 1 from provider_profiles where id=?", (provider_id,)):
        raise HTTPException(status_code=404, detail="provider not found")

    # Both references count. A profile can now be named by a single agent of a job
    # whose own `provider_id` is something else, and deleting it would erase the record
    # of what that agent actually ran on.
    in_use = int(
        await db.fetch_value(
            "select count(*) from jobs where provider_id=?", (provider_id,), default=0
        )
    ) + int(
        await db.fetch_value(
            "select count(*) from job_agent_providers where provider_id=?",
            (provider_id,),
            default=0,
        )
    )
    if in_use:
        await db.execute("update provider_profiles set enabled=0 where id=?", (provider_id,))
        return {"id": provider_id, "deleted": False, "disabled": True, "jobs": in_use}

    try:
        await db.execute("delete from provider_profiles where id=?", (provider_id,))
    except IntegrityError:
        await db.execute("update provider_profiles set enabled=0 where id=?", (provider_id,))
        return {"id": provider_id, "deleted": False, "disabled": True, "jobs": in_use}
    return {"id": provider_id, "deleted": True, "disabled": False, "jobs": 0}


# ------------------------------------------------------------------------ sandbox


@router.get("/sandbox")
async def get_sandbox() -> dict[str, Any]:
    """Which confinement backends work on this host, and which one jobs get.

    The New Job picker is built from this rather than from a hardcoded list, because
    ``sandboxed`` depends on whether the kernel allows unprivileged user namespaces —
    a fact only the startup probe knows. Offering a backend that fails on every
    command would be worse than not offering it, and silently substituting a weaker
    one would be worse still.

    ``default`` is set by ``AGENT_HUB_SANDBOX`` on the service, not through the API:
    it is an operator decision about the host, and a job that wants the other one
    says so at creation.
    """
    probed = sandbox_status()
    backends = [
        {
            "id": state.id,
            "label": state.label,
            "available": state.available,
            "reason": state.reason,
        }
        for state in probed.values()
    ]
    default = default_kind()
    default_state = probed.get(default)
    return {
        "default": default,
        "default_available": default_state.available if default_state else None,
        "tools_enabled": settings.tools_enabled,
        "network": settings.tool_network,
        "backends": backends,
        "limits": {
            "max_turns": settings.tool_max_turns,
            "command_timeout": settings.tool_timeout,
            "wall_clock": settings.tool_wall_clock,
            "output_limit": settings.tool_output_limit,
        },
    }


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
