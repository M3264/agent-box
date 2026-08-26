/**
 * The session stream: one chronological account of everything that happened.
 *
 * The old screen split a single sequence of events across nine tabs. Conversation had
 * what agents said, Commands had what they ran, Approvals had what they asked
 * permission for, Timeline had all of it in a form nobody could read — and the one
 * thing an operator actually wants, *the order it happened in*, was the thing no tab
 * could show. So there are no tabs. There is one stream, and everything lands in it at
 * the moment it happened: phase dividers, agent turns, commands with their output,
 * questions with their buttons, gates with their two verbs.
 *
 * Assembled from state plus the event log, with no new endpoint. The distinction
 * matters and is deliberate:
 *
 * - **Spoken turns come from the event log**, which is the only place holding both
 *   sides of the conversation.
 * - **Commands, questions and gates come from the snapshot rows**, not from their
 *   events, because a row is *current* — a running command shows as running, an
 *   answered question shows its answer — where an event is a photograph of one moment.
 *   Those three event kinds are therefore excluded from the spoken set, or every one
 *   of them would appear twice.
 */

import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { CommandRow } from '../../components/CommandRow'
import { GateCard } from '../../components/GateCard'
import { QuestionCard } from '../../components/QuestionCard'
import { Avatar, Empty } from '../../components/ui'
import { Markdown } from '../../lib/markdown'
import { fullTime, text, time } from '../../lib/format'
import { voiceOf } from '../../lib/voices'
import type { Approval, JobEvent, JobSnapshot, Question, ToolCall } from '../../types'

/**
 * The chip row's cuts. Not a tab strip in disguise: the default is everything, each
 * filter is one keystroke from the others, and no filter hides the phase dividers, so
 * the shape of the job survives every one of them.
 */
export const STREAM_FILTERS = [
  { id: 'all', text: 'everything' },
  { id: 'talk', text: 'said' },
  { id: 'commands', text: 'ran' },
  { id: 'waiting', text: 'decisions' },
  { id: 'notes', text: 'notes' },
] as const

export type StreamFilter = (typeof STREAM_FILTERS)[number]['id']

/** Which chip an item answers to. Dividers answer to all of them. */
type Family = 'talk' | 'commands' | 'waiting' | 'notes'

interface Common {
  key: string
  at: number
  /** Breaks ties at an identical timestamp so a divider never lands mid-phase. */
  rank: number
  phaseId: number | null
  family: Family | null
}

type Item = Common &
  (
    | {
        kind: 'divider'
        seq: number
        name: string
        owner: string
        model: string | null
        round: number
      }
    | { kind: 'round'; round: number }
    | {
        kind: 'say'
        who: string
        variant: 'agent' | 'operator' | 'result'
        context: string | null
        body: string
      }
    | { kind: 'jot'; shade: 'plain' | 'bad'; body: string }
    | { kind: 'command'; call: ToolCall }
    | { kind: 'ask'; question: Question }
    | { kind: 'gate'; approval: Approval }
  )

/** `phase_id` where an event carries one; null is honest rather than guessed. */
function phaseOf(payload: Record<string, unknown>): number | null {
  return typeof payload.phase_id === 'number' ? payload.phase_id : null
}

