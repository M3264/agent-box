-- Agent Hub v2 initial schema.
--
-- Renames runs -> jobs to match the product vocabulary already used by PLAN.md,
-- the UI and the CLI. Carries over the parts of the v1 schema that were sound
-- (the events cursor index, artifacts, provider profiles, team templates) and
-- adds what the orchestrator needs to resume rather than replay: a real phases
-- table with persisted per-phase output, approvals bound to a phase, and a
-- consumed_at marker on operator messages.

create table jobs(
  id          text primary key,
  task        text not null,
  team_id     integer not null references team_templates(id),
  provider_id text references provider_profiles(id),
  mode        text not null default 'controlled',   -- controlled | yolo
  -- queued | planning | running | blocked | complete | error | stopped
  status      text not null default 'queued',
  paused      integer not null default 0,
  workspace   text,
  result      text,
  error       text,
  created_at  real not null,
  updated_at  real not null
);
create index jobs_created_idx on jobs(created_at desc);
create index jobs_status_idx on jobs(status);

-- Append-only event log. The (job_id, id) index backs cursor replay for both
-- the WebSocket ?after= parameter and the SSE Last-Event-ID header.
create table events(
  id         integer primary key autoincrement,
  job_id     text not null references jobs(id) on delete cascade,
  created_at real not null,
  kind       text not null,
  source     text,
  payload    text not null
);
create index events_job_idx on events(job_id, id);

-- Phases are authored by the manager at plan time, not seeded from a hardcoded
-- role list. `output` is persisted on completion so a restart reads prior work
-- instead of re-deriving it.
create table phases(
  id          integer primary key autoincrement,
  job_id      text not null references jobs(id) on delete cascade,
  seq         integer not null,
  -- plan (seq 0, authors the rest) | work | synthesis (final result)
  kind        text not null default 'work',
  name        text not null,
  owner       text not null,
  acceptance  text,
  depends_on  text not null default '[]',
  -- pending | active | blocked_on_approval | complete | failed | skipped
  status      text not null default 'pending',
  requires_approval integer not null default 0,
  output      text,
  error       text,
  attempts    integer not null default 0,
  started_at  real,
  finished_at real,
  created_at  real not null,
  unique(job_id, seq)
);
create index phases_job_idx on phases(job_id, seq);

create table job_agents(
  job_id         text not null references jobs(id) on delete cascade,
  agent          text not null,
  status         text not null,
  current_action text,
  last_event_id  integer,
  updated_at     real not null,
  primary key(job_id, agent)
);

-- consumed_at is what makes the operator conversation real: the engine drains
-- unconsumed messages into the next phase's prompt and stamps them.
create table job_messages(
  id          integer primary key autoincrement,
  job_id      text not null references jobs(id) on delete cascade,
  agent       text not null,
  role        text not null,          -- operator | agent
  content     text not null,
  created_at  real not null,
  consumed_at real
);
create index job_messages_job_idx on job_messages(job_id, id);
create index job_messages_pending_idx on job_messages(job_id, consumed_at);

-- phase_id ties a gate to the work it blocks, so a restart can re-register the
-- waiter for a phase left in blocked_on_approval instead of re-running it.
create table approvals(
  id            text primary key,
  job_id        text not null references jobs(id) on delete cascade,
  phase_id      integer references phases(id) on delete cascade,
  agent         text,
  action        text not null,
  detail        text,
  risk          text not null default 'high',
  status        text not null default 'pending',   -- pending | approved | rejected
  auto          integer not null default 0,        -- 1 when yolo mode auto-approved
  created_at    real not null,
  decided_at    real,
  decision_note text
);
create index approvals_job_idx on approvals(job_id, created_at);
create index approvals_status_idx on approvals(status, created_at);

create table artifacts(
  id         integer primary key autoincrement,
  job_id     text not null references jobs(id) on delete cascade,
  phase_id   integer references phases(id) on delete set null,
  agent      text,
  name       text not null,
  mime_type  text,
  content    text,
  created_at real not null
);
create index artifacts_job_idx on artifacts(job_id, id);

-- secret_ref names an environment variable (or a ~/.codex/config.toml key as a
-- fallback). The secret value itself is never stored in the database and is
-- never returned by the API.
create table provider_profiles(
  id         text primary key,
  label      text not null,
  kind       text not null default 'openai_compatible',
  base_url   text not null,
  model      text not null,
  secret_ref text,
  headers    text not null default '{}',
  enabled    integer not null default 1,
  created_at real not null
);

create table team_templates(
  id         integer primary key autoincrement,
  name       text not null,
  version    integer not null,
  roles      text not null,
  is_default integer not null default 0,
  created_at real not null
);

-- Default team: manager orchestrates, three specialists execute. The manager
-- owns the plan and never writes solution content; the architect owns technical
-- design inside a phase and never assigns work.
insert into team_templates(id, name, version, roles, is_default, created_at) values(
  1,
  'Default delivery team',
  1,
  json_array(
    json_object(
      'id', 'manager',
      'name', 'Manager',
      'orchestrator', json('true'),
      'instructions', 'Own the plan. Break the task into ordered phases with a clear owner and testable acceptance criteria for each, decide when work needs approval, and synthesise the final result from the specialists'' output. Do not write the solution yourself.'
    ),
    json_object(
      'id', 'architect',
      'name', 'Architect',
      'orchestrator', json('false'),
      'instructions', 'Own technical design for your phase: structure, interfaces, data flow, and trade-offs, with the reasoning behind each decision. Do not assign work to others.'
    ),
    json_object(
      'id', 'coder',
      'name', 'Coder',
      'orchestrator', json('false'),
      'instructions', 'Implement the design for your phase concretely and completely. Produce working code or a precise, directly actionable solution.'
    ),
    json_object(
      'id', 'tester',
      'name', 'Tester',
      'orchestrator', json('false'),
      'instructions', 'Verify the work against your phase''s acceptance criteria. Report each criterion as met or unmet with evidence, list concrete failures, and state your confidence.'
    )
  ),
  1,
  unixepoch('subsec')
);
