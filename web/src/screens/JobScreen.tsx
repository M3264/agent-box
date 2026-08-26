/**
 * Job detail: header, controls, and the routed tabs from PLAN.md §4.
 *
 * One `useJobStream` for the whole screen, so switching tabs neither refetches nor
 * drops the stream. Tab state lives in the URL, which makes a particular view of a
 * job linkable — `#/jobs/<id>/approvals` goes straight to the gates.
 *
 * A finished job keeps every control that still means something. Continue lives in
 * the composer, because it needs an instruction; Run again lives here, because it
 * opens a form. Both are reachable from a job that ended hours ago.
 */

import { useState } from 'react'
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { ApiError, api } from '../api'
import { NewJob } from '../components/NewJob'
import { ErrorNote, Progress, Spinner, StatusPill } from '../components/ui'
import { isLive, useJobStream } from '../hooks/useJobStream'
import type { Connection } from '../hooks/useJobStream'
import { fullTime, since } from '../lib/format'
import { Markdown } from '../lib/markdown'
import type { JobSeed } from '../types'
import { AgentsView } from './job/AgentsView'
import { ApprovalsView } from './job/ApprovalsView'
import { ArtifactsView } from './job/ArtifactsView'
import { CommandsView } from './job/CommandsView'
import { Conversation } from './job/Conversation'
import { PlanView } from './job/PlanView'
import { Timeline } from './job/Timeline'
import { UsageView } from './job/UsageView'

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

export function JobScreen() {
  const { jobId = '' } = useParams()
  const { job, events, connection, error, refresh, reopen } = useJobStream(jobId)
  const [acting, setActing] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [seed, setSeed] = useState<JobSeed | null>(null)

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
  const pending = job.approvals.filter((approval) => approval.status === 'pending').length
  const complete = job.plan.filter((phase) => phase.status === 'complete').length
  const tools = job.tool_calls.length
  const openRerun = () =>
    setSeed({
      from: job.id,
      task: job.task,
      mode: job.mode,
      team_id: job.team_id,
      provider_id: job.provider_id,
      sandbox: job.sandbox,
      agents: job.agent_providers,
    })

  const tab = (path: string, text: string, count?: number) => (
    <NavLink to={`/jobs/${jobId}${path}`} end={path === ''} className="tab">
      {text}
      {count ? <span className="tab-count">{count}</span> : null}
    </NavLink>
  )

  return (
    <section className="screen job-screen">
      <header className="job-head">
        <div className="job-head-main">
          <h1 className="job-title">{job.task}</h1>
          <p className="job-head-meta">
            <code>{job.id}</code>
            <span>·</span>
            <span>{job.mode}</span>
            <span>·</span>
            <span>{job.provider_id ?? 'default provider'}</span>
            {job.rounds > 1 ? (
              <>
                <span>·</span>
                <span>{job.rounds} rounds</span>
              </>
            ) : null}
            {job.forked_from ? (
              <>
                <span>·</span>
                <NavLink to={`/jobs/${job.forked_from}`} className="link">
                  forked from {job.forked_from.slice(0, 8)}
                </NavLink>
              </>
            ) : null}
            <span>·</span>
            <span title={fullTime(job.created_at)}>created {since(job.created_at)}</span>
            <span>·</span>
            <span className={`stream stream-${connection}`}>
              <span className="dot" aria-hidden />
              {CONNECTION_TEXT[connection]}
            </span>
          </p>
        </div>

        <div className="job-head-side">
          <div className="job-head-status">
            <StatusPill status={job.status} />
            {job.paused ? <span className="pill pill-muted">Paused</span> : null}
            {job.total_tokens > 0 ? (
              <NavLink
                to={`/jobs/${jobId}/usage`}
                className="pill pill-idle"
                title={`${job.prompt_tokens.toLocaleString()} in · ${job.completion_tokens.toLocaleString()} out · ${job.provider_calls} calls`}
              >
                {tokens(job.total_tokens)} tokens
              </NavLink>
            ) : null}
          </div>
          <div className="job-head-progress">
            <Progress done={complete} total={job.plan.length} />
            <span className="job-progress-text">
              {job.plan.length > 0 ? `${complete}/${job.plan.length} phases` : 'planning'}
            </span>
          </div>
          <div className="job-controls">
            {live ? (
              <>
                {job.paused ? (
                  <button
                    type="button"
                    className="button"
                    disabled={acting !== null}
                    onClick={() => void act('resume')}
                  >
                    Resume
                  </button>
                ) : (
                  <button
                    type="button"
                    className="button"
                    disabled={acting !== null}
                    onClick={() => void act('pause')}
                  >
                    Pause
                  </button>
                )}
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
            <button type="button" className="button" onClick={openRerun}>
              Run again
            </button>
          </div>
        </div>
      </header>

      {actionError ? <ErrorNote>{actionError}</ErrorNote> : null}
      {job.error ? <ErrorNote>{job.error}</ErrorNote> : null}

      <nav className="tabs" aria-label="Job sections">
        {tab('', 'Conversation')}
        {job.result ? tab('/result', 'Result') : null}
        {tab('/timeline', 'Timeline', events.length)}
        {tab('/plan', 'Plan', job.plan.length)}
        {tab('/agents', 'Agents', job.team.length)}
        {tab('/commands', 'Commands', tools || undefined)}
        {tab('/artifacts', 'Artifacts', job.artifacts.length)}
        {tab('/approvals', 'Approvals', pending)}
        {tab('/usage', 'Usage')}
      </nav>

      <div className="tab-body">
        <Routes>
          <Route
            index
            element={
              <Conversation
                jobId={jobId}
                events={events}
                messages={job.messages}
                live={live}
                canContinue={job.can_continue}
                onSent={() => void refresh()}
                onContinued={() => void reopen()}
                onRerun={openRerun}
              />
            }
          />
          <Route
            path="result"
            element={
              <div className="result-view">
                <Markdown source={job.result?.content} className="result-prose" />
              </div>
            }
          />
          <Route path="timeline" element={<Timeline events={events} />} />
          <Route path="plan" element={<PlanView plan={job.plan} />} />
          <Route
            path="agents"
            element={
              <AgentsView
                team={job.team}
                plan={job.plan}
                events={events}
                assignments={job.agent_providers}
                usage={job.usage}
              />
            }
          />
          <Route
            path="commands"
            element={
              <CommandsView
                jobId={jobId}
                toolCalls={job.tool_calls}
                plan={job.plan}
                sandbox={job.sandbox}
              />
            }
          />
          <Route
            path="artifacts"
            element={<ArtifactsView jobId={jobId} artifacts={job.artifacts} plan={job.plan} />}
          />
          <Route
            path="approvals"
            element={
              <ApprovalsView
                jobId={jobId}
                approvals={job.approvals}
                onDecided={() => void refresh()}
              />
            }
          />
          <Route path="usage" element={<UsageView job={job} />} />
          <Route path="*" element={<Navigate to={`/jobs/${jobId}`} replace />} />
        </Routes>
      </div>

      <NewJob open={seed !== null} seed={seed} onClose={() => setSeed(null)} />
    </section>
  )
}
