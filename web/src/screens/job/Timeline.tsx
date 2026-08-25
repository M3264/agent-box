/** The timeline tab: every event, in order, with the raw payload one click away. */

import { useMemo, useState } from 'react'
import { Empty } from '../../components/ui'
import { label, time, tone } from '../../lib/format'
import type { JobEvent } from '../../types'

/** A one-line gist per kind, so the list is readable without expanding rows. */
function gist(event: JobEvent): string {
  const payload = event.payload
  const value = (key: string): string => {
    const raw = payload[key]
    return typeof raw === 'string' ? raw : raw === undefined || raw === null ? '' : String(raw)
  }

  switch (event.kind) {
    case 'status':
      return value('status')
    case 'phase':
      return `${value('seq')}. ${value('name')} → ${value('status')}`
    case 'agent_state':
      return value('current_action') || value('status')
    case 'message':
      return value('content').slice(0, 160)
    case 'result':
      return value('content').slice(0, 160)
    case 'handoff':
      return `${value('from')} → ${value('to')}`
    case 'approval':
      return `${value('status')}${value('action') ? ` · ${value('action')}` : ''}`
    case 'artifact':
      return value('name')
    case 'plan':
      return `${Array.isArray(payload.phases) ? payload.phases.length : 0} phases`
    case 'guidance':
      return `${Array.isArray(payload.messages) ? payload.messages.length : 0} operator messages`
    case 'tool_call':
      return `${value('display')} → ${value('status')}`
    case 'notice':
      return value('message')
    case 'error':
      return value('error')
    default:
      return ''
  }
}

export function Timeline({ events }: { events: JobEvent[] }) {
  const [kind, setKind] = useState<string>('all')

  const kinds = useMemo(
    () => Array.from(new Set(events.map((event) => event.kind))).sort(),
    [events],
  )
  const visible = kind === 'all' ? events : events.filter((event) => event.kind === kind)

  if (events.length === 0) return <Empty title="No events yet" />

  return (
    <div className="timeline">
      <div className="chips" role="group" aria-label="Filter by event kind">
        <button
          type="button"
          className={`chip ${kind === 'all' ? 'chip-on' : ''}`}
          onClick={() => setKind('all')}
        >
          all <span className="chip-count">{events.length}</span>
        </button>
        {kinds.map((name) => (
          <button
            key={name}
            type="button"
            className={`chip ${kind === name ? 'chip-on' : ''}`}
            onClick={() => setKind(name)}
          >
            {label(name).toLowerCase()}
            <span className="chip-count">{events.filter((event) => event.kind === name).length}</span>
          </button>
        ))}
      </div>

      <ol className="events">
        {visible.map((event) => (
          <li key={event.id} className={`event event-${tone(String(event.payload.status ?? event.kind))}`}>
            <span className="event-time" title={String(event.id)}>
              {time(event.created_at)}
            </span>
            <span className="event-kind">{event.kind}</span>
            <span className="event-source">{event.source ?? '—'}</span>
            <details className="event-detail">
              <summary>{gist(event) || 'view payload'}</summary>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
    </div>
  )
}
