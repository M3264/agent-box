-- Four things, all of which make the operator a participant rather than an audience.
--
-- 1. `questions` — an agent can stop and ask. Until now the only way an agent could
--    involve the operator was an *approval*: a yes/no on an action it had already
--    decided to take. That cannot express "which of these two should I do?", so a
--    model missing a fact either guessed or wrote its uncertainty into a report
--    nobody reads until the end. A question is durable and phase-attached for the
--    same reason an approval is: the waiter is an in-process `asyncio.Event`, but
--    the row is the truth, so a restart re-attaches instead of re-asking.
--
-- 2. Message controls. `job_messages` had exactly one lifecycle — written, then
--    drained at the next phase boundary — and no way back. A queued message could
--    not be corrected or withdrawn, and there was no way to say "stop what you are
--    doing and read this". `delivery` splits those two intents apart and
--    `cancelled_at` gives the operator an undo, which matters most in the window
--    where it is easiest to send the wrong thing.
--
-- 3. `jobs.token_budget` — a cap that stops a job rather than describing it after
--    the fact. Metering told the operator what a job cost once it was over; on a
--    metered endpoint the useful moment is before.
--
-- 4. Prices on `provider_models`, so usage can be read in money. Deliberately not
--    stamped onto `token_usage` at call time: cost is presented as an estimate from
--    the prices currently on file, and re-pricing history when a price is corrected
--    is the behaviour you want from a tool whose real question is "what is this
--    costing me".

-- A question is not an approval. An approval gates an action the agent chose; a
-- question supplies a fact the agent is missing. Mixing them would make the inbox
-- unreadable — "reject" and "no" are not the same answer.
create table questions (
  id       text    primary key,
  job_id   text    not null references jobs(id) on delete cascade,
  -- Null only for a question asked outside a phase, which nothing does today but
  -- which the plan/synthesis calls could reasonably start doing.
  phase_id integer references phases(id) on delete cascade,
  agent    text    not null,
  question text    not null,
  -- Optional context: why it is being asked and what turns on the answer.
  detail   text,
  -- JSON array of {value, label, detail}. Empty for a plain open question. The
  -- agent supplies labels; `value` is derived server-side so the answer written
  -- back into the transcript is stable even if the label is prose.
  options  text    not null default '[]',
  -- Whether an answer outside the options is accepted. An agent offering a closed
  -- set can turn this off, but the UI always shows the escape hatch when it is on,
  -- because a forced choice between two wrong options is worse than no question.
  allow_free_text integer not null default 1,
  -- pending | answered | cancelled | timeout
  status   text    not null default 'pending',
  -- The free text the operator typed, or the chosen option's label.
  answer   text,
  -- The `value` of the option picked, when one was, so a decision can be counted
  -- rather than only read.
  chosen   text,
  created_at  real not null,
  answered_at real
);

create index questions_job on questions(job_id, created_at);
create index questions_status on questions(status, created_at);
create index questions_phase on questions(phase_id);

-- boundary: injected into the next phase's prompt, the original behaviour and
-- still the default, because it is the one that does not interrupt.
-- immediate: injected into the running agent's conversation at its next turn, which
-- is as fast as it can be honoured without killing work in flight.
alter table job_messages add column delivery text not null default 'boundary';

-- Withdrawn before delivery. Distinct from `consumed_at`: consumed means the team
-- read it, cancelled means it never will. Both close the row for the drain query,
-- which is why that query cannot just test `consumed_at is null` any more.
alter table job_messages add column cancelled_at real;

-- Set when the text is edited, so the transcript can say "edited" instead of
-- quietly showing something other than what was sent.
alter table job_messages add column updated_at real;

-- Null means no cap. A cap is on *total* tokens for the job across every round,
-- because per-round caps are the number nobody can predict.
alter table jobs add column token_budget integer;

-- Per one million tokens, in whatever currency the operator prices in. Null means
-- unknown, which is different from free — the UI shows unpriced usage as unpriced
-- rather than folding it in as zero.
alter table provider_models add column price_in  real;
alter table provider_models add column price_out real;

-- v3 of the team: the specialists are told they can ask. Templates are append-only
-- (app/api/config.py), so this inserts a row and moves the default rather than
-- editing v2 out from under jobs that ran on it.
update team_templates set is_default=0 where is_default=1;

insert into team_templates(name, version, roles, is_default, created_at) values(
  'Default delivery team',
  3,
  json_array(
    json_object(
      'id', 'manager',
      'name', 'Manager',
      'orchestrator', json('true'),
      'instructions', 'Own the plan. Break the task into ordered phases with a clear owner and testable acceptance criteria for each, and synthesise the final result from the specialists'' output. Your specialists can run shell commands, read and write files in the job workspace, fetch URLs, and ask the operator a question when the task is genuinely ambiguous. Prefer acceptance criteria that can be checked by running something over ones that can only be asserted. Mark a phase as requiring approval when its work is outward-facing or hard to undo. Do not write the solution yourself.'
    ),
    json_object(
      'id', 'architect',
      'name', 'Architect',
      'orchestrator', json('false'),
      'instructions', 'Own technical design for your phase: structure, interfaces, data flow, and trade-offs, with the reasoning behind each decision. Ground the design in what is actually there — read the real files and run read-only commands to check your assumptions before designing around them. If a decision turns on something only the operator knows — which of two directions they want, a constraint that is not in the repo — call ask_operator with the concrete options rather than picking one and burying the assumption in prose. Say plainly when you could not verify something. Do not assign work to others.'
    ),
    json_object(
      'id', 'coder',
      'name', 'Coder',
      'orchestrator', json('false'),
      'instructions', 'Implement the design for your phase concretely and completely. Write real files into the workspace with write_file rather than pasting code into your answer, and run the code you write. If a command fails, read the error and fix the cause before moving on. Use ask_operator only when you are blocked on a fact you cannot discover by reading or running something — a credential, a target, a choice between two acceptable behaviours. Finish by stating what you changed, which commands you ran, and what their output showed.'
    ),
    json_object(
      'id', 'tester',
      'name', 'Tester',
      'orchestrator', json('false'),
      'instructions', 'Verify the work by executing it, not by describing it. Run the tests, the linter, the command the acceptance criteria imply, and quote the output you actually got. Report each criterion as met or unmet with that evidence attached, and list concrete failures with the exact command that produced them. If the acceptance criteria are ambiguous enough that you could report either way, ask_operator which reading they meant instead of choosing the one that passes. If you could not run something, say so explicitly — an untested criterion is unmet, never assumed passing.'
    )
  ),
  1,
  unixepoch('subsec')
);
