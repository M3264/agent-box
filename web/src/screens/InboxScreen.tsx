/**
 * The cross-job approvals inbox.
 *
 * This is the screen that answers "what is waiting on me", which previously required
 * opening each job in turn. The rows are joined against `jobs` server-side, so each
 * one shows what it is gating without a request per approval.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../api'
import { Empty, ErrorNote, Spinner, StatusPill } from '../components/ui'
import { fullTime, since } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { ApprovalStatus, InboxApproval } from '../types'

type Filter = ApprovalStatus | 'all'

const FILTERS: Filter[] = ['pending', 'approved', 'rejected', 'all']

export function InboxScreen() {
  const [filter, setFilter] = useState<Filter>('pending')
  const { data, error, loading, reload } = usePoll(() => api.inbox(filter), 5000)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const approvals: InboxApproval[] = data ?? []

  const decide = async (approval: InboxApproval, decision: 'approved' | 'rejected') => {
    setBusy(approval.id)
    setActionError(null)
    try {
      await api.decide(approval.job_id, approval.id, decision, note.trim() || undefined)
      setNote('')
      await reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'could not record that decision')
      await reload()
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="screen">
      <header className="screen-head">
        <h1>Approvals</h1>
        <div className="chips" role="group" aria-label="Filter approvals">
          {FILTERS.map((name) => (
            <button
              key={name}
              type="button"
              className={`chip ${filter === name ? 'chip-on' : ''}`}
              onClick={() => {
                setFilter(name)
                // The poll keeps the fetcher in a ref, so ask for the new filter now
                // rather than waiting up to a full interval for the next tick.
                void reload()
              }}
            >
              {name}
            </button>
          ))}
        </div>
      </header>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {actionError ? <ErrorNote>{actionError}</ErrorNote> : null}
      {loading && approvals.length === 0 ? <Spinner label="Loading approvals" /> : null}

      {!loading && approvals.length === 0 ? (
        <Empty
          title={filter === 'pending' ? 'Nothing is waiting' : `No ${filter} approvals`}
          hint={
            filter === 'pending'
              ? 'Gated phases in controlled mode appear here the moment they block.'
              : undefined
          }
        />
      ) : (
        <div className="approvals">
          {approvals.map((approval) => (
            <article key={approval.id} className={`gate gate-${approval.status}`}>
              <header className="gate-head">
                <div>
                  <h3>{approval.action}</h3>
                  <p className="gate-meta">
                    <Link className="link" to={`/jobs/${approval.job_id}/approvals`}>
                      {approval.job_task.length > 80
                        ? `${approval.job_task.slice(0, 80)}…`
                        : approval.job_task}
                    </Link>
                    <span>·</span>
                    <span>{approval.job_mode}</span>
                    {approval.phase_name ? (
                      <>
                        <span>·</span>
                        <span>
                          phase {approval.phase_seq} · {approval.phase_name}
                        </span>
                      </>
                    ) : null}
                    <span>·</span>
                    <span title={fullTime(approval.created_at)}>{since(approval.created_at)}</span>
                  </p>
                </div>
                <div className="gate-head-side">
                  <StatusPill status={approval.job_status} />
                  <StatusPill status={approval.status} />
                </div>
              </header>

              {approval.detail ? <p className="gate-detail">{approval.detail}</p> : null}

              {approval.status === 'pending' ? (
                <div className="gate-actions">
                  <input
                    type="text"
                    className="gate-note"
                    placeholder="Note (optional)"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />
                  <button
                    type="button"
                    className="button danger"
                    disabled={busy === approval.id}
                    onClick={() => void decide(approval, 'rejected')}
                  >
                    Reject
                  </button>
                  <button
                    type="button"
                    className="button primary"
                    disabled={busy === approval.id}
                    onClick={() => void decide(approval, 'approved')}
                  >
                    {busy === approval.id ? 'Recording…' : 'Approve'}
                  </button>
                </div>
              ) : (
                <p className="gate-outcome">
                  {approval.status}
                  {approval.auto ? ' automatically' : ''}
                  {approval.decided_at ? ` ${since(approval.decided_at)}` : ''}
                  {approval.decision_note ? ` — “${approval.decision_note}”` : ''}
                </p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
