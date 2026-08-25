/**
 * The approvals tab.
 *
 * Deciding here releases a phase that is genuinely blocked: the engine awaits the
 * gate, so approve continues the work and reject skips the phase with the reason
 * recorded. In v1 this wrote a row that unblocked nothing.
 */

import { useState } from 'react'
import { ApiError, api } from '../../api'
import { Empty, ErrorNote, StatusPill } from '../../components/ui'
import { fullTime, since } from '../../lib/format'
import type { Approval } from '../../types'

interface Props {
  jobId: string
  approvals: Approval[]
  onDecided: () => void
}

export function ApprovalsView({ jobId, approvals, onDecided }: Props) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const decide = async (approval: Approval, decision: 'approved' | 'rejected') => {
    setBusy(approval.id)
    setError(null)
    try {
      await api.decide(jobId, approval.id, decision, note.trim() || undefined)
      setNote('')
      onDecided()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not record that decision')
    } finally {
      setBusy(null)
    }
  }

  if (approvals.length === 0) {
    return (
      <Empty
        title="No approvals requested"
        hint="Gated phases raise one in controlled mode; yolo mode auto-approves and records it."
      />
    )
  }

  return (
    <div className="approvals">
      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {approvals.map((approval) => (
        <article key={approval.id} className={`gate gate-${approval.status}`}>
          <header className="gate-head">
            <div>
              <h3>{approval.action}</h3>
              <p className="gate-meta">
                {approval.phase_seq !== null && approval.phase_seq !== undefined ? (
                  <>
                    <span>
                      phase {approval.phase_seq}
                      {approval.phase_name ? ` · ${approval.phase_name}` : ''}
                    </span>
                    <span>·</span>
                  </>
                ) : null}
                <span>{approval.agent ?? 'system'}</span>
                <span>·</span>
                <span title={fullTime(approval.created_at)}>raised {since(approval.created_at)}</span>
                {approval.risk ? (
                  <>
                    <span>·</span>
                    <span className={`pill pill-${approval.risk === 'high' ? 'bad' : 'warn'}`}>
                      {approval.risk} risk
                    </span>
                  </>
                ) : null}
                {approval.auto ? (
                  <>
                    <span>·</span>
                    <span className="pill pill-muted">auto</span>
                  </>
                ) : null}
              </p>
            </div>
            <StatusPill status={approval.status} />
          </header>

          {approval.detail ? <p className="gate-detail">{approval.detail}</p> : null}

          {approval.status === 'pending' ? (
            <div className="gate-actions">
              <input
                type="text"
                className="gate-note"
                placeholder="Note (optional) — recorded with the decision"
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
              {approval.status} {approval.decided_at ? since(approval.decided_at) : ''}
              {approval.decision_note ? ` — “${approval.decision_note}”` : ''}
            </p>
          )}
        </article>
      ))}
    </div>
  )
}
