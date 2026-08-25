/**
 * The conversation tab — PLAN.md §1's "lead conversation as the primary workflow".
 *
 * The transcript is built from the event log rather than from the `job_messages`
 * table, because the log is the only place that holds *both* sides: operator
 * messages and everything the team produced. `job_messages` is used for one thing
 * the log cannot answer — whether guidance has been picked up yet (`consumed_at`).
 *
 * The composer is new. v1 had `send_message` on the server and nothing to call it.
 */

import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api } from '../../api'
import { Avatar, Empty, ErrorNote } from '../../components/ui'
import { text, time } from '../../lib/format'
import type { JobEvent, JobMessage } from '../../types'

/** Event kinds that belong in a conversation, as opposed to the full timeline. */
const SPOKEN = new Set([
  'message',
  'result',
  'handoff',
  'guidance',
  'notice',
  'plan',
  'error',
  'approval',
  'tool_call',
])

interface Turn {
  id: number
  at: number
  who: string
  variant: 'agent' | 'operator' | 'result' | 'note' | 'command'
  context: string | null
  body: string
}

function toTurn(event: JobEvent): Turn | null {
  const payload = event.payload
  const who = event.source ?? 'system'

  switch (event.kind) {
    case 'message':
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: who === 'operator' ? 'operator' : 'agent',
        context: typeof payload.phase === 'string' ? payload.phase : null,
        body: text(payload.content),
      }
    case 'result':
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'result',
        context: 'Final result',
        body: text(payload.content),
      }
    case 'handoff':
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: `${text(payload.from)} → ${text(payload.to)} · ${text(payload.reason)}`,
      }
    case 'guidance': {
      const count = Array.isArray(payload.messages) ? payload.messages.length : 0
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: `Operator guidance delivered to the team (${count} message${count === 1 ? '' : 's'})`,
      }
    }
    case 'plan': {
      const count = Array.isArray(payload.phases) ? payload.phases.length : 0
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: `Plan created — ${count} phase${count === 1 ? '' : 's'}. See the Plan tab.`,
      }
    }
    case 'notice':
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: text(payload.message),
      }
    case 'tool_call': {
      // Rendered inline rather than only in the Commands tab: what makes this read
      // like a CLI session is seeing the command between the two things the agent
      // said around it.
      const exit = payload.exit_code
      const suffix =
        payload.status === 'ok'
          ? ''
          : typeof exit === 'number'
            ? ` → exit ${exit}`
            : ` → ${text(payload.status)}`
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'command',
        context: typeof payload.phase === 'string' ? payload.phase : null,
        body: `$ ${text(payload.display)}${suffix}`,
      }
    }
    case 'error':
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: `Error: ${text(payload.error)}`,
      }
    case 'approval': {
      const status = text(payload.status)
      const action = text(payload.action)
      const auto = payload.auto === true ? ' (auto-approved)' : ''
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: action
          ? `Approval ${status}${auto}: ${action}`
          : `Approval ${status}${auto}${payload.note ? ` — ${text(payload.note)}` : ''}`,
      }
    }
    default:
      return null
  }
}

interface Props {
  jobId: string
  events: JobEvent[]
  messages: JobMessage[]
  live: boolean
  onSent: () => void
}

export function Conversation({ jobId, events, messages, live, onSent }: Props) {
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const turns = useMemo(
    () => events.filter((event) => SPOKEN.has(event.kind)).map(toTurn).filter((turn): turn is Turn => turn !== null),
    [events],
  )
  const queued = messages.filter((message) => message.role === 'operator' && message.consumed_at === null)

  const send = async () => {
    const content = draft.trim()
    if (!content || sending) return
    setSending(true)
    setError(null)
    try {
      await api.sendMessage(jobId, content)
      setDraft('')
      onSent()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not send that message')
    } finally {
      setSending(false)
    }
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    void send()
  }

  return (
    <div className="conversation">
      <div className="transcript">
        {turns.length === 0 ? (
          <Empty title="Nothing said yet" hint="The manager speaks first, once planning finishes." />
        ) : (
          turns.map((turn) =>
            turn.variant === 'note' ? (
              <p key={turn.id} className="turn-note">
                <span className="turn-note-time">{time(turn.at)}</span>
                {turn.body}
              </p>
            ) : turn.variant === 'command' ? (
              <p key={turn.id} className="turn-command">
                <span className="turn-note-time">{time(turn.at)}</span>
                <span className="turn-command-who">{turn.who}</span>
                <code>{turn.body}</code>
              </p>
            ) : (
              <article key={turn.id} className={`turn turn-${turn.variant}`}>
                <Avatar agent={turn.who} />
                <div className="turn-body">
                  <header className="turn-head">
                    <span className="turn-who">{turn.who}</span>
                    {turn.context ? <span className="turn-context">{turn.context}</span> : null}
                    <span className="turn-time">{time(turn.at)}</span>
                  </header>
                  <div className="prose">{turn.body}</div>
                </div>
              </article>
            ),
          )
        )}
      </div>

      <form className="composer" onSubmit={onSubmit}>
        {queued.length > 0 ? (
          <p className="composer-note">
            {queued.length} message{queued.length === 1 ? '' : 's'} waiting to be picked up at the
            next phase boundary.
          </p>
        ) : null}
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        <div className="composer-row">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter makes a new line — the usual chat contract.
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void send()
              }
            }}
            placeholder={
              live
                ? 'Send guidance to the team — picked up before the next phase…'
                : 'This job has finished; nothing would read a new message.'
            }
            rows={2}
            disabled={!live}
          />
          <button type="submit" className="button primary" disabled={!live || sending || !draft.trim()}>
            {sending ? 'Sending…' : 'Send'}
          </button>
        </div>
      </form>
    </div>
  )
}
