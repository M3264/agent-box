-- Three things this migration exists for.
--
-- 1. A provider is no longer one model. `provider_profiles.model` stays as the
--    profile's *default* model, and `provider_models` holds everything else the
--    endpoint can serve. One row per model rather than a JSON blob so a model can
--    be referenced by a job assignment and still be listed, renamed, or removed
--    without rewriting a document.
--
-- 2. A job no longer has one provider. `jobs.provider_id` stays as the job-level
--    default, and `job_agent_providers` overrides it per agent — the "different
--    provider or model for each agent in a team" case. It is a table, not JSON on
--    the job row, because the foreign key is what stops `DELETE /api/providers/{id}`
--    from hard-deleting a profile some job's audit trail still names.
--
-- 3. Token usage was parsed off every provider response and thrown away.
--    `token_usage` is the per-call ledger; the columns on `jobs` and `phases` are
--    running totals, written in the same transaction as the ledger row so a
--    snapshot can never show a total that disagrees with its detail.

create table provider_models (
  provider_id text    not null references provider_profiles(id) on delete cascade,
  -- The id sent as `model` on the wire, verbatim.
  model       text    not null,
  -- Optional human name; the wire id is used when this is null.
  label       text,
  -- Advisory only: a model the operator knows cannot call tools. The profile-level
  -- `supports_tools` still wins when it is off, since that is about the endpoint.
  supports_tools integer not null default 1,
  created_at  real    not null,
  primary key (provider_id, model)
);

create table job_agent_providers (
  job_id      text not null references jobs(id) on delete cascade,
  -- A role id from the job's team template.
  agent       text not null,
  -- Null means "the job's provider"; a value pins this agent to another profile.
  provider_id text references provider_profiles(id),
  -- Null means "that provider's default model".
  model       text,
  primary key (job_id, agent)
);

create index job_agent_providers_provider on job_agent_providers(provider_id);

create table token_usage (
  id          integer primary key,
  job_id      text    not null references jobs(id) on delete cascade,
  phase_id    integer references phases(id) on delete cascade,
  -- The role that made the call, so cost can be attributed to an agent.
  agent       text,
  provider_id text,
  -- Recorded as the provider reported it, which may differ from what was asked
  -- for: an endpoint that silently substitutes a model should be visible here.
  model       text,
  -- Which call this was: plan | work | synthesis | tool_turn | summary.
  purpose     text,
  prompt_tokens     integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens      integer not null default 0,
  -- Present on some endpoints only; 0 when not reported.
  cached_tokens     integer not null default 0,
  reasoning_tokens  integer not null default 0,
  created_at  real not null
);

create index token_usage_job on token_usage(job_id, id);
create index token_usage_phase on token_usage(phase_id);

alter table jobs add column prompt_tokens     integer not null default 0;
alter table jobs add column completion_tokens integer not null default 0;
alter table jobs add column total_tokens      integer not null default 0;
alter table jobs add column provider_calls    integer not null default 0;

alter table phases add column prompt_tokens     integer not null default 0;
alter table phases add column completion_tokens integer not null default 0;
alter table phases add column total_tokens      integer not null default 0;

-- A job created by "run again" from another one. Null for an original.
alter table jobs add column forked_from text references jobs(id);

-- How many planning rounds this job has had. 1 for every existing job; a follow-up
-- raises it, which is what lets `_finish` judge success on the current round only
-- instead of failing a continued job forever because an earlier round had a
-- failed phase.
alter table jobs add column rounds integer not null default 1;

-- Which round a phase belongs to, so the plan can be read as rounds rather than
-- one flat list. Existing phases are all round 1.
alter table phases add column round integer not null default 1;

-- Backfill the model list from the single model each profile already had, so an
-- existing profile keeps working and immediately has one selectable model.
insert into provider_models(provider_id, model, label, supports_tools, created_at)
select id, model, null, supports_tools, created_at from provider_profiles
where model is not null and trim(model) <> '';
