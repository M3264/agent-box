/**
 * Cmd+K: everything reachable without leaving the keyboard.
 *
 * The rail has three destinations and a job screen has a dozen actions, and the ones
 * an operator wants in a hurry — stop that job, jump to the job that is asking a
 * question — are the ones furthest from the pointer. So this is a flat list: the
 * destinations, whatever applies to the job currently open, and every recent job by
 * task. No categories, no fuzzy matching cleverness; a substring match over the label
 * and its hint, in a fixed order, so the same keystrokes always select the same thing.
 *
 * Recent jobs are fetched when the palette opens rather than kept in sync, because a
 * list that is four seconds stale is fine for navigation and a permanent poll is not.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api'
import { IconSearch } from './icons'
import { useAttention } from '../hooks/useAttention'
import { label as statusLabel, tone } from '../lib/format'
import type { JobSummary } from '../types'

interface Action {
  id: string
  label: string
  hint: string
  /** The six tones, for the dot beside a job. Undefined for a plain command. */
  tone?: string
  run: () => void | Promise<void>
}

/** The job id from the current route, or null. The palette's actions depend on it. */
function jobFromPath(pathname: string): string | null {
  const match = /^\/jobs\/([^/]+)/.exec(pathname)
  return match ? match[1] : null
}

export function CommandPalette({
  open,
  onClose,
  onNewJob,
}: {
  open: boolean
  onClose: () => void
  onNewJob: () => void
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const attention = useAttention()
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement | null>(null)
  const jobId = jobFromPath(location.pathname)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setCursor(0)
    setError(null)
    input.current?.focus()
    let alive = true
    void api
      .jobs(30)
      .then((rows) => {
        if (alive) setJobs(rows)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [open])

  const act = useCallback(
    (work: () => Promise<unknown>) => async () => {
      try {
        await work()
        onClose()
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : 'that did not work')
      }
    },
    [onClose],
  )

  const go = useCallback(
    (to: string) => () => {
      navigate(to)
      onClose()
    },
    [navigate, onClose],
  )

  const actions = useMemo<Action[]>(() => {
    const list: Action[] = [
      {
        id: 'new',
        label: 'New job',
        hint: 'start something',
        run: () => {
          onNewJob()
          onClose()
        },
      },
      { id: 'jobs', label: 'Jobs', hint: 'all jobs', run: go('/jobs') },
      {
        id: 'attention',
        label: 'Attention',
        hint:
          attention.counts.blocking > 0
            ? `${attention.counts.blocking} waiting on you`
            : 'nothing waiting',
        run: go('/attention'),
      },
      { id: 'settings', label: 'Settings', hint: 'providers and teams', run: go('/settings') },
    ]

    if (jobId) {
      list.push(
        { id: 'plan', label: 'Show the plan', hint: 'this job', run: go(`/jobs/${jobId}/plan`) },
        { id: 'tokens', label: 'Show tokens and cost', hint: 'this job', run: go(`/jobs/${jobId}/tokens`) },
        { id: 'pause', label: 'Pause this job', hint: 'after the current phase', run: act(() => api.pause(jobId)) },
        { id: 'resume', label: 'Resume this job', hint: 'carry on', run: act(() => api.resume(jobId)) },
        { id: 'stop', label: 'Stop this job', hint: 'ends it for good', run: act(() => api.stop(jobId)) },
      )
    }

    for (const job of jobs) {
      list.push({
        id: `job-${job.id}`,
        label: job.task,
        hint: `${statusLabel(job.status)} · ${job.id.slice(0, 8)}`,
        tone: tone(job.status),
        run: go(`/jobs/${job.id}`),
      })
    }
    return list
  }, [act, attention.counts.blocking, go, jobId, jobs, onClose, onNewJob])

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return actions
    return actions.filter((action) =>
      `${action.label} ${action.hint}`.toLowerCase().includes(needle),
    )
  }, [actions, query])

  if (!open) return null

  const chosen = Math.min(cursor, Math.max(0, matches.length - 1))

  const onKey = (event: ReactKeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setCursor((current) => (matches.length === 0 ? 0 : (current + 1) % matches.length))
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setCursor((current) =>
        matches.length === 0 ? 0 : (current - 1 + matches.length) % matches.length,
      )
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      void matches[chosen]?.run()
    }
  }

  return (
    <div className="palette-veil" role="presentation" onMouseDown={onClose}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="palette-field">
          <IconSearch />
          <input
            ref={input}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setCursor(0)
            }}
            onKeyDown={onKey}
            placeholder="Jump to a job, or run a command"
            aria-label="Search commands and jobs"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd>esc</kbd>
        </div>

        {error ? <p className="palette-error">{error}</p> : null}

        {matches.length === 0 ? (
          <p className="palette-none">Nothing matches “{query}”.</p>
        ) : (
          <ul className="palette-list">
            {matches.map((action, index) => (
              <li key={action.id}>
                <button
                  type="button"
                  className={`palette-item${index === chosen ? ' on' : ''}`}
                  onMouseEnter={() => setCursor(index)}
                  onClick={() => void action.run()}
                >
                  {action.tone ? <span className={`dot dot-${action.tone}`} aria-hidden /> : null}
                  <span className="palette-label">{action.label}</span>
                  <span className="palette-hint">{action.hint}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
