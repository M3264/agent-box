/**
 * Routing and the app shell.
 *
 * HashRouter, not BrowserRouter: the bundle is served at both `/` and `/hub/` with
 * `base: './'`, so assets are resolved relative to the document. A history-routed
 * deep link like `/hub/jobs/abc` would make the browser look for
 * `/hub/jobs/assets/…`. Keeping the route in the fragment holds the document path
 * at the mount point, where the relative asset URLs are correct.
 *
 * The chrome is a rail, not a bar. It is a bottom dock on a phone — where the thumb
 * is — and a left rail on a desktop, and those are the *same element* moved by the
 * grid rather than a bar that pretends to be a rail by flipping `flex-direction`. What
 * that buys is the whole vertical axis for the thing being looked at: a job screen is
 * a transcript, and a transcript wants height.
 *
 * Three destinations and no more. Everything else in this app belongs to a job, and
 * putting it in the global chrome was what made the old shell feel like a filing
 * cabinet: nine tabs, none of which were where the work was happening.
 */

import { useEffect, useState } from 'react'
import { HashRouter, Link, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { CommandPalette } from './components/CommandPalette'
import { NewJob } from './components/NewJob'
import { IconAttention, IconJobs, IconPlus, IconSearch, IconSettings } from './components/icons'
import { AttentionProvider, useAttention } from './hooks/useAttention'
import { usePoll } from './hooks/usePoll'
import { api } from './api'
import { AttentionScreen } from './screens/AttentionScreen'
import { JobScreen } from './screens/JobScreen'
import { JobsScreen } from './screens/JobsScreen'
import { SettingsScreen } from './screens/SettingsScreen'
import type { ReactNode } from 'react'

function RailLink({
  to,
  icon,
  text,
  count,
}: {
  to: string
  icon: ReactNode
  text: string
  count?: number
}) {
  return (
    <NavLink to={to} className="rail-link">
      <span className="rail-icon">
        {icon}
        {count !== undefined && count > 0 ? (
          <span className="rail-count" aria-hidden>
            {count > 9 ? '9+' : count}
          </span>
        ) : null}
      </span>
      <span className="rail-text">{text}</span>
      {count !== undefined && count > 0 ? (
        <span className="sr-only">{count} waiting</span>
      ) : null}
    </NavLink>
  )
}

function Rail({ onNewJob, onFind }: { onNewJob: () => void; onFind: () => void }) {
  const health = usePoll(api.health, 15_000)
  const { counts } = useAttention()
  const state = health.data?.status ?? (health.error ? 'degraded' : 'unknown')

  return (
    <nav className="rail" aria-label="Primary">
      <Link to="/jobs" className="rail-brand">
        <span className="brand-mark" aria-hidden />
        <span className="brand-text">Agent Hub</span>
      </Link>

      <RailLink to="/jobs" icon={<IconJobs />} text="Jobs" />
      <RailLink
        to="/attention"
        icon={<IconAttention />}
        text="Attention"
        count={counts.blocking}
      />
      <RailLink to="/settings" icon={<IconSettings />} text="Settings" />

      <span className="rail-gap" aria-hidden />

      <button type="button" className="rail-link rail-find" onClick={onFind}>
        <span className="rail-icon">
          <IconSearch />
        </span>
        <span className="rail-text">Find</span>
        <kbd className="rail-key">⌘K</kbd>
      </button>

      <button type="button" className="rail-link rail-new" onClick={onNewJob}>
        <span className="rail-icon">
          <IconPlus />
        </span>
        <span className="rail-text">New job</span>
      </button>

      {/*
        Silent when the service is fine. On a phone the dock has no room for a status
        it reports four hundred times a day, so it appears only when it has something
        to say — which is also the only time anyone reads it.
      */}
      <span
        className={`rail-health health-${state}`}
        title={
          health.error
            ? `API unreachable: ${health.error}`
            : `v${health.data?.version ?? '?'} · schema ${health.data?.schema_version ?? '?'} · ${health.data?.active_jobs ?? 0} active`
        }
      >
        <span className="dot" aria-hidden />
        <span className="rail-text">{health.error ? 'offline' : state}</span>
      </span>
    </nav>
  )
}

function Shell() {
  const [creating, setCreating] = useState(false)
  const [finding, setFinding] = useState(false)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setFinding((current) => !current)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="app">
      <Rail onNewJob={() => setCreating(true)} onFind={() => setFinding(true)} />

      <main className="stage">
        <Routes>
          <Route path="/" element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<JobsScreen />} />
          <Route path="/jobs/:jobId/*" element={<JobScreen />} />
          <Route path="/attention" element={<AttentionScreen />} />
          {/* The old inbox. Bookmarks and the odd deep link should still land. */}
          <Route path="/approvals" element={<Navigate to="/attention" replace />} />
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
      <CommandPalette
        open={finding}
        onClose={() => setFinding(false)}
        onNewJob={() => setCreating(true)}
      />
    </div>
  )
}

export function App() {
  return (
    <HashRouter>
      <AttentionProvider>
        <Shell />
      </AttentionProvider>
    </HashRouter>
  )
}
