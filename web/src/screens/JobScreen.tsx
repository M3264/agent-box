/**
 * The session console: one job, one stream, one inspector, one composer.
 *
 * What this replaced was a nine-tab strip over a job — Conversation, Result, Timeline,
 * Plan, Agents, Commands, Artifacts, Approvals, Usage — which took a single sequence of
 * events and scattered it across nine places, none of which was where the work was
 * happening. Reading a job meant reconstructing its order in your head from four tabs.
 *
 * The structure now says what the thing is. A job is a session:
 *
 * - **The head** is small and permanent: what the job is, how it is going, and the four
 *   verbs that change that.
 * - **The stream** is everything that happened, in order, with nothing hidden behind a
 *   tab. Chips narrow it; the plan spine narrows it to one phase.
 * - **The inspector** holds the five questions a stream answers badly — plan, agents,
 *   tokens, files, events — beside the stream rather than instead of it.
 * - **The composer** is docked. It is never somewhere you have to navigate back to.
 *
 * One `useJobStream` feeds all of it, so moving between inspector panels neither
 * refetches nor drops the live stream.
 */

import { useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ApiError, api } from '../api'
import { NewJob } from '../components/NewJob'
import { ErrorNote, Progress, Spinner, StatusPill } from '../components/ui'
import { isLive, useJobStream } from '../hooks/useJobStream'
import type { Connection } from '../hooks/useJobStream'
import { fullTime, since } from '../lib/format'
import type { JobSeed } from '../types'
import { Composer } from './job/Composer'
import { Inspector, panelFromPath } from './job/Inspector'
import { SessionStream } from './job/SessionStream'
import type { StreamFilter } from './job/SessionStream'

const CONNECTION_TEXT: Record<Connection, string> = {
  connecting: 'connecting',
  live: 'live',
  reconnecting: 'reconnecting…',
  closed: 'stream closed',
  gone: 'unavailable',
}

