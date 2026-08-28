/** Shapes returned by the FastAPI layer. Kept narrow: only fields the UI reads. */

export type JobStatus =
  | 'queued'
  | 'planning'
  | 'running'
  | 'blocked'
  | 'complete'
  | 'error'
  | 'stopped'

export type PhaseStatus =
  | 'pending'
  | 'active'
  | 'blocked_on_approval'
  | 'complete'
  | 'failed'
  | 'skipped'

export type Mode = 'controlled' | 'yolo'

export type SandboxKind = 'sandboxed' | 'unconfined'

export type ApprovalStatus = 'pending' | 'approved' | 'rejected'

/**
 * When a queued message reaches the team.
 *
 * `boundary` waits for the phase to end, so the team reads it between pieces of work.
 * `immediate` is delivered to the running agent's next turn — which is what "send it
 * now" means, and why the two are one field rather than two endpoints.
 */
export type Delivery = 'boundary' | 'immediate'

export type QuestionStatus = 'pending' | 'answered' | 'cancelled' | 'timeout'

/** One choice an agent offered. `value` is derived server-side and is stable. */
export interface QuestionOption {
  value: string
  label: string
  detail: string
}

/**
 * An agent asking the operator something it cannot work out for itself.
 *
 * Distinct from an approval: an approval is a veto on an action already chosen, where
 * the only answers are yes and no. This carries the agent's own options, or none, and
 * the answer is prose the model reads.
 */
export interface Question {
  id: string
  job_id: string
  phase_id: number | null
  agent: string
  question: string
  detail: string | null
  options: QuestionOption[]
  allow_free_text: boolean
  status: QuestionStatus
  answer: string | null
  chosen: string | null
  created_at: number
  answered_at: number | null
  /** Present on the joined endpoints. Which phase is stuck is half the context. */
  phase_name?: string | null
}

/**
 * How a command ended.
 *
 * `running` is not a transient UI state — it is what the database holds while the
 * process exists, and what a crash leaves behind until recovery turns it into
 * `interrupted`.
 */
export type ToolCallStatus =
  | 'pending'
  | 'running'
  | 'ok'
  | 'error'
  | 'denied'
  | 'refused'
  | 'timeout'
  | 'interrupted'
  | 'capped'
  | 'cancelled'

export interface ToolCall {
  id: string
  job_id: string
  phase_id: number | null
  turn: number
  agent: string
  tool: 'run' | 'read_file' | 'write_file' | 'fetch' | string
  args: Record<string, unknown>
  status: ToolCallStatus
  exit_code: number | null
  truncated: boolean
  sandbox: SandboxKind | null
  approval_id: string | null
  duration_ms: number | null
  created_at: number
  started_at: number | null
  finished_at: number | null
  /** Only on the single-call endpoint: the list omits output on purpose. */
  stdout?: string | null
  stderr?: string | null
  stdout_path?: string
  stderr_path?: string
}

export interface SandboxBackend {
  id: SandboxKind
  label: string
  available: boolean
  reason: string
}

export interface SandboxInfo {
  default: SandboxKind
  default_available: boolean | null
  tools_enabled: boolean
  network: boolean
  backends: SandboxBackend[]
  limits: {
    max_turns: number
    command_timeout: number
    wall_clock: number
    output_limit: number
  }
}

export interface JobSummary {
  id: string
  task: string
  status: JobStatus
  paused: boolean
  mode: Mode
  team_id: number
  provider_id: string | null
  /** Which confinement the job ran under; null until the engine resolves it. */
  sandbox: SandboxKind | null
  created_at: number
  updated_at: number
  error: string | null
  pending_approvals: number
  /** Open questions from the agents. Blocks the job exactly like a gate does. */
  pending_questions: number
  /** Queued operator messages the team has not read yet — editable until it does. */
  pending_messages: number
  phase_total: number
  phase_complete: number
  artifact_count: number
  /** Running totals, written as each provider call returns. */
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  provider_calls: number
  /** The job's own token cap. Null means "the server default when it runs". */
  token_budget: number | null
  /** How many rounds of work the operator has asked for. 1 for most jobs. */
  rounds: number
  /** The job this one was re-run from, if any. */
  forked_from: string | null
}

