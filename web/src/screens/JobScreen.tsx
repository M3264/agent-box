/**
 * Job detail: header, controls, and the six routed tabs from PLAN.md §4.
 *
 * One `useJobStream` for the whole screen, so switching tabs neither refetches nor
 * drops the stream. Tab state lives in the URL, which makes a particular view of a
 * job linkable — `#/jobs/<id>/approvals` goes straight to the gates.
 */

import { useState } from 'react'
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { ApiError, api } from '../api'
import { ErrorNote, Progress, Spinner, StatusPill } from '../components/ui'
import { isLive, useJobStream } from '../hooks/useJobStream'
import type { Connection } from '../hooks/useJobStream'
import { fullTime, since } from '../lib/format'
import { AgentsView } from './job/AgentsView'
import { ApprovalsView } from './job/ApprovalsView'
import { ArtifactsView } from './job/ArtifactsView'
import { Conversation } from './job/Conversation'
import { PlanView } from './job/PlanView'
import { Timeline } from './job/Timeline'

const CONNECTION_TEXT: Record<Connection, string> = {
  connecting: 'connecting',
  live: 'live',
  reconnecting: 'reconnecting…',
  closed: 'stream closed',
  gone: 'unavailable',
}

export function JobScreen() {
  const { jobId = '' } = useParams()
  const { job, events, connection, error, refresh } = useJobStream(jobId)
  const [acting, setActing] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

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
          </div>
          <div className="job-head-progress">
            <Progress done={complete} total={job.plan.length} />
            <span className="job-progress-text">
              {job.plan.length > 0 ? `${complete}/${job.plan.length} phases` : 'planning'}
            </span>
          </div>
          {live ? (
            <div className="job-controls">
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
            </div>
          ) : null}
        </div>
      </header>

      {actionError ? <ErrorNote>{actionError}</ErrorNote> : null}
      {job.error ? <ErrorNote>{job.error}</ErrorNote> : null}

      <nav className="tabs" aria-label="Job sections">
        {tab('', 'Conversation')}
        {tab('/timeline', 'Timeline', events.length)}
        {tab('/plan', 'Plan', job.plan.length)}
        {tab('/agents', 'Agents', job.team.length)}
        {tab('/artifacts', 'Artifacts', job.artifacts.length)}
        {tab('/approvals', 'Approvals', pending)}
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
                onSent={() => void refresh()}
              />
            }
          />
          <Route path="timeline" element={<Timeline events={events} />} />
          <Route path="plan" element={<PlanView plan={job.plan} />} />
          <Route
            path="agents"
            element={<AgentsView team={job.team} plan={job.plan} events={events} />}
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
          <Route path="*" element={<Navigate to={`/jobs/${jobId}`} replace />} />
        </Routes>
      </div>
    </section>
  )
}