/** Thousands separators everywhere; `1.2M` once a job gets genuinely expensive. */
function tokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(2)}M`
  return count.toLocaleString()
}

/** Cents matter on a cheap job and stop mattering on an expensive one. */
function money(amount: number): string {
  return amount < 1 ? `$${amount.toFixed(3)}` : `$${amount.toFixed(2)}`
}

export function JobScreen() {
  const { jobId = '' } = useParams()
  const { pathname } = useLocation()
  const { job, events, connection, error, refresh, reopen } = useJobStream(jobId)
  const [acting, setActing] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [seed, setSeed] = useState<JobSeed | null>(null)
  const [filter, setFilter] = useState<StreamFilter>('all')
  const [phase, setPhase] = useState<number | null>(null)

  const { panel, open } = panelFromPath(pathname)

  const act = async (name: 'pause' | 'resume' | 'stop') => {
    setActing(name)
    setActionError(null)
    try {
      await api[name](jobId)
      await refresh()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : `could not ${name} this job`)
    } finally {
      setActing(null)
    }
  }

  if (!job) {
    return (
      <section className="screen">
        {error ? <ErrorNote>{error}</ErrorNote> : <Spinner label="Loading job" />}
      </section>
    )
  }

  const live = isLive(job.status)
  const complete = job.plan.filter((row) => row.status === 'complete').length
  const waiting =
    job.approvals.filter((row) => row.status === 'pending').length +
    job.questions.filter((row) => row.status === 'pending').length
  const queued = job.messages.filter(
    (message) => message.role === 'operator' && message.consumed_at === null,
  )
  const openRerun = () =>
    setSeed({
      from: job.id,
      task: job.task,
      mode: job.mode,
      team_id: job.team_id,
      provider_id: job.provider_id,
      sandbox: job.sandbox,
      token_budget: job.token_budget,
      agents: job.agent_providers,
    })

  return (
    <section className="console" data-inspecting={open ? 'true' : 'false'}>
      <header className="console-head">
        <div className="console-title">
          <h1>{job.task}</h1>
          <p className="console-meta">
            <StatusPill status={job.status} />
            {job.paused ? <span className="pill pill-muted">paused</span> : null}
            {waiting > 0 ? (
              <span className="pill pill-warn">
                <span className="dot" aria-hidden />
                {waiting} waiting on you
              </span>
            ) : null}
            <span className="mono">{job.id.slice(0, 8)}</span>
            <span aria-hidden>·</span>
            <span>{job.mode}</span>
            <span aria-hidden>·</span>
            <span>{job.provider_id ?? 'default provider'}</span>
            {job.sandbox ? (
              <>
                <span aria-hidden>·</span>
                <span>{job.sandbox}</span>
              </>
            ) : null}
            {job.rounds > 1 ? (
              <>
                <span aria-hidden>·</span>
                <span>{job.rounds} rounds</span>
              </>
            ) : null}
            {job.forked_from ? (
              <>
                <span aria-hidden>·</span>
                <Link to={`/jobs/${job.forked_from}`} className="link">
                  forked from {job.forked_from.slice(0, 8)}
                </Link>
              </>
            ) : null}
            <span aria-hidden>·</span>
            <span title={fullTime(job.created_at)}>{since(job.created_at)}</span>
            <span aria-hidden>·</span>
            <span className={`stream-state stream-${connection}`}>
              <span className="dot" aria-hidden />
              {CONNECTION_TEXT[connection]}
            </span>
          </p>
        </div>

        <div className="console-gauge">
          <Progress done={complete} total={job.plan.length} />
          <p className="console-gauge-text">
            <span>{job.plan.length > 0 ? `${complete}/${job.plan.length} phases` : 'planning'}</span>
            {job.total_tokens > 0 ? (
              <Link
                to={`/jobs/${jobId}/tokens`}
                className="console-spend"
                title={`${job.prompt_tokens.toLocaleString()} in · ${job.completion_tokens.toLocaleString()} out · ${job.provider_calls} calls`}
              >
                {tokens(job.total_tokens)} tok
                {job.usage.cost !== null ? ` · ${money(job.usage.cost)}` : ''}
              </Link>
            ) : null}
          </p>
        </div>

        <div className="console-verbs">
          {live ? (
            <>
              <button
                type="button"
                className="button"
                disabled={acting !== null}
                onClick={() => void act(job.paused ? 'resume' : 'pause')}
              >
                {job.paused ? 'Resume' : 'Pause'}
              </button>
              <button
                type="button"
                className="button danger"
                disabled={acting !== null}
                onClick={() => void act('stop')}
              >
                Stop
              </button>
            </>
          ) : null}
          <Link
            to={open ? `/jobs/${jobId}` : `/jobs/${jobId}/plan`}
            className="button ghost console-inspect"
          >
            {open ? 'Back to the stream' : 'Inspect'}
          </Link>
        </div>
      </header>

      {actionError ? <ErrorNote>{actionError}</ErrorNote> : null}
      {job.error ? <ErrorNote>{job.error}</ErrorNote> : null}

      <div className="console-body">
        <div className="console-main">
          <SessionStream
            jobId={jobId}
            job={job}
            events={events}
            filter={filter}
            onFilter={setFilter}
            phase={phase}
            onPhase={setPhase}
            onChanged={() => void refresh()}
          />
          <Composer
            jobId={jobId}
            live={live}
            canContinue={job.can_continue}
            queued={queued}
            onSent={() => void refresh()}
            onContinued={() => void reopen()}
            onRerun={openRerun}
          />
        </div>

        <Inspector
          jobId={jobId}
          job={job}
          events={events}
          panel={panel}
          phase={phase}
          onPhase={setPhase}
          onChanged={() => void refresh()}
        />
      </div>

      <NewJob open={seed !== null} seed={seed} onClose={() => setSeed(null)} />
    </section>
  )
}
