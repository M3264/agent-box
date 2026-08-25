/** Formatting helpers. Timestamps from the API are float seconds, not milliseconds. */

const RELATIVE: [limit: number, divisor: number, unit: Intl.RelativeTimeFormatUnit][] = [
  [60, 1, 'second'],
  [3600, 60, 'minute'],
  [86_400, 3600, 'hour'],
  [604_800, 86_400, 'day'],
  [2_592_000, 604_800, 'week'],
  [31_536_000, 2_592_000, 'month'],
]

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
const clock = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
const stamp = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function since(seconds: number | null | undefined): string {
  if (!seconds) return '—'
  const delta = seconds - Date.now() / 1000
  for (const [limit, divisor, unit] of RELATIVE) {
    if (Math.abs(delta) < limit) return relative.format(Math.round(delta / divisor), unit)
  }
  return relative.format(Math.round(delta / 31_536_000), 'year')
}

export function time(seconds: number | null | undefined): string {
  return seconds ? clock.format(new Date(seconds * 1000)) : '—'
}

export function fullTime(seconds: number | null | undefined): string {
  return seconds ? stamp.format(new Date(seconds * 1000)) : '—'
}

export function duration(from: number | null, to: number | null): string {
  if (!from) return '—'
  const end = to ?? Date.now() / 1000
  const total = Math.max(0, Math.round(end - from))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes}m ${total % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export function bytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

/** Human label for a machine status, e.g. `blocked_on_approval` → `Blocked`. */
export function label(status: string): string {
  const LABELS: Record<string, string> = {
    blocked_on_approval: 'Needs approval',
    agent_state: 'Agent',
    pending: 'Pending',
    active: 'Active',
    complete: 'Complete',
    failed: 'Failed',
    skipped: 'Skipped',
    queued: 'Queued',
    planning: 'Planning',
    running: 'Running',
    blocked: 'Blocked',
    error: 'Error',
    stopped: 'Stopped',
    approved: 'Approved',
    rejected: 'Rejected',
    waiting: 'Waiting',
  }
  return LABELS[status] ?? status.replace(/_/g, ' ')
}

/** Maps any status to one of six visual tones defined in styles.css. */
export function tone(status: string): 'idle' | 'busy' | 'good' | 'warn' | 'bad' | 'muted' {
  switch (status) {
    case 'complete':
    case 'approved':
      return 'good'
    case 'active':
    case 'running':
    case 'planning':
      return 'busy'
    case 'blocked':
    case 'blocked_on_approval':
    case 'pending':
    case 'waiting':
      return 'warn'
    case 'error':
    case 'failed':
    case 'rejected':
      return 'bad'
    case 'stopped':
    case 'skipped':
      return 'muted'
    default:
      return 'idle'
  }
}

export function initials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}

/** A payload field as a display string, for event payloads typed as unknown. */
export function text(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value, null, 2)
}
