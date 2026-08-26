/**
 * One command the team ran, as a foldable line in the session stream.
 *
 * This used to be a row on a Commands tab, which put every shell command in a filing
 * cabinet next to the conversation instead of inside it. A CLI session does not have a
 * commands tab: the command sits between the sentence that led to it and the sentence
 * that followed, and that ordering is most of the meaning.
 *
 * The snapshot deliberately carries no output — a job that ran a test suite would
 * otherwise ship megabytes into every poll — so a row fetches its own stdout/stderr
 * from `/tool-calls/{id}` the first time it is opened, and keeps it. The captured text
 * is only requested for commands somebody actually wants to read.
 *
 * A `running` row is not a spinner state invented by the UI: it is what the database
 * holds while the process exists. `interrupted` means a restart caught one mid-flight,
 * which is the honest end of the durability story rather than an error.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError, api } from '../api'
import { IconChevron } from './icons'
import { ErrorNote } from './ui'
import { fullTime, time, tone } from '../lib/format'
import { voiceOf } from '../lib/voices'
import type { ToolCall, ToolCallStatus } from '../types'

/** What each status means, in words an operator can act on. */
const STATUS_TEXT: Record<ToolCallStatus, string> = {
  pending: 'waiting',
  running: 'running',
  ok: 'ok',
  error: 'failed',
  denied: 'declined',
  // Distinct from `denied` on purpose: nobody was asked. The job's own confinement
  // forbids it, so reading this as "someone said no" would send the operator looking
  // for a decision that was never made. Why is on the row, in the reason line.
  refused: 'refused',
  timeout: 'timed out',
  interrupted: 'interrupted by a restart',
  capped: 'output cap hit',
  cancelled: 'never ran',
}

/** Reuses the six shared tones rather than inventing a seventh palette. */
export function commandTone(status: ToolCallStatus): string {
  switch (status) {
    case 'ok':
      return 'good'
    case 'running':
      return 'busy'
    case 'pending':
      return 'warn'
    // Worth seeing rather than muting: an agent reaching for a masked path is not a
    // failure, but it is the kind of thing an operator should notice happened.
    case 'refused':
      return 'warn'
    case 'error':
    case 'timeout':
      return 'bad'
    case 'denied':
    case 'interrupted':
    case 'cancelled':
      return 'muted'
    default:
      return tone(status)
  }
}

function millis(value: number | null): string {
  if (value === null) return ''
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`
}

/** The `$ …` line: what the agent asked for, whatever tool it used. */
export function commandLine(call: ToolCall): string {
  const args = call.args as Record<string, unknown>
  const str = (key: string): string => (typeof args[key] === 'string' ? (args[key] as string) : '')
  switch (call.tool) {
    case 'run':
      return str('command') || '(empty command)'
    case 'read_file':
      return `read ${str('path') || '(no path)'}`
    case 'write_file':
      return `write ${str('path') || '(no path)'}`
    case 'fetch':
      return `${(str('method') || 'GET').toUpperCase()} ${str('url') || '(no url)'}`
    default:
      return `${call.tool} ${JSON.stringify(args).slice(0, 200)}`
  }
}

export function CommandRow({
  jobId,
  call,
  /** Given only where the phase is not already implied by a divider above. */
  phaseName,
}: {
  jobId: string
  call: ToolCall
  phaseName?: string | null
}) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ToolCall | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (detail || loading) return
    setLoading(true)
    setError(null)
    try {
      setDetail(await api.toolCall(jobId, call.id))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not load this output')
    } finally {
      setLoading(false)
    }
  }, [call.id, detail, jobId, loading])

  // A row opened while its command is still running has nothing useful cached, so drop
  // the stale copy when the status moves on and let the next open refetch.
  useEffect(() => {
    if (detail && detail.status !== call.status) setDetail(null)
  }, [call.status, detail])

  const stdout = detail?.stdout ?? ''
  const stderr = detail?.stderr ?? ''
  const nothing = Boolean(detail) && !stdout && !stderr
  const shade = commandTone(call.status)

  return (
    <div className={`cmd cmd-${shade}${open ? ' cmd-open' : ''}`} style={voiceOf(call.agent)}>
      <button
        type="button"
        className="cmd-head"
        aria-expanded={open}
        onClick={() => {
          const next = !open
          setOpen(next)
          if (next) void load()
        }}
      >
        <span className="cmd-caret" aria-hidden>
          <IconChevron />
        </span>
        <code className="cmd-line">
          <span className="cmd-prompt" aria-hidden>
            ${' '}
          </span>
          {commandLine(call)}
        </code>
        <span className="cmd-meta">
          {call.exit_code !== null ? <span className="cmd-exit">exit {call.exit_code}</span> : null}
          {call.duration_ms !== null ? (
            <span className="cmd-duration">{millis(call.duration_ms)}</span>
          ) : null}
          <span className={`pill pill-${shade}`}>
            <span className="dot" aria-hidden />
            {STATUS_TEXT[call.status] ?? call.status}
          </span>
        </span>
      </button>

      <p className="cmd-sub">
        <span className="cmd-agent">{call.agent}</span>
        {phaseName ? (
          <>
            <span aria-hidden>·</span>
            <span>{phaseName}</span>
          </>
        ) : null}
        <span aria-hidden>·</span>
        <span>turn {call.turn}</span>
        {call.sandbox ? (
          <>
            <span aria-hidden>·</span>
            <span className={`badge badge-${call.sandbox === 'sandboxed' ? 'good' : 'warn'}`}>
              {call.sandbox}
            </span>
          </>
        ) : null}
        {call.approval_id ? (
          <>
            <span aria-hidden>·</span>
            <span className="badge badge-warn">gated</span>
          </>
        ) : null}
        <span aria-hidden>·</span>
        <span title={fullTime(call.created_at)}>{time(call.created_at)}</span>
      </p>

      {open ? (
        <div className="cmd-output">
          {loading ? <p className="cmd-note">Loading output…</p> : null}
          {error ? <ErrorNote>{error}</ErrorNote> : null}
          {call.status === 'pending' && call.approval_id ? (
            <p className="cmd-note">
              Waiting on a decision. The gate is in this stream, just below — approve it
              and the agent carries on from here.
            </p>
          ) : null}
          {call.status === 'running' ? (
            <p className="cmd-note">Still running — output lands when it exits.</p>
          ) : null}
          {call.status === 'interrupted' ? (
            <p className="cmd-note">
              The service restarted while this was running. The workspace may already
              reflect it; the phase was told so when it re-ran.
            </p>
          ) : null}
          {call.status === 'refused' ? (
            <p className="cmd-note">
              Nothing ran and no approval was raised: this job&rsquo;s confinement forbids
              it outright. The reason below is what the agent was told.
            </p>
          ) : null}
          {stdout ? <pre className="cmd-stream">{stdout}</pre> : null}
          {stderr ? <pre className="cmd-stream cmd-stream-err">{stderr}</pre> : null}
          {nothing ? <p className="cmd-note">No output.</p> : null}
          {call.truncated ? (
            <p className="cmd-note">
              Output was truncated for storage. The full streams are in the workspace at{' '}
              <code>{detail?.stdout_path ?? `.agent-hub/tool-${call.id}.out`}</code>.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
