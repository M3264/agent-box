"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Mode = Literal["controlled", "yolo"]
Decision = Literal["approved", "rejected"]
Sandbox = Literal["sandboxed", "unconfined"]

#: Job statuses nothing may write over with a non-terminal one. Lives here, with the
#: other shared vocabulary, because both the engine and the tool loop need it and the
#: engine imports the tool loop — so the tool loop cannot import it back from there.
TERMINAL_JOB_STATUSES = frozenset({"complete", "error", "stopped"})


class JobCreate(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    #: None means "whichever template is flagged default", resolved at creation.
    #: Naming a fixed id here is how v1 ended up defaulting to team 3, which never
    #: existed.
    team_id: int | None = None
    mode: Mode = "controlled"
    provider_id: str | None = None
    #: None means "use the server default at the moment the job starts". Recording
    #: the resolved value on the row (rather than reading the setting per command)
    #: is what keeps the audit trail honest if the default changes mid-job.
    sandbox: Sandbox | None = None

    @field_validator("task")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task must not be blank")
        return stripped


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    agent: str = "manager"

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class ApprovalCreate(BaseModel):
    action: str = Field(min_length=1, max_length=500)
    detail: str | None = None
    risk: str = "high"
    agent: str | None = None
    phase_id: int | None = None


class ApprovalDecision(BaseModel):
    decision: Decision
    note: str | None = Field(default=None, max_length=2_000)


class ProviderUpsert(BaseModel):
    """Provider profiles are written without ever accepting a secret value.

    ``secret_ref`` names an environment variable; the value stays outside the
    database and outside the API surface entirely.
    """

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    secret_ref: str | None = Field(default=None, max_length=200)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value


class TeamRole(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=5_000)
    orchestrator: bool = False


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    roles: list[TeamRole] = Field(min_length=1, max_length=12)

    @field_validator("roles")
    @classmethod
    def _one_orchestrator(cls, roles: list[TeamRole]) -> list[TeamRole]:
        ids = [role.id for role in roles]
        if len(ids) != len(set(ids)):
            raise ValueError("role ids must be unique")
        leads = [role.id for role in roles if role.orchestrator]
        if len(leads) != 1:
            raise ValueError("exactly one role must be the orchestrator")
        if len(roles) < 2:
            raise ValueError("a team needs the orchestrator plus at least one specialist")
        return roles


class JobAction(BaseModel):
    """Response shape for pause/resume/stop."""

    id: str
    status: str
    paused: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    schema_version: int
    active_jobs: int
    detail: dict[str, Any] = Field(default_factory=dict)
