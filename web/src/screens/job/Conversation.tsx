/**
 * The conversation tab — PLAN.md §1's "lead conversation as the primary workflow".
 *
 * The transcript is built from the event log rather than from the `job_messages`
 * table, because the log is the only place that holds *both* sides: operator
 * messages and everything the team produced. `job_messages` is used for one thing
 * the log cannot answer — whether guidance has been picked up yet (`consumed_at`).
 *
 * Two things this screen refuses to do:
 *
 * - **Print markdown verbatim.** Agents write headings, `**Severity:**`, bullet
 *   lists and fenced code, so the transcript renders through `Markdown` instead of
 *   dropping the string into a `<div>`.
 * - **Close when the job ends.** A finished job is not a finished conversation. The
 *   composer stays open and switches from "send guidance" (picked up at the next
 *   phase boundary) to "continue" (a new round of work on the same job).
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api } from '../../api'
import { Avatar, Empty, ErrorNote } from '../../components/ui'
import { Markdown } from '../../lib/markdown'
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
  /** Set on the turn that opens a new round, which draws a labelled divider. */
  round?: number
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
    case 'notice': {
      // A continuation notice carries the operator's own instruction, and nothing else
      // records it as a `message` event. Rendering it as a note would drop the words
      // the operator actually typed out of their own transcript.
      const round = typeof payload.round === 'number' ? payload.round : undefined
      const instruction = text(payload.instruction)
      if (round !== undefined && instruction) {
        return {
          id: event.id,
          at: event.created_at,
          who: 'operator',
          variant: 'operator',
          context: `Round ${round}`,
          body: instruction,
          round,
        }
      }
      return {
        id: event.id,
        at: event.created_at,
        who,
        variant: 'note',
        context: null,
        body: text(payload.message),
      }
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
  /** True on a terminal job: another round can be added to it. */
  canContinue: boolean
  onSent: () => void
  /** A round was started, so the caller can reopen a stream it had let close. */
  onContinued: () => void
  /** Open New Job prefilled from this one. */
  onRerun: () => void
}

export function Conversation({
  jobId,
  events,
  messages,
  live,
  canContinue,
  onSent,
  onContinued,
  onRerun,
}: Props) {
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  /** Whether the operator is reading the bottom, which is when auto-scroll is wanted. */
  const pinned = useRef(true)

  const turns = useMemo(
    () =>
      events
        .filter((event) => SPOKEN.has(event.kind))
        .map(toTurn)
        .filter((turn): turn is Turn => turn !== null),
    [events],
  )
  const queued = messages.filter(
    (message) => message.role === 'operator' && message.consumed_at === null,
  )

  // Follow the tail as the team works, but never yank the view away from someone who
  // has scrolled up to read an earlier phase.
  useLayoutEffect(() => {
    const node = scroller.current
    if (node && pinned.current) node.scrollTop = node.scrollHeight
  }, [turns.length])

  useEffect(() => {
    const node = scroller.current
    if (!node) return
    const onScroll = () => {
      pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120
    }
    node.addEventListener('scroll', onScroll, { passive: true })
    return () => node.removeEventListener('scroll', onScroll)
  }, [])

  const send = async () => {
    const content = draft.trim()
    if (!content || sending) return
    setSending(true)
    setError(null)
    try {
      if (live) {
        await api.sendMessage(jobId, content)
        setDraft('')
        onSent()
      } else {
        await api.continueJob(jobId, content)
        setDraft('')
        pinned.current = true
        onContinued()
      }
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : live
            ? 'could not send that message'
            : 'could not continue this job',
      )
    } finally {
      setSending(false)
    }
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    void send()
  }

  const canSubmit = live || canContinue
  const action = live ? 'Send' : 'Continue'

  return (
    <div className="conversation">
      <div className="transcript" ref={scroller}>
        {turns.length === 0 ? (
          <Empty title="Nothing said yet" hint="The manager speaks first, once planning finishes." />
        ) : (
          turns.map((turn) => (
            <div key={turn.id} className="turn-slot">
              {turn.round !== undefined ? (
                <p className="round-divider">
                  <span>Round {turn.round}</span>
                </p>
              ) : null}
              {turn.variant === 'note' ? (
                <p className="turn-note">
                  <span className="turn-note-time">{time(turn.at)}</span>
                  {turn.body}
                </p>
              ) : turn.variant === 'command' ? (
                <p className="turn-command">
                  <span className="turn-note-time">{time(turn.at)}</span>
                  <span className="turn-command-who">{turn.who}</span>
                  <code>{turn.body}</code>
                </p>
              ) : (
                <article className={`turn turn-${turn.variant}`}>
                  <Avatar agent={turn.who} />
                  <div className="turn-body">
                    <header className="turn-head">
                      <span className="turn-who">{turn.who}</span>
                      {turn.context ? <span className="turn-context">{turn.context}</span> : null}
                      <span className="turn-time">{time(turn.at)}</span>
                    </header>
                    <Markdown source={turn.body} className="turn-prose" />
                  </div>
                </article>
              )}
            </div>
          ))
        )}
      </div>

      <form className="composer" onSubmit={onSubmit}>
        {queued.length > 0 ? (
          <p className="composer-note">
            {queued.length} message{queued.length === 1 ? '' : 's'} waiting to be picked up at the
            next phase boundary.
          </p>
        ) : null}
        {!live && canContinue ? (
          <p className="composer-note">
            This job has finished. Say what should happen next and the team picks up where it
            left off — earlier rounds stay exactly as they are.
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
                : canContinue
                  ? 'Now also check the login flow…'
                  : 'This job is winding down; try again in a moment.'
            }
            rows={2}
            disabled={!canSubmit}
          />
          <div className="composer-actions">
            <button
              type="submit"
              className="button primary"
              disabled={!canSubmit || sending || !draft.trim()}
            >
              {sending ? (live ? 'Sending…' : 'Starting…') : action}
            </button>
            <button type="button" className="button ghost" onClick={onRerun}>
              Run again
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}
