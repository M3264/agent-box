"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Mode = Literal["controlled", "yolo"]
Decision = Literal["approved", "rejected"]
Sandbox = Literal["sandboxed", "unconfined"]
#: When a queued operator message reaches the team. ``boundary`` waits for the next
#: phase, which is the polite default; ``immediate`` is injected into the running
#: agent's conversation at its next turn, which is as fast as it can be honoured
#: without killing work already in flight.
Delivery = Literal["boundary", "immediate"]

#: Job statuses nothing may write over with a non-terminal one. Lives here, with the
#: other shared vocabulary, because both the engine and the tool loop need it and the
#: engine imports the tool loop — so the tool loop cannot import it back from there.
TERMINAL_JOB_STATUSES = frozenset({"complete", "error", "stopped"})


class AgentAssignment(BaseModel):
    """Which provider and model one agent in the team should use for a job.

    Both fields are optional on purpose, so the three useful cases are all sayable:
    a different provider entirely, the same provider on a different model, or nothing
    at all — which means "whatever the job uses".
    """

    agent: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    provider_id: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("provider_id", "model")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


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
    #: Total tokens this job may spend across every round before it stops itself. None
    #: takes the server default, 0 means no cap. A ceiling rather than a report: on a
    #: metered endpoint the useful moment to find out is before, not after.
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    #: Per-agent overrides of ``provider_id``. The job-level provider stays the
    #: default for every agent that is not listed, so the simple case still needs
    #: nothing but a task.
    agents: list[AgentAssignment] = Field(default_factory=list, max_length=12)

    @field_validator("task")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task must not be blank")
        return stripped

    @field_validator("agents")
    @classmethod
    def _one_per_agent(cls, agents: list[AgentAssignment]) -> list[AgentAssignment]:
        names = [entry.agent for entry in agents]
        if len(names) != len(set(names)):
            raise ValueError("each agent may be assigned at most once")
        return agents


class JobContinue(BaseModel):
    """Another round of work on a job that has already finished.

    The same job, not a copy: its plan grows a round, its conversation continues, and
    everything the earlier rounds produced stays readable as context. This is what
    keeps the chat open after a job completes rather than making the operator restate
    a task they already stated once.
    """

    instruction: str = Field(min_length=1, max_length=20_000)

    @field_validator("instruction")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("instruction must not be blank")
        return stripped


class JobRerun(BaseModel):
    """A fresh job seeded from an existing one.

    For "do that again, but…" — the team, mode, sandbox, provider and per-agent
    assignments are inherited so only what actually changes has to be typed. The new
    job records ``forked_from`` so the pair stays traceable.
    """

    #: None reuses the original task verbatim.
    task: str | None = Field(default=None, max_length=20_000)
    team_id: int | None = None
    mode: Mode | None = None
    #: Omitting these inherits the original's value; sending an explicit ``null``
    #: clears it back to the server default. The two are told apart with
    #: ``model_fields_set``, which is the only way a nullable field can express both
    #: "leave it alone" and "there should not be one" — and the UI needs both, because
    #: a re-run form shows the inherited provider and has to be able to unset it.
    provider_id: str | None = None
    sandbox: Sandbox | None = None
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    #: None inherits the original's assignments; an empty list clears them.
    agents: list[AgentAssignment] | None = Field(default=None, max_length=12)

    @field_validator("task")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("task must not be blank")
        return stripped


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    agent: str = "manager"
    delivery: Delivery = "boundary"

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class MessageUpdate(BaseModel):
    """Change a message that has not been delivered yet.

    Both fields are optional so the two things an operator actually wants are separate
    actions rather than one form: fix the wording, or stop waiting for the phase
    boundary. ``model_fields_set`` tells an omitted field from an explicit one, so
    sending only ``delivery`` never silently rewrites the text.
    """

    content: str | None = Field(default=None, min_length=1, max_length=20_000)
    delivery: Delivery | None = None

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped

    @model_validator(mode="after")
    def _something_to_do(self) -> MessageUpdate:
        if self.content is None and self.delivery is None:
            raise ValueError("send content, delivery, or both")
        return self


class QuestionAnswer(BaseModel):
    """The operator's reply to an agent's question.

    Either names one of the offered options, or types an answer, or both — "the second
    one, but only for the staging bucket" is a real answer and refusing it would push
    the operator into the free-text box and lose which option they meant.
    """

    chosen: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=20_000)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _not_empty(self) -> QuestionAnswer:
        if not self.chosen and not self.text:
            raise ValueError("pick an option or write an answer")
        return self


