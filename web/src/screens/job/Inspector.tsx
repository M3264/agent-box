/**
 * The inspector: the job's structure, alongside the stream rather than instead of it.
 *
 * The stream answers "what happened". These five panels answer the questions a stream
 * is bad at — how far through the plan are we, who is on what model, what has this cost,
 * what files came out, and (for when something is genuinely wrong) the raw event log.
 * They used to be five of the nine tabs, which meant looking at the plan required
 * leaving the conversation.
 *
 * The panel is a route, not local state, so `#/jobs/<id>/tokens` is a link somebody can
 * send. On a wide screen the inspector is a rail and the stream stays visible next to
 * it; on a narrow one the same route swaps the stage over to the panel, with a close
 * button back to the stream. That is one structure at two widths, not two structures.
 *
 * The plan spine is the only panel that reaches back into the stream: clicking a phase
 * filters the stream to it, which is how "what did the tester actually do" is answered
 * in one click instead of by scrolling.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { ApiError, api } from '../../api'
import {
  IconAgents,
  IconClose,
  IconEvents,
  IconFiles,
  IconPlan,
  IconTokens,
} from '../../components/icons'
import { ErrorNote } from '../../components/ui'
import { duration, tone } from '../../lib/format'
import { voiceOf } from '../../lib/voices'
import type { JobEvent, JobSnapshot, Phase } from '../../types'
import { AgentsView } from './AgentsView'
import { ArtifactsView } from './ArtifactsView'
import { Timeline } from './Timeline'
import { UsageView } from './UsageView'

export const PANELS = ['plan', 'agents', 'tokens', 'files', 'events'] as const
export type Panel = (typeof PANELS)[number]

/** The trailing route segment, or '' at the job root. */
export function panelFromPath(pathname: string): { panel: Panel; open: boolean } {
  const tail = pathname.replace(/^\/jobs\/[^/]+\/?/, '').split('/')[0]
  const found = PANELS.find((name) => name === tail)
  return { panel: found ?? 'plan', open: found !== undefined }
}

const TABS: { id: Panel; text: string; icon: () => ReactNode }[] = [
  { id: 'plan', text: 'Plan', icon: IconPlan },
  { id: 'agents', text: 'Agents', icon: IconAgents },
  { id: 'tokens', text: 'Tokens', icon: IconTokens },
  { id: 'files', text: 'Files', icon: IconFiles },
  { id: 'events', text: 'Events', icon: IconEvents },
]

function PhaseSpine({
  plan,
  phase,
  onPhase,
}: {
  plan: Phase[]
  phase: number | null
  onPhase: (id: number | null) => void
}) {
  if (plan.length === 0) {
    return <p className="panel-note">The manager is still planning.</p>
  }

  const rounds = [...new Set(plan.map((row) => row.round || 1))].sort((a, b) => a - b)

  return (
    <div className="spine">
      {rounds.map((round) => (
        <section key={round} className="spine-round">
          {rounds.length > 1 ? <h3 className="spine-round-head">Round {round}</h3> : null}
          <ol className="spine-list">
            {plan
              .filter((row) => (row.round || 1) === round)
              .map((row) => (
                <li key={row.id}>
                  <button
                    type="button"
                    className={`spine-row${phase === row.id ? ' spine-on' : ''}`}
                    style={voiceOf(row.owner)}
                    aria-pressed={phase === row.id}
                    onClick={() => onPhase(phase === row.id ? null : row.id)}
                    title={row.acceptance ?? undefined}
                  >
                    <span className={`spine-mark spine-${tone(row.status)}`} aria-hidden />
                    <span className="spine-seq">{row.seq}</span>
                    <span className="spine-main">
                      <span className="spine-name">{row.name}</span>
                      <span className="spine-meta">
                        <span className="spine-owner">{row.owner}</span>
                        {row.requires_approval ? (
                          <>
                            <span aria-hidden>·</span>
                            <span>gated</span>
                          </>
                        ) : null}
                        {row.attempts > 1 ? (
                          <>
                            <span aria-hidden>·</span>
                            <span>{row.attempts} tries</span>
                          </>
                        ) : null}
                        {row.started_at ? (
                          <>
                            <span aria-hidden>·</span>
                            <span>{duration(row.started_at, row.finished_at)}</span>
                          </>
                        ) : null}
                        {row.total_tokens > 0 ? (
                          <>
                            <span aria-hidden>·</span>
                            <span>{row.total_tokens.toLocaleString()} tok</span>
                          </>
                        ) : null}
                      </span>
                      {row.error ? <span className="spine-error">{row.error}</span> : null}
                    </span>
                    <span className="sr-only">{row.status}</span>
                  </button>
                </li>
              ))}
          </ol>
        </section>
      ))}
      <p className="panel-note">
        Pick a phase to narrow the stream to it. Pick it again to see the whole job.
      </p>
    </div>
  )
}

