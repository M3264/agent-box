/**
 * Routing and the app shell.
 *
 * HashRouter, not BrowserRouter: the bundle is served at both `/` and `/hub/` with
 * `base: './'`, so assets are resolved relative to the document. A history-routed
 * deep link like `/hub/jobs/abc` would make the browser look for
 * `/hub/jobs/assets/…`. Keeping the route in the fragment holds the document path
 * at the mount point, where the relative asset URLs are correct.
 */

import { useState } from 'react'
import { HashRouter, Link, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { NewJob } from './components/NewJob'
import { usePoll } from './hooks/usePoll'
import { api } from './api'
import { InboxScreen } from './screens/InboxScreen'
import { JobScreen } from './screens/JobScreen'
import { JobsScreen } from './screens/JobsScreen'
import { SettingsScreen } from './screens/SettingsScreen'

function Shell() {
  const [creating, setCreating] = useState(false)
  const health = usePoll(api.health, 15_000)
  const inbox = usePoll(() => api.inbox('pending'), 10_000)
  const pending = inbox.data?.length ?? 0

  return (
    <>
      <header className="topbar">
        <Link to="/jobs" className="brand">
          <span className="brand-mark" aria-hidden />
          <span className="brand-text">Agent Hub</span>
        </Link>

        <nav className="nav" aria-label="Primary">
          <NavLink to="/jobs" className="nav-link">
            Jobs
          </NavLink>
          <NavLink to="/approvals" className="nav-link">
            Approvals
            {pending > 0 ? <span className="nav-count">{pending}</span> : null}
          </NavLink>
          <NavLink to="/settings" className="nav-link">
            Settings
          </NavLink>
        </nav>

        <div className="topbar-right">
          <span
            className={`health health-${health.data?.status ?? (health.error ? 'degraded' : 'unknown')}`}
            title={
              health.error
                ? `API unreachable: ${health.error}`
                : `v${health.data?.version ?? '?'} · schema ${health.data?.schema_version ?? '?'} · ${health.data?.active_jobs ?? 0} active`
            }
          >
            <span className="dot" aria-hidden />
            {health.error ? 'offline' : (health.data?.status ?? '…')}
          </span>
          <button type="button" className="button primary" onClick={() => setCreating(true)}>
            New job
          </button>
        </div>
      </header>

      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<JobsScreen />} />
          <Route path="/jobs/:jobId/*" element={<JobScreen />} />
          <Route path="/approvals" element={<InboxScreen />} />
          <Route path="/settings" element={<SettingsScreen />} />
          <Route
            path="*"
            element={
              <div className="panel empty">
                <p className="empty-title">No such screen</p>
                <Link className="button ghost" to="/jobs">
                  Back to jobs
                </Link>
              </div>
            }
          />
        </Routes>
      </main>

      <NewJob open={creating} onClose={() => setCreating(false)} />
    </>
  )
}

export function App() {
  return (
    <HashRouter>
      <Shell />
    </HashRouter>
  )
}
