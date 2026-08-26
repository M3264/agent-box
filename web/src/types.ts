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
  phase_total: number
  phase_complete: number
  artifact_count: number
  /** Running totals, written as each provider call returns. */
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  provider_calls: number
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
  by_model: { provider_id: string | null; model: string | null; calls: number; total: number }[]
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
  agents: AgentProvider[]
}

export interface JobSnapshot extends Omit<JobSummary, 'pending_approvals' | 'phase_total' | 'phase_complete' | 'artifact_count'> {
  workspace: string | null
  /** Which confinement this job actually ran under; null when tools are off. */
  sandbox: SandboxKind | null
  result: { content: string } | null
  team: Agent[]
  plan: Phase[]
  artifacts: Artifact[]
  messages: JobMessage[]
  approvals: Approval[]
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
  /** Whether `secret_ref` resolves server-side; null when none is needed. */
  secret_ok: boolean | null
  created_at: number
}

/** What discovery found on an endpoint. Nothing is saved until the operator says so. */
export interface DiscoveredModels {
  provider_id: string
  count: number
  models: { model: string; label: string | null; known: boolean }[]
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
    error?: string
  }
}