class BudgetUpdate(BaseModel):
    """Change a job's token cap while it exists.

    Exists mainly for the job that stopped *because* of its cap: without a way to raise
    it, the only route forward is a fresh job that repeats work already paid for. 0
    removes the cap.
    """

    token_budget: int = Field(ge=0, le=1_000_000_000)


class PushKeys(BaseModel):
    """The public halves of a browser's push subscription.

    These are exactly the keys the browser mints for itself; the payload is encrypted
    to them so only that browser can read it. They carry no server secret.
    """

    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscribe(BaseModel):
    """A browser opting in to away-from-browser notifications.

    Shaped like the ``PushSubscription.toJSON()`` the browser emits, so the frontend
    can post it verbatim. Re-subscribing the same browser upserts on ``endpoint``.
    """

    endpoint: str = Field(min_length=1, max_length=2_000)
    keys: PushKeys


class PushUnsubscribe(BaseModel):
    """A browser opting back out, identified by the endpoint it subscribed with."""

    endpoint: str = Field(min_length=1, max_length=2_000)


class ApprovalCreate(BaseModel):
    action: str = Field(min_length=1, max_length=500)
    detail: str | None = None
    risk: str = "high"
    agent: str | None = None
    phase_id: int | None = None


class ApprovalDecision(BaseModel):
    decision: Decision
    note: str | None = Field(default=None, max_length=2_000)


class ProviderModel(BaseModel):
    """One model an endpoint serves."""

    model: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    #: Advisory. For a model the operator knows cannot call tools, on an endpoint that
    #: otherwise can — the profile-level flag still wins when it is off.
    supports_tools: bool = True
    #: Price per million tokens, in whatever currency the operator prices in. None means
    #: unknown, which the UI shows as unpriced rather than as free.
    price_in: float | None = Field(default=None, ge=0, le=10_000)
    price_out: float | None = Field(default=None, ge=0, le=10_000)

    @field_validator("model")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("model must not be blank")
        return stripped


class ProviderUpsert(BaseModel):
    """Provider profiles are written without ever accepting a secret value.

    ``secret_ref`` names an environment variable; the value stays outside the
    database and outside the API surface entirely.
    """

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(min_length=1, max_length=200)
    #: Kept as a Literal rather than validated against the catalogue in
    #: ``app.orchestrator.providers`` so this module stays free of the orchestrator's
    #: imports. ``PROVIDER_KINDS`` there is the source of truth; a test asserts the two
    #: agree, which is cheaper than the layering inversion.
    kind: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=500)
    #: The default model for this profile — what a job gets when it names no model.
    #: Blank is accepted and filled from the first entry of ``models``, so a form that
    #: only lists models does not also have to nominate one.
    model: str = Field(default="", max_length=200)
    #: Everything else the endpoint serves. Replaces the profile's model list wholesale
    #: on write, which is what makes the Settings form a plain edit rather than a diff.
    models: list[ProviderModel] = Field(default_factory=list, max_length=200)
    secret_ref: str | None = Field(default=None, max_length=200)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    #: Off for an endpoint that rejects the ``tools`` parameter outright.
    supports_tools: bool = True

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value

    @model_validator(mode="after")
    def _resolve_default_model(self) -> ProviderUpsert:
        """Reconcile the default model with the list, in whichever direction is needed.

        A profile with no usable model is the failure this catches: it would save
        cleanly and then fail on the first call of every job that used it.
        """
        default = self.model.strip()
        listed = [entry.model for entry in self.models]

        if not default:
            if not listed:
                raise ValueError("a provider needs at least one model")
            default = listed[0]

        if default not in listed:
            # The default is always selectable, so nominating a model implicitly adds
            # it rather than leaving the list disagreeing with the profile row.
            self.models.insert(0, ProviderModel(model=default))

        object.__setattr__(self, "model", default)
        return self


class TeamRole(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=5_000)
    orchestrator: bool = False
    #: A provider this role prefers by default, or None for the job's provider. Lets a
    #: team be built out of different models deliberately, rather than the operator
    #: re-picking the same combination on every job.
    provider_id: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("provider_id", "model")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


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
