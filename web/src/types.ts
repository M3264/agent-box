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
}

export interface JobAction {
  id: string
  status: JobStatus
  paused: boolean
}

export interface Provider {
  id: string
  label: string
  kind: string
  base_url: string
  model: string
  /** A reference to a secret, never the secret itself. */
  secret_ref: string | null
  headers: Record<string, string>
  enabled: boolean
  /** Whether `secret_ref` resolves server-side; null when none is needed. */
  secret_ok: boolean | null
  created_at: number
}

export interface TeamRole {
  id: string
  name: string
  instructions: string
  orchestrator?: boolean
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