export interface Phase {
  id: number
  job_id: string
  seq: number
  kind: 'plan' | 'work' | 'synthesis'
  name: string
  owner: string
  acceptance: string | null
  depends_on: number[]
  status: PhaseStatus
  requires_approval: boolean
  output: string | null
  error: string | null
  attempts: number
  started_at: number | null
  finished_at: number | null
  /** Which round of the conversation produced this phase. */
  round: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface Agent {
  job_id: string
  agent: string
  status: string
  current_action: string | null
  updated_at: number
}

export interface Artifact {
  id: number
  job_id: string
  phase_id: number | null
  agent: string | null
  name: string
  mime_type: string
  size: number
  created_at: number
}

export interface JobMessage {
  id: number
  job_id: string
  agent: string | null
  role: string
  content: string
  created_at: number
  consumed_at: number | null
  /** When it reaches the team. Only meaningful while `consumed_at` is null. */
  delivery: Delivery
  /** Withdrawn by the operator before delivery. Kept in the log rather than deleted. */
  cancelled_at: number | null
  /** Last edit, so the UI can say so instead of silently showing different words. */
  updated_at: number | null
}

/** A queued message joined to its job, as the cross-job inbox returns it. */
export interface InboxMessage extends JobMessage {
  job_task: string
  job_status: JobStatus
}

export interface Approval {
  id: string
  job_id: string
  phase_id: number | null
  agent: string | null
  action: string
  detail: string | null
  risk: string | null
  status: ApprovalStatus
  auto: boolean
  decision_note: string | null
  created_at: number
  decided_at: number | null
  /** Present on the joined endpoints (`/api/approvals`, `/api/jobs/{id}/approvals`). */
  phase_name?: string | null
  phase_seq?: number | null
}

/** An approval joined to its job, as the cross-job inbox returns it. */
export interface InboxApproval extends Approval {
  job_task: string
  job_status: JobStatus
  job_mode: Mode
}

/** A question joined to its job. */
export interface InboxQuestion extends Question {
  job_task: string
  job_status: JobStatus
}

/**
 * Everything waiting on the operator, across every job, oldest first.
 *
 * `counts.blocking` is what a badge shows: gates and questions, which hold work up. A
 * queued message does not, so it is counted but deliberately kept out of that figure.
 */
export interface Attention {
  approvals: InboxApproval[]
  questions: InboxQuestion[]
  messages: InboxMessage[]
  counts: { approvals: number; questions: number; messages: number; blocking: number }
}

export interface JobEvent {
  id: number
  job_id: string
  created_at: number
  kind: string
  source: string | null
  payload: Record<string, unknown>
}

/** What a job spent, in total and broken down. */
export interface JobUsage {
  totals: { prompt: number; completion: number; total: number; calls: number }
  by_agent: { agent: string | null; calls: number; prompt: number; completion: number; total: number }[]
  by_model: {
    provider_id: string | null
    model: string | null
    calls: number
    prompt: number
    completion: number
    total: number
    /** Null when no price is on file for that model — not zero. */
    cost: number | null
  }[]
  /** Estimated spend in USD, or null when nothing billed is priced. */
  cost: number | null
  /** Tokens that went through a model with no price, so the estimate excludes them. */
  unpriced_tokens: number
  /** The cap and how much of it is gone. `limit: 0` means no cap. */
  budget: { limit: number; used: number; remaining: number | null }
}

/** One agent's pinned provider and model for a job. */
export interface AgentProvider {
  agent: string
  provider_id: string | null
  model: string | null
}

/**
 * Everything New Job needs to open prefilled from an existing job.
 *
 * `from` is the job id rather than a copy of its settings, because the re-run posts
 * to `/rerun` so the server records `forked_from` and inherits anything the operator
 * did not change.
 */
export interface JobSeed {
  from: string
  task: string
  mode: Mode
  team_id: number
  provider_id: string | null
  sandbox: SandboxKind | null
  /** Null means the job carried no cap of its own and took the server's. */
  token_budget: number | null
  agents: AgentProvider[]
}

/**
 * A live retune of a job that has not finished — everything New Job sets except the
 * task itself. Sent by Edit config; the server applies it at the next phase boundary,
 * never mid-phase.
 *
 * Every field is optional so only what changes is sent. An explicit `null` clears a
 * nullable field back to the server default (provider, sandbox, budget → the default;
 * `team_id` → the default template), exactly as the re-run form does.
 */
export interface JobPatchPayload {
  mode?: Mode
  team_id?: number | null
  provider_id?: string | null
  sandbox?: SandboxKind | null
  token_budget?: number | null
  agents?: AgentProvider[]
}

export interface JobSnapshot
  extends Omit<
    JobSummary,
    'pending_approvals' | 'pending_questions' | 'pending_messages' | 'phase_total' | 'phase_complete' | 'artifact_count'
  > {
  workspace: string | null
  /** Which confinement this job actually ran under; null when tools are off. */
  sandbox: SandboxKind | null
  result: { content: string } | null
  team: Agent[]
  plan: Phase[]
  artifacts: Artifact[]
  messages: JobMessage[]
  approvals: Approval[]
  questions: Question[]
  tool_calls: ToolCall[]
  events: JobEvent[]
  cursor: number
  usage: JobUsage
  agent_providers: AgentProvider[]
  /** True once the job is terminal: the conversation can take another round. */
  can_continue: boolean
}

export interface JobAction {
  id: string
  status: JobStatus
  paused: boolean
}

/** One model an endpoint serves. */
export interface ProviderModel {
  model: string
  label: string | null
  supports_tools: boolean
  /** USD per million prompt tokens. Null means no estimate is possible. */
  price_in: number | null
  /** USD per million completion tokens. */
  price_out: number | null
}

/**
 * A wire protocol this build can speak. There is one adapter class per kind, so the
 * list is short and only changes with a deploy — which is why the form asks rather
 * than guessing.
 */
export interface ProviderKind {
  id: string
  label: string
  detail: string
  auth: string
  models_path: string | null
  supports_tools: boolean
}

/** A codeless vendor preset that fills in a new profile. */
export interface ProviderTemplate {
  id: string
  label: string
  kind: string
  base_url: string
  secret_ref: string | null
  models: string[]
  headers: Record<string, string>
}

export interface Provider {
  id: string
  label: string
  kind: string
  base_url: string
  /** The default model — what a job gets when it names none. One of `models`. */
  model: string
  /** Everything this endpoint serves. A provider is not one model. */
  models: ProviderModel[]
  /** A reference to a secret, never the secret itself. */
  secret_ref: string | null
  headers: Record<string, string>
  enabled: boolean
  supports_tools: boolean
  /** Whether the profile's credential resolves server-side; null when none is needed. */
  secret_ok: boolean | null
  /**
   * Whether a key was pasted into the server's protected store for this profile. The
   * value never leaves the server — this boolean is all the UI is told, so it can show a
   * stored-key state and offer to clear it.
   */
  has_saved_secret: boolean
  created_at: number
}

/** What discovery found on an endpoint. Nothing is saved until the operator says so. */
export interface DiscoveredModels {
  provider_id: string
  count: number
  models: { model: string; label: string | null; known: boolean }[]
}

/** The result of one live test call against a provider and model. */
export interface ProviderTestResult {
  ok: boolean
  /** The model that actually answered, as the endpoint reported it. */
  model: string
  /** Round-trip time of the single ping, in milliseconds. */
  latency_ms: number
}

export interface TeamRole {
  id: string
  name: string
  instructions: string
  orchestrator?: boolean
  /** A provider this role prefers, or null for the job's. */
  provider_id?: string | null
  model?: string | null
}

export interface Team {
  id: number
  name: string
  version: number
  roles: TeamRole[]
  is_default: boolean
  created_at: number
}

/**
 * What a browser needs to opt in to Web Push, plus enough to show current state.
 *
 * `key` is the public VAPID `applicationServerKey` — the private half never leaves the
 * server. `configured: false` means push is switched off there, so the UI offers no
 * switch rather than a switch that cannot deliver.
 */
export interface PushInfo {
  configured: boolean
  key: string | null
  /** How many browsers are subscribed right now, across everyone who opted in. */
  subscribers: number
}

/** The subscription shape `/api/push/subscribe` takes — the browser's `toJSON()`. */
export interface PushSubscriptionPayload {
  endpoint: string
  keys: { p256dh: string; auth: string }
}

export interface Health {
  status: 'ok' | 'degraded'
  version: string
  schema_version: number
  active_jobs: number
  detail: {
    subscribers?: number
    jobs?: number
    pending_approvals?: number
    db_path?: string
    tools_enabled?: boolean
    sandbox_default?: SandboxKind
    sandboxes?: Record<string, { available: boolean; reason: string }>
    /** Away-from-browser push: whether a VAPID key is configured, and subscriber count. */
    push?: { configured: boolean; subscribers: number }
    error?: string
  }
}
