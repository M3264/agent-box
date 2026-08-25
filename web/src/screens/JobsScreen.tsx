/**
 * The jobs list — PLAN.md §4's home screen.
 *
 * The "needs attention" figure comes from `pending_approvals`, computed by the list
 * endpoint. v1 derived it from `status === 'approval-needed'`, a status the backend
 * never set, so the card read 0 no matter what was waiting.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Empty, ErrorNote, Progress, Spinner, StatusPill } from '../components/ui'
import { usePoll } from '../hooks/usePoll'
import { since } from '../lib/format'
import type { JobStatus, JobSummary } from '../types'

type Filter = 'all' | 'live' | 'attention' | 'done'

const LIVE: JobStatus[] = ['queued', 'planning', 'running', 'blocked']

function matches(job: JobSummary, filter: Filter): boolean {
  switch (filter) {
    case 'live':
      return LIVE.includes(job.status)
    case 'attention':
      return job.pending_approvals > 0 || job.status === 'error'
    case 'done':
      return job.status === 'complete' || job.status === 'stopped'
    default:
      return true
  }
}

export function JobsScreen() {
  const { data, error, loading } = usePoll(() => api.jobs(), 4000)
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')

  const jobs = data ?? []
  const counts = useMemo(
    () => ({
      live: jobs.filter((job) => matches(job, 'live')).length,
      attention: jobs.filter((job) => matches(job, 'attention')).length,
      done: jobs.filter((job) => matches(job, 'done')).length,
    }),
    [jobs],
  )

  const visible = jobs.filter(
    (job) =>
      matches(job, filter) &&
      (query === '' ||
        job.task.toLowerCase().includes(query.toLowerCase()) ||
        job.id.includes(query)),
  )

  return (
    <section className="screen">
      <div className="cards">
        <button
          type="button"
          className={`card ${filter === 'live' ? 'card-on' : ''}`}
          onClick={() => setFilter(filter === 'live' ? 'all' : 'live')}
        >
          <span className="card-value">{counts.live}</span>
          <span className="card-label">In flight</span>
        </button>
        <button
          type="button"
          className={`card ${filter === 'attention' ? 'card-on' : ''} ${counts.attention ? 'card-warn' : ''}`}
          onClick={() => setFilter(filter === 'attention' ? 'all' : 'attention')}
        >
          <span className="card-value">{counts.attention}</span>
          <span className="card-label">Needs attention</span>
        </button>
        <button
          type="button"
          className={`card ${filter === 'done' ? 'card-on' : ''}`}
          onClick={() => setFilter(filter === 'done' ? 'all' : 'done')}
        >
          <span className="card-value">{counts.done}</span>
          <span className="card-label">Finished</span>
        </button>
      </div>

      <div className="toolbar">
        <input
          className="search"
          type="search"
          placeholder="Search tasks…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search jobs"
        />
        {filter !== 'all' ? (
          <button type="button" className="button ghost" onClick={() => setFilter('all')}>
            Clear filter
          </button>
        ) : null}
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {loading && jobs.length === 0 ? <Spinner label="Loading jobs" /> : null}

      {!loading && visible.length === 0 ? (
        <Empty
          title={jobs.length === 0 ? 'No jobs yet' : 'Nothing matches that filter'}
          hint={jobs.length === 0 ? 'Start one with “New job” in the top right.' : undefined}
        />
      ) : (
        <ul className="job-list">
          {visible.map((job) => (
            <li key={job.id}>
              <Link to={`/jobs/${job.id}`} className="job-row">
                <div className="job-main">
                  <p className="job-task">{job.task}</p>
                  <p className="job-meta">
                    <code>{job.id}</code>
                    <span>·</span>
                    <span>{job.mode}</span>
                    <span>·</span>
                    <span>updated {since(job.updated_at)}</span>
                    {job.provider_id ? (
                      <>
                        <span>·</span>
                        <span>{job.provider_id}</span>
                      </>
                    ) : null}
                  </p>
                </div>

                <div className="job-progress">
                  <Progress done={job.phase_complete} total={job.phase_total} />
                  <span className="job-progress-text">
                    {job.phase_total > 0
                      ? `${job.phase_complete}/${job.phase_total} phases`
                      : 'planning'}
                  </span>
                </div>

                <div className="job-flags">
                  {job.pending_approvals > 0 ? (
                    <span className="pill pill-warn">
                      {job.pending_approvals} to approve
                    </span>
                  ) : null}
                  {job.artifact_count > 0 ? (
                    <span className="pill pill-idle">
                      {job.artifact_count} artifact{job.artifact_count === 1 ? '' : 's'}
                    </span>
                  ) : null}
                  {job.paused ? <span className="pill pill-muted">Paused</span> : null}
                  <StatusPill status={job.status} />
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
