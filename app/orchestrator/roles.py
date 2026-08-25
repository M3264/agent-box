"""Team templates and role resolution.

Roles come from the ``team_templates`` table rather than a hardcoded list, so the
team can change without a deploy. v1 defined ``ROLES`` in ``runtime.py`` while a
``team_templates`` table sat unused and ``team_id`` defaulted to 3 with only
template 1 in existence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.db import Database
from app.logging_setup import get_logger

log = get_logger("agent_hub.roles")


class TeamError(RuntimeError):
    """The requested team template is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    name: str
    instructions: str
    orchestrator: bool = False


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    version: int
    roles: tuple[Role, ...]

    @property
    def orchestrator(self) -> Role:
        for role in self.roles:
            if role.orchestrator:
                return role
        raise TeamError(f"team {self.id} '{self.name}' has no orchestrator role")

    @property
    def specialists(self) -> tuple[Role, ...]:
        return tuple(role for role in self.roles if not role.orchestrator)

    def get(self, role_id: str) -> Role | None:
        return next((role for role in self.roles if role.id == role_id), None)

    def require(self, role_id: str) -> Role:
        role = self.get(role_id)
        if role is None:
            raise TeamError(f"'{role_id}' is not a role in team '{self.name}'")
        return role

    @property
    def role_ids(self) -> tuple[str, ...]:
        return tuple(role.id for role in self.roles)


def _parse_roles(raw: str, team_id: int) -> tuple[Role, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TeamError(f"team {team_id} has malformed roles JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise TeamError(f"team {team_id} has no roles")

    roles: list[Role] = []
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise TeamError(f"team {team_id} has a malformed role entry: {entry!r}")
        roles.append(
            Role(
                id=str(entry["id"]),
                name=str(entry.get("name") or entry["id"].replace("_", " ").title()),
                instructions=str(entry.get("instructions") or ""),
                orchestrator=bool(entry.get("orchestrator")),
            )
        )

    if sum(role.orchestrator for role in roles) != 1:
        raise TeamError(f"team {team_id} must have exactly one orchestrator role")
    return tuple(roles)


def _to_team(row: Any) -> Team:
    return Team(
        id=int(row["id"]),
        name=row["name"],
        version=int(row["version"]),
        roles=_parse_roles(row["roles"], int(row["id"])),
    )


async def load_team(database: Database, team_id: int | None = None) -> Team:
    """Load a team by id, or the default team when ``team_id`` is None."""
    if team_id is not None:
        row = await database.fetch_one("select * from team_templates where id=?", (team_id,))
        if row is None:
            raise TeamError(f"team template {team_id} not found")
        return _to_team(row)

    row = await database.fetch_one(
        "select * from team_templates order by is_default desc, id asc limit 1"
    )
    if row is None:
        raise TeamError("no team templates are configured")
    return _to_team(row)


async def resolve_team_id(database: Database, team_id: int | None) -> int:
    """Validate a requested team id, falling back to the default team.

    Accepting an unknown id and silently ignoring it is what made ``team_id: 3``
    look supported in v1.
    """
    if team_id is not None and await database.exists(
        "select 1 from team_templates where id=?", (team_id,)
    ):
        return team_id
    team = await load_team(database, None)
    if team_id is not None:
        log.warning("unknown team template; using default", extra={"requested": team_id, "team_id": team.id})
    return team.id
