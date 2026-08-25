-- Tools: durable records for every command an agent runs, and the settings that
-- decide where it runs.
--
-- Before this, a phase was one prompt and one text answer, so the "tester" could
-- only describe having tested something. Agents now get `run`, `read_file`,
-- `write_file` and `fetch`, which makes a command the first thing in this system
-- with side effects *outside* the database. tool_calls exists so those effects are
-- never invisible: the row is written before the process is spawned, so a crash
-- always leaves evidence that something was in flight.

create table tool_calls(
  id         text primary key,
  job_id     text not null references jobs(id) on delete cascade,
  phase_id   integer references phases(id) on delete cascade,
  turn       integer not null,
  agent      text not null,
  tool       text not null,
  args       text not null,            -- JSON, as the model supplied it
  -- pending: recorded, not yet started (may be waiting on an approval gate)
  -- running: the process was spawned; only recovery may resolve this
  -- ok | error | denied | timeout | interrupted | capped
  status     text not null default 'pending',
  exit_code  integer,
  stdout     text,                     -- truncated slice; the full log is in the workspace
  stderr     text,
  truncated  integer not null default 0,
  sandbox    text,                     -- which backend actually ran it
  approval_id text references approvals(id),
  duration_ms integer,
  created_at real not null,
  started_at  real,
  finished_at real
);
-- (job_id, id) mirrors the events cursor index: the Commands view pages by job.
create index tool_calls_job_idx on tool_calls(job_id, id);
create index tool_calls_phase_idx on tool_calls(phase_id);
-- Recovery's only query: find rows left mid-flight by a crash.
create index tool_calls_status_idx on tool_calls(status);

-- null means "use the server default", so changing the default in Settings applies
-- to future jobs without rewriting history. A job that recorded a backend keeps it
-- across restarts, which is what makes the audit trail trustworthy.
alter table jobs add column sandbox text;

-- Not every OpenAI-compatible endpoint implements function calling. When this is 0
-- the provider omits the `tools` parameter and the loop relies on the documented
-- text-envelope fallback instead of failing outright.
alter table provider_profiles add column supports_tools integer not null default 1;

-- Command gates and phase gates both hang off a phase_id, so without this column
-- `latest_for_phase` — which is how a restart decides whether a phase was already
-- approved — would read the most recent *command* gate as the phase's own verdict. A
-- rejected `git push` would then skip the entire phase on the next boot.
alter table approvals add column kind text not null default 'phase';   -- phase | tool

-- Team templates are append-only (see app/api/config.py), so tool-aware roles are
-- a new version rather than an edit: jobs already pointing at version 1 keep the
-- instructions they actually ran with.
update team_templates set is_default=0 where is_default=1;

insert into team_templates(name, version, roles, is_default, created_at) values(
  'Default delivery team',
  2,
  json_array(
    json_object(
      'id', 'manager',
      'name', 'Manager',
      'orchestrator', json('true'),
      'instructions', 'Own the plan. Break the task into ordered phases with a clear owner and testable acceptance criteria for each, and synthesise the final result from the specialists'' output. Your specialists can run shell commands, read and write files in the job workspace, and fetch URLs, so prefer acceptance criteria that can be checked by running something over ones that can only be asserted. Mark a phase as requiring approval when its work is outward-facing or hard to undo. Do not write the solution yourself.'
    ),
    json_object(
      'id', 'architect',
      'name', 'Architect',
      'orchestrator', json('false'),
      'instructions', 'Own technical design for your phase: structure, interfaces, data flow, and trade-offs, with the reasoning behind each decision. Ground the design in what is actually there — read the real files and run read-only commands to check your assumptions before designing around them. Say plainly when you could not verify something. Do not assign work to others.'
    ),
    json_object(
      'id', 'coder',
      'name', 'Coder',
      'orchestrator', json('false'),
      'instructions', 'Implement the design for your phase concretely and completely. Write real files into the workspace with write_file rather than pasting code into your answer, and run the code you write. If a command fails, read the error and fix the cause before moving on. Finish by stating what you changed, which commands you ran, and what their output showed.'
    ),
    json_object(
      'id', 'tester',
      'name', 'Tester',
      'orchestrator', json('false'),
      'instructions', 'Verify the work by executing it, not by describing it. Run the tests, the linter, the command the acceptance criteria imply, and quote the output you actually got. Report each criterion as met or unmet with that evidence attached, and list concrete failures with the exact command that produced them. If you could not run something, say so explicitly — an untested criterion is unmet, never assumed passing.'
    )
  ),
  1,
  unixepoch('subsec')
);
