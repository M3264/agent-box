/**
 * The commands tab: every tool call the team made, as a terminal transcript.
 *
 * The list in the job snapshot deliberately carries no output — a job that ran a
 * test suite would otherwise ship megabytes into every snapshot — so a row fetches
 * its own stdout/stderr from `/tool-calls/{id}` the first time it is expanded, and
 * keeps it. That also means the captured text is only requested for commands the
 * operator actually wants to read.
 *
 * A `running` row is not a spinner state invented by the UI: it is what the
 * database holds while the process exists. `interrupted` means a restart caught one
 * mid-flight, which is the honest end of the durability story rather than an error.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../../api'
import { Empty, ErrorNote } from '../../components/ui'
import { fullTime, tone } from '../../lib/format'
import type { Phase, ToolCall, ToolCallStatus } from '../../types'

/** What each terminal status means, in words an operator can act on. */
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
function statusTone(status: ToolCallStatus): string {
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
function commandLine(call: ToolCall): string {
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

interface RowProps {
  jobId: string
  call: ToolCall
  phaseName: string
}

function CommandRow({ jobId, call, phaseName }: RowProps) {
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

  // A row expanded while its command is still running has nothing useful cached, so
  // drop the stale copy when the status moves on and let the next open refetch.
  useEffect(() => {
    if (detail && detail.status !== call.status) setDetail(null)
  }, [call.status, detail])

  const stdout = detail?.stdout ?? ''
  const stderr = detail?.stderr ?? ''
  const nothing = Boolean(detail) && !stdout && !stderr

  return (
    <li className={`command command-${statusTone(call.status)}`}>
      <button
        type="button"
        className="command-head"
        aria-expanded={open}
        onClick={() => {
          const next = !open
          setOpen(next)
          if (next) void load()
        }}
      >
        <span className="command-caret" aria-hidden>
          {open ? '▾' : '▸'}
        </span>
        <code className="command-line">
          <span className="command-prompt" aria-hidden>
            ${' '}
          </span>
          {commandLine(call)}
        </code>
        <span className="command-meta">
          <span className={`pill pill-${statusTone(call.status)}`}>
            <span className="dot" aria-hidden />
            {STATUS_TEXT[call.status] ?? call.status}
          </span>
          {call.exit_code !== null ? (
            <span className="command-exit">exit {call.exit_code}</span>
          ) : null}
          {call.duration_ms !== null ? (
            <span className="command-duration">{millis(call.duration_ms)}</span>
          ) : null}
        </span>
      </button>

      <p className="command-sub">
        <span>{call.agent}</span>
        <span>·</span>
        <span>{phaseName}</span>
        <span>·</span>
        <span>turn {call.turn}</span>
        {call.sandbox ? (
          <>
            <span>·</span>
            <span className={`badge badge-${call.sandbox === 'sandboxed' ? 'good' : 'warn'}`}>
              {call.sandbox}
            </span>
          </>
        ) : null}
        {call.approval_id ? (
          <>
            <span>·</span>
            <span className="badge badge-warn">gated</span>
          </>
        ) : null}
        <span>·</span>
        <span title={fullTime(call.created_at)}>{fullTime(call.created_at)}</span>
      </p>

      {open ? (
        <div className="command-output">
          {loading ? <p className="command-note">Loading output…</p> : null}
          {error ? <ErrorNote>{error}</ErrorNote> : null}
          {call.status === 'pending' && call.approval_id ? (
            <p className="command-note">
              Waiting on an approval. Decide it in the Approvals tab and the agent
              continues from here.
            </p>
          ) : null}
          {call.status === 'running' ? (
            <p className="command-note">Still running — output lands when it exits.</p>
          ) : null}
          {call.status === 'interrupted' ? (
            <p className="command-note">
              The service restarted while this was running. The workspace may already
              reflect it; the phase was told so when it re-ran.
            </p>
          ) : null}
          {call.status === 'refused' ? (
            <p className="command-note">
              Nothing ran and no approval was raised: this job&rsquo;s confinement
              forbids it outright. The reason below is what the agent was told.
            </p>
          ) : null}
          {stdout ? (
            <pre className="command-stream">{stdout}</pre>
          ) : null}
          {stderr ? (
            <pre className="command-stream command-stream-err">{stderr}</pre>
          ) : null}
          {nothing ? <p className="command-note">No output.</p> : null}
          {call.truncated ? (
            <p className="command-note">
              Output was truncated for storage. The full streams are in the workspace at{' '}
              <code>{detail?.stdout_path ?? `.agent-hub/tool-${call.id}.out`}</code>.
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  )
}

interface Props {
  jobId: string
  toolCalls: ToolCall[]
  plan: Phase[]
  sandbox: string | null
}

export function CommandsView({ jobId, toolCalls, plan, sandbox }: Props) {
  const [filter, setFilter] = useState<string>('all')

  const phaseName = useCallback(
    (id: number | null): string => {
      if (id === null) return 'no phase'
      const phase = plan.find((candidate) => candidate.id === id)
      return phase ? `${phase.seq}. ${phase.name}` : `phase ${id}`
    },
    [plan],
  )

  const failures = useMemo(
    () => toolCalls.filter((call) => call.status !== 'ok' && call.status !== 'running').length,
    [toolCalls],
  )
  const visible = useMemo(
    () =>
      filter === 'all'
        ? toolCalls
        : filter === 'failed'
          ? toolCalls.filter((call) => call.status !== 'ok')
          : toolCalls.filter((call) => call.tool === filter),
    [filter, toolCalls],
  )

  if (toolCalls.length === 0) {
    return (
      <Empty
        title="No commands yet"
        hint={
          sandbox
            ? `Specialists run shell commands, read and write files, and fetch URLs — ${sandbox}, in this job's workspace.`
            : 'Tools are disabled on this server, so this job is text-only.'
        }
      />
    )
  }

  const chip = (id: string, text: string, count: number) => (
    <button
      key={id}
      type="button"
      className={`chip ${filter === id ? 'chip-on' : ''}`}
      onClick={() => setFilter(id)}
    >
      {text}
      <span className="chip-count">{count}</span>
    </button>
  )

  return (
    <div className="commands">
      <div className="chips" role="group" aria-label="Filter commands">
        {chip('all', 'all', toolCalls.length)}
        {chip('run', 'shell', toolCalls.filter((call) => call.tool === 'run').length)}
        {chip(
          'write_file',
          'writes',
          toolCalls.filter((call) => call.tool === 'write_file').length,
        )}
        {chip('read_file', 'reads', toolCalls.filter((call) => call.tool === 'read_file').length)}
        {chip('fetch', 'fetches', toolCalls.filter((call) => call.tool === 'fetch').length)}
        {failures > 0 ? chip('failed', 'not ok', failures) : null}
      </div>

      <ol className="command-list">
        {visible.map((call) => (
          <CommandRow
            key={call.id}
            jobId={jobId}
            call={call}
            phaseName={phaseName(call.phase_id)}
          />
        ))}
      </ol>
    </div>
  )
}