function build(job: JobSnapshot, events: JobEvent[]): Item[] {
  const items: Item[] = []

  for (const event of events) {
    const payload = event.payload
    const who = event.source ?? 'system'
    const at = event.created_at
    const phaseId = phaseOf(payload)

    switch (event.kind) {
      case 'phase': {
        // Only the transition into a phase. The other three statuses are the same
        // phase reported again, and a divider per status would shred the stream.
        if (payload.status !== 'active') break
        items.push({
          kind: 'divider',
          key: `divider-${event.id}`,
          at,
          rank: 0,
          phaseId,
          family: null,
          seq: typeof payload.seq === 'number' ? payload.seq : 0,
          name: text(payload.name) || 'phase',
          owner: who,
          model: typeof payload.model === 'string' ? payload.model : null,
          round: typeof payload.round === 'number' ? payload.round : 1,
        })
        break
      }
      case 'message':
        items.push({
          kind: 'say',
          key: `say-${event.id}`,
          at,
          rank: 2,
          phaseId,
          family: 'talk',
          who,
          variant: who === 'operator' ? 'operator' : 'agent',
          context: typeof payload.phase === 'string' && payload.partial ? 'thinking' : null,
          body: text(payload.content),
        })
        break
      case 'result':
        // Carries no phase_id, so it is the one item a phase filter always hides.
        items.push({
          kind: 'say',
          key: `result-${event.id}`,
          at,
          rank: 2,
          phaseId: null,
          family: 'talk',
          who,
          variant: 'result',
          context: 'The result',
          body: text(payload.content),
        })
        break
      case 'handoff':
        items.push({
          kind: 'jot',
          key: `jot-${event.id}`,
          at,
          rank: 1,
          phaseId,
          family: 'notes',
          shade: 'plain',
          body: `${text(payload.from)} → ${text(payload.to)} · ${text(payload.reason)}`,
        })
        break
      case 'guidance': {
        const count = Array.isArray(payload.messages) ? payload.messages.length : 0
        items.push({
          kind: 'jot',
          key: `jot-${event.id}`,
          at,
          rank: 1,
          phaseId,
          family: 'notes',
          shade: 'plain',
          body: `Your ${count === 1 ? 'message was' : `${count} messages were`} handed to the team`,
        })
        break
      }
      case 'plan': {
        const count = Array.isArray(payload.phases) ? payload.phases.length : 0
        items.push({
          kind: 'jot',
          key: `jot-${event.id}`,
          at,
          rank: 1,
          phaseId,
          family: 'notes',
          shade: 'plain',
          body: `Planned ${count} phase${count === 1 ? '' : 's'}`,
        })
        break
      }
      case 'notice': {
        // A continuation notice carries the operator's own instruction, and nothing
        // else records it as a `message`. Flattening it to a note would drop the words
        // the operator typed out of their own transcript.
        const round = typeof payload.round === 'number' ? payload.round : undefined
        const instruction = text(payload.instruction)
        if (round !== undefined && instruction) {
          items.push({
            kind: 'round',
            key: `round-${event.id}`,
            at,
            rank: 0,
            phaseId,
            family: null,
            round,
          })
          items.push({
            kind: 'say',
            key: `say-${event.id}`,
            at,
            rank: 2,
            phaseId,
            family: 'talk',
            who: 'operator',
            variant: 'operator',
            context: `Round ${round}`,
            body: instruction,
          })
          break
        }
        items.push({
          kind: 'jot',
          key: `jot-${event.id}`,
          at,
          rank: 1,
          phaseId,
          family: 'notes',
          shade: payload.budget === true ? 'bad' : 'plain',
          body: text(payload.message),
        })
        break
      }
      case 'error':
        items.push({
          kind: 'jot',
          key: `jot-${event.id}`,
          at,
          rank: 1,
          phaseId,
          family: 'notes',
          shade: 'bad',
          body: text(payload.error),
        })
        break
      default:
        // `tool_call`, `question` and `approval` events are covered by the rows below;
        // `status`, `agent_state` and `artifact` belong to the inspector, not here.
        break
    }
  }

  for (const call of job.tool_calls) {
    items.push({
      kind: 'command',
      key: `cmd-${call.id}`,
      at: call.created_at,
      rank: 3,
      phaseId: call.phase_id,
      family: 'commands',
      call,
    })
  }
  for (const question of job.questions) {
    items.push({
      kind: 'ask',
      key: `ask-${question.id}`,
      at: question.created_at,
      rank: 4,
      phaseId: question.phase_id,
      family: 'waiting',
      question,
    })
  }
  for (const approval of job.approvals) {
    items.push({
      kind: 'gate',
      key: `gate-${approval.id}`,
      at: approval.created_at,
      rank: 4,
      phaseId: approval.phase_id,
      family: 'waiting',
      approval,
    })
  }

  items.sort((left, right) => left.at - right.at || left.rank - right.rank)
  return items
}

interface Props {
  jobId: string
  job: JobSnapshot
  events: JobEvent[]
  filter: StreamFilter
  onFilter: (filter: StreamFilter) => void
  /** A phase id, set by clicking the plan spine. Null is the whole job. */
  phase: number | null
  onPhase: (phase: number | null) => void
  /** Something was answered or decided, so the caller can refresh the snapshot. */
  onChanged: () => void
}

