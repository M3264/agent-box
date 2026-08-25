/**
 * HTTP and WebSocket access.
 *
 * The mount prefix is derived at runtime rather than baked in at build time: the
 * same bundle is served at `/` and at `/hub/`, and the `/hub/` nginx location
 * strips the prefix before proxying, so the server sees `/`-rooted paths either
 * way. Everything client-side has to put the prefix back.
 */

import type {
  Approval,
  ApprovalStatus,
  Health,
  InboxApproval,
  JobAction,
  JobMessage,
  JobSnapshot,
  JobSummary,
  Mode,
  Phase,
  Provider,
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
  }) => request<{ id: string }>('/api/jobs', body(payload)),

  plan: (id: string) => request<Phase[]>(`/api/jobs/${id}/plan`),
  messages: (id: string) => request<JobMessage[]>(`/api/jobs/${id}/messages`),
  sendMessage: (id: string, content: string) =>
    request<JobMessage>(`/api/jobs/${id}/messages`, body({ content })),

  toolCalls: (id: string, phaseId?: number) =>
    request<ToolCall[]>(
      `/api/jobs/${id}/tool-calls${phaseId ? `?phase_id=${phaseId}` : ''}`,
    ),
  toolCall: (id: string, callId: string) => request<ToolCall>(`/api/jobs/${id}/tool-calls/${callId}`),

  pause: (id: string) => request<JobAction>(`/api/jobs/${id}/pause`, { method: 'POST' }),
  resume: (id: string) => request<JobAction>(`/api/jobs/${id}/resume`, { method: 'POST' }),
  stop: (id: string) => request<JobAction>(`/api/jobs/${id}/stop`, { method: 'POST' }),

  inbox: (status: ApprovalStatus | 'all' = 'pending') =>
    request<InboxApproval[]>(`/api/approvals?status=${status}`),
  decide: (jobId: string, approvalId: string, decision: 'approved' | 'rejected', note?: string) =>
    request<Approval>(
      `/api/jobs/${jobId}/approvals/${approvalId}`,
      body({ decision, note: note || null }),
    ),

  providers: () => request<Provider[]>('/api/providers'),
  saveProvider: (profile: Omit<Provider, 'secret_ok' | 'created_at'>) =>
    request<Provider>(`/api/providers/${profile.id}`, {
      method: 'PUT',
      body: JSON.stringify(profile),
    }),
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
