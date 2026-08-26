/**
 * HTTP and WebSocket access.
 *
 * The mount prefix is derived at runtime rather than baked in at build time: the
 * same bundle is served at `/` and at `/hub/`, and the `/hub/` nginx location
 * strips the prefix before proxying, so the server sees `/`-rooted paths either
 * way. Everything client-side has to put the prefix back.
 */

import type {
  AgentProvider,
  Approval,
  ApprovalStatus,
  Attention,
  Delivery,
  DiscoveredModels,
  Health,
  InboxApproval,
  JobAction,
  JobMessage,
  JobSnapshot,
  JobSummary,
  JobUsage,
  Mode,
  Phase,
  Provider,
  ProviderKind,
  ProviderTemplate,
  Question,
  SandboxInfo,
  SandboxKind,
  Team,
  TeamRole,
  ToolCall,
} from './types'

/** `/hub` when served from the sub-path mount, otherwise the empty string. */
export const mountPrefix: string = window.location.pathname.startsWith('/hub') ? '/hub' : ''

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${mountPrefix}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    // FastAPI returns {detail: string} for HTTPException and {detail: [...]} for
    // validation errors; both are worth surfacing verbatim rather than "failed".
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) {
        detail = body.detail
          .map((item) => {
            const issue = item as { loc?: unknown[]; msg?: string }
            const field = Array.isArray(issue.loc) ? issue.loc.slice(1).join('.') : ''
            return field ? `${field}: ${issue.msg}` : (issue.msg ?? '')
          })
          .join('; ')
      }
    } catch {
      /* non-JSON error body; the status line is all we have */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** WebSocket URL for a path, honouring the mount prefix and TLS. */
export function socketUrl(path: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}${mountPrefix}${path}`
}

/** Browser-navigable URL for a download, so the browser handles the response. */
export function downloadUrl(jobId: string, artifactId: number): string {
  return `${mountPrefix}/api/jobs/${jobId}/artifacts/${artifactId}`
}

const body = (payload: unknown): RequestInit => ({
  method: 'POST',
  body: JSON.stringify(payload),
})

const patch = (payload: unknown): RequestInit => ({
  method: 'PATCH',
  body: JSON.stringify(payload),
})

export const api = {
  health: () => request<Health>('/api/health'),

  jobs: (limit = 100) => request<JobSummary[]>(`/api/jobs?limit=${limit}`),
  job: (id: string) => request<JobSnapshot>(`/api/jobs/${id}`),
  createJob: (payload: {
    task: string
    mode: Mode
    team_id?: number
    provider_id?: string | null
    sandbox?: SandboxKind | null
    /** Total tokens this job may spend. 0 is an explicit "no cap". */
    token_budget?: number | null
    /** Per-agent overrides. Omitted agents use the job's provider. */
    agents?: AgentProvider[]
  }) => request<{ id: string }>('/api/jobs', body(payload)),

  plan: (id: string) => request<Phase[]>(`/api/jobs/${id}/plan`),
  messages: (id: string) => request<JobMessage[]>(`/api/jobs/${id}/messages`),
  sendMessage: (id: string, content: string, delivery: Delivery = 'boundary') =>
    request<JobMessage>(`/api/jobs/${id}/messages`, body({ content, delivery })),
  /**
   * Change a queued message before the team reads it.
   *
   * Omitting `content` and setting `delivery: 'immediate'` is "send it now" — the same
   * endpoint, because expediting a message *is* an edit of when it arrives, and two
   * endpoints would let the two race each other.
   */
  editMessage: (
    id: string,
    messageId: number,
    payload: { content?: string; delivery?: Delivery },
  ) => request<JobMessage>(`/api/jobs/${id}/messages/${messageId}`, patch(payload)),
  /** Withdraw it. The row stays in the log, stamped `cancelled_at`. */
  cancelMessage: (id: string, messageId: number) =>
    request<JobMessage & { cancelled: boolean }>(`/api/jobs/${id}/messages/${messageId}`, {
      method: 'DELETE',
    }),

  questions: (id: string) => request<Question[]>(`/api/jobs/${id}/questions`),
  /** Unblock an agent. Either a `chosen` option value, free text, or both. */
  answerQuestion: (
    id: string,
    questionId: string,
    payload: { chosen?: string; text?: string },
  ) => request<Question>(`/api/jobs/${id}/questions/${questionId}/answer`, body(payload)),

  usage: (id: string) => request<JobUsage>(`/api/jobs/${id}/usage`),
  /** Raise, lower or lift the cap. 0 means no cap; returns the whole ledger back. */
  setBudget: (id: string, tokenBudget: number) =>
    request<JobUsage>(`/api/jobs/${id}/budget`, patch({ token_budget: tokenBudget })),
  /** Another round on the same job: the conversation continues where it stopped. */
  continueJob: (id: string, instruction: string) =>
    request<{ id: string; status: string; round: number }>(
      `/api/jobs/${id}/continue`,
      body({ instruction }),
    ),
  /** A fresh job seeded from this one — settings inherited, workspace clean. */
  rerunJob: (
    id: string,
    payload: {
      task?: string
      team_id?: number
      mode?: Mode
      provider_id?: string | null
      sandbox?: SandboxKind | null
      agents?: AgentProvider[]
      token_budget?: number | null
    } = {},
  ) => request<{ id: string; forked_from: string }>(`/api/jobs/${id}/rerun`, body(payload)),

  toolCalls: (id: string, phaseId?: number) =>
    request<ToolCall[]>(
      `/api/jobs/${id}/tool-calls${phaseId ? `?phase_id=${phaseId}` : ''}`,
    ),
  toolCall: (id: string, callId: string) => request<ToolCall>(`/api/jobs/${id}/tool-calls/${callId}`),

  pause: (id: string) => request<JobAction>(`/api/jobs/${id}/pause`, { method: 'POST' }),
  resume: (id: string) => request<JobAction>(`/api/jobs/${id}/resume`, { method: 'POST' }),
  stop: (id: string) => request<JobAction>(`/api/jobs/${id}/stop`, { method: 'POST' }),

  /**
   * Everything waiting on the operator, across every job.
   *
   * One request rather than three: the question "is anything waiting for me" is not
   * per-kind, and three polls would let the badge disagree with itself.
   */
  attention: (limit = 200) => request<Attention>(`/api/attention?limit=${limit}`),

  inbox: (status: ApprovalStatus | 'all' = 'pending') =>
    request<InboxApproval[]>(`/api/approvals?status=${status}`),
  decide: (jobId: string, approvalId: string, decision: 'approved' | 'rejected', note?: string) =>
    request<Approval>(
      `/api/jobs/${jobId}/approvals/${approvalId}`,
      body({ decision, note: note || null }),
    ),

  providers: () => request<Provider[]>('/api/providers'),
  providerKinds: () => request<ProviderKind[]>('/api/provider-kinds'),
  providerTemplates: () => request<ProviderTemplate[]>('/api/provider-templates'),
  saveProvider: (profile: Omit<Provider, 'secret_ok' | 'created_at'>) =>
    request<Provider>(`/api/providers/${profile.id}`, {
      method: 'PUT',
      body: JSON.stringify(profile),
    }),
  /** Ask the endpoint what it serves. Saves nothing; the operator picks. */
  discoverModels: (id: string) =>
    request<DiscoveredModels>(`/api/providers/${id}/models/discover`, { method: 'POST' }),
  deleteProvider: (id: string) =>
    request<{ deleted: boolean; disabled: boolean; jobs: number }>(`/api/providers/${id}`, {
      method: 'DELETE',
    }),

  teams: () => request<Team[]>('/api/teams'),
  createTeam: (payload: { name: string; roles: TeamRole[] }) =>
    request<Team>('/api/teams', body(payload)),
  setDefaultTeam: (id: number) => request<Team>(`/api/teams/${id}/default`, { method: 'POST' }),

  sandbox: () => request<SandboxInfo>('/api/sandbox'),
}