export function SessionStream({
  jobId,
  job,
  events,
  filter,
  onFilter,
  phase,
  onPhase,
  onChanged,
}: Props) {
  const scroller = useRef<HTMLDivElement>(null)
  /** Whether the operator is reading the tail, which is when following it is wanted. */
  const pinned = useRef(true)

  const items = useMemo(() => build(job, events), [job, events])
  const counts = useMemo(() => {
    const tally: Record<Family, number> = { talk: 0, commands: 0, waiting: 0, notes: 0 }
    for (const item of items) if (item.family) tally[item.family] += 1
    return tally
  }, [items])

  const named = phase === null ? null : (job.plan.find((row) => row.id === phase) ?? null)
  const visible = useMemo(
    () =>
      items.filter((item) => {
        if (phase !== null && item.phaseId !== phase) return false
        if (filter !== 'all' && item.family !== null && item.family !== filter) return false
        return true
      }),
    [items, filter, phase],
  )

  // Follow the tail as the team works, but never yank the view away from someone who
  // has scrolled up to read an earlier phase.
  useLayoutEffect(() => {
    const node = scroller.current
    if (node && pinned.current) node.scrollTop = node.scrollHeight
  }, [visible.length])

  // Narrowing the stream is a deliberate act of reading, so it starts at the top of
  // what was asked for rather than at the bottom of it.
  useLayoutEffect(() => {
    const node = scroller.current
    if (!node) return
    const narrowed = phase !== null || filter !== 'all'
    pinned.current = !narrowed
    node.scrollTop = narrowed ? 0 : node.scrollHeight
  }, [phase, filter])

  useEffect(() => {
    const node = scroller.current
    if (!node) return
    const onScroll = () => {
      pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120
    }
    node.addEventListener('scroll', onScroll, { passive: true })
    return () => node.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <div className="stream-wrap">
      <div className="stream-chips" role="group" aria-label="Filter the stream">
        {STREAM_FILTERS.map((chip) => (
          <button
            key={chip.id}
            type="button"
            className={`chip${filter === chip.id ? ' chip-on' : ''}`}
            aria-pressed={filter === chip.id}
            onClick={() => onFilter(chip.id)}
          >
            {chip.text}
            {chip.id === 'all' ? null : <span className="chip-count">{counts[chip.id]}</span>}
          </button>
        ))}
        {named ? (
          <button type="button" className="chip chip-phase" onClick={() => onPhase(null)}>
            {named.seq}. {named.name}
            <span className="chip-x" aria-hidden>
              ×
            </span>
            <span className="sr-only">Show every phase</span>
          </button>
        ) : null}
      </div>

      <div className="stream" ref={scroller}>
        {visible.length === 0 ? (
          <Empty
            title={
              items.length === 0
                ? 'Nothing has happened yet'
                : 'Nothing in the stream matches that'
            }
            hint={
              items.length === 0
                ? 'The manager plans first; everything after that lands here as it happens.'
                : 'The result and anything recorded outside a phase are hidden while a phase is picked.'
            }
          />
        ) : (
          visible.map((item) => {
            switch (item.kind) {
              case 'divider':
                return (
                  <div key={item.key} className="mark" style={voiceOf(item.owner)}>
                    <span className="mark-seq">{item.seq}</span>
                    <div className="mark-main">
                      <p className="mark-name">{item.name}</p>
                      <p className="mark-meta">
                        <span className="mark-owner">{item.owner}</span>
                        {item.model ? (
                          <>
                            <span aria-hidden>·</span>
                            <span className="mono">{item.model}</span>
                          </>
                        ) : null}
                      </p>
                    </div>
                    <span className="mark-time" title={fullTime(item.at)}>
                      {time(item.at)}
                    </span>
                  </div>
                )
              case 'round':
                return (
                  <p key={item.key} className="round-mark">
                    <span>Round {item.round}</span>
                  </p>
                )
              case 'jot':
                return (
                  <p key={item.key} className={`jot jot-${item.shade}`}>
                    <span className="jot-time">{time(item.at)}</span>
                    {item.body}
                  </p>
                )
              case 'command':
                return <CommandRow key={item.key} jobId={jobId} call={item.call} />
              case 'ask':
                return (
                  <QuestionCard
                    key={item.key}
                    jobId={jobId}
                    question={item.question}
                    onAnswered={onChanged}
                  />
                )
              case 'gate':
                return (
                  <GateCard
                    key={item.key}
                    jobId={jobId}
                    approval={item.approval}
                    onDecided={onChanged}
                  />
                )
              case 'say':
                return (
                  <article
                    key={item.key}
                    className={`say say-${item.variant}`}
                    style={voiceOf(item.who)}
                  >
                    <Avatar agent={item.who} />
                    <div className="say-body">
                      <header className="say-head">
                        <span className="say-who">{item.who}</span>
                        {item.context ? (
                          <span className="say-context">{item.context}</span>
                        ) : null}
                        <span className="say-time" title={fullTime(item.at)}>
                          {time(item.at)}
                        </span>
                      </header>
                      <Markdown source={item.body} className="say-prose" />
                    </div>
                  </article>
                )
            }
          })
        )}
      </div>
    </div>
  )
}