/**
 * The cap, and the two things you can do to it.
 *
 * A cap that only exists at creation time is a cap you set before you knew anything. The
 * interesting moment is halfway through a job that is burning more than expected — so it
 * is editable here, and 0 means no cap rather than a cap of nothing.
 */
function Budget({ job, onChanged }: { job: JobSnapshot; onChanged: () => void }) {
  const { limit, used, remaining } = job.usage.budget
  const [draft, setDraft] = useState(String(limit || ''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async (value: number) => {
    setBusy(true)
    setError(null)
    try {
      await api.setBudget(job.id, value)
      setDraft(String(value || ''))
      onChanged()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not change the cap')
    } finally {
      setBusy(false)
    }
  }

  const typed = Number(draft.trim())
  const valid = draft.trim() === '' || (Number.isFinite(typed) && typed >= 0)
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0

  return (
    <section className="budget">
      <header className="budget-head">
        <h3>Cap</h3>
        {limit > 0 ? (
          <span className={`pill pill-${pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : 'idle'}`}>
            {pct}% used
          </span>
        ) : (
          <span className="pill pill-muted">no cap</span>
        )}
      </header>

      {limit > 0 ? (
        <>
          <div className="budget-bar" aria-hidden>
            <span className="budget-fill" style={{ width: `${pct}%` }} />
          </div>
          <p className="budget-line">
            {used.toLocaleString()} of {limit.toLocaleString()} tokens
            {remaining !== null ? ` · ${remaining.toLocaleString()} left` : ''}
          </p>
        </>
      ) : (
        <p className="panel-note">
          This job runs until it finishes. A cap stops it at the next provider call and
          leaves it stopped, rather than truncating a phase halfway.
        </p>
      )}

      <div className="budget-edit">
        <label className="sr-only" htmlFor="budget-input">
          Token cap
        </label>
        <input
          id="budget-input"
          type="number"
          min={0}
          step={1000}
          value={draft}
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="no cap"
        />
        <button
          type="button"
          className="button"
          disabled={busy || !valid}
          onClick={() => void save(draft.trim() === '' ? 0 : Math.round(typed))}
        >
          {busy ? 'Saving…' : 'Set'}
        </button>
        {limit > 0 ? (
          <button
            type="button"
            className="button ghost"
            disabled={busy}
            onClick={() => void save(0)}
          >
            Lift it
          </button>
        ) : null}
      </div>
      {!valid ? <p className="field-note error">A cap is a whole number of tokens.</p> : null}
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </section>
  )
}

interface Props {
  jobId: string
  job: JobSnapshot
  events: JobEvent[]
  panel: Panel
  phase: number | null
  onPhase: (id: number | null) => void
  onChanged: () => void
}

export function Inspector({
  jobId,
  job,
  events,
  panel,
  phase,
  onPhase,
  onChanged,
}: Props) {
  const counts: Record<Panel, number> = {
    plan: job.plan.length,
    agents: job.team.length,
    tokens: 0,
    files: job.artifacts.length,
    events: events.length,
  }

  return (
    <aside className="inspector" aria-label="Job inspector">
      <nav className="inspector-tabs">
        {TABS.map(({ id, text, icon: Icon }) => (
          <NavLink
            key={id}
            to={`/jobs/${jobId}/${id}`}
            className={`inspector-tab${panel === id ? ' inspector-tab-on' : ''}`}
          >
            <Icon />
            <span className="inspector-tab-text">{text}</span>
            {counts[id] > 0 ? <span className="inspector-tab-count">{counts[id]}</span> : null}
          </NavLink>
        ))}
        <Link to={`/jobs/${jobId}`} className="inspector-close" aria-label="Back to the stream">
          <IconClose />
        </Link>
      </nav>

      <div className="inspector-body">
        {panel === 'plan' ? <PhaseSpine plan={job.plan} phase={phase} onPhase={onPhase} /> : null}
        {panel === 'agents' ? (
          <AgentsView
            team={job.team}
            plan={job.plan}
            events={events}
            assignments={job.agent_providers}
            usage={job.usage}
          />
        ) : null}
        {panel === 'tokens' ? (
          <>
            <Budget job={job} onChanged={onChanged} />
            <UsageView job={job} />
          </>
        ) : null}
        {panel === 'files' ? (
          <ArtifactsView jobId={jobId} artifacts={job.artifacts} plan={job.plan} />
        ) : null}
        {panel === 'events' ? <Timeline events={events} /> : null}
      </div>
    </aside>
  )
}
