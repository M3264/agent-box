/**
 * One approval gate, wherever it appears.
 *
 * Shared by the job's stream and the cross-job Attention screen, because a gate is the
 * same object and the same decision in both places — and when the two had separate
 * implementations they drifted: one showed the risk reason, the other did not.
 *
 * The note is optional but it is recorded with the decision and shown in the
 * transcript, which is the only durable answer to "why was this rejected".
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, api } from '../api'
import { StatusPill } from './ui'
import { IconGate } from './icons'
import { fullTime, since } from '../lib/format'
import type { Approval } from '../types'

export function GateCard({
  jobId,
  approval,
  where,
  onDecided,
}: {
  jobId: string
  approval: Approval
  /** Which job this belongs to. Given only where the job is not already implied. */
  where?: ReactNode
  onDecided: () => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const decide = async (decision: 'approved' | 'rejected') => {
    setBusy(true)
    setError(null)
    try {
      await api.decide(jobId, approval.id, decision, note.trim() || undefined)
      setNote('')
      onDecided()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not record that decision')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={`gate gate-${approval.status}`}>
      <header className="gate-head">
        <div className="gate-title">
          <span className="gate-icon" aria-hidden>
            <IconGate />
          </span>
          <div>
            <h3>{approval.action}</h3>
            <p className="gate-meta">
              {where ? (
                <>
                  {where}
                  <span aria-hidden>·</span>
                </>
              ) : null}
              {approval.phase_seq !== null && approval.phase_seq !== undefined ? (
                <>
                  <span>
                    phase {approval.phase_seq}
                    {approval.phase_name ? ` · ${approval.phase_name}` : ''}
                  </span>
                  <span aria-hidden>·</span>
                </>
              ) : null}
              <span>{approval.agent ?? 'system'}</span>
              <span aria-hidden>·</span>
              <span title={fullTime(approval.created_at)}>
                raised {since(approval.created_at)}
              </span>
              {approval.risk ? (
                <>
                  <span aria-hidden>·</span>
                  <span className={`pill pill-${approval.risk === 'high' ? 'bad' : 'warn'}`}>
                    {approval.risk} risk
                  </span>
                </>
              ) : null}
              {approval.auto ? (
                <>
                  <span aria-hidden>·</span>
                  <span className="pill pill-muted">auto</span>
                </>
              ) : null}
            </p>
          </div>
        </div>
        <StatusPill status={approval.status} />
      </header>

      {approval.detail ? <p className="gate-detail">{approval.detail}</p> : null}
      {error ? (
        <p className="gate-error" role="alert">
          {error}
        </p>
      ) : null}

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
            disabled={busy}
            onClick={() => void decide('rejected')}
          >
            Reject
          </button>
          <button
            type="button"
            className="button primary"
            disabled={busy}
            onClick={() => void decide('approved')}
          >
            {busy ? 'Recording…' : 'Approve'}
          </button>
        </div>
      ) : (
        <p className="gate-outcome">
          {approval.status} {approval.decided_at ? since(approval.decided_at) : ''}
          {approval.decision_note ? ` — “${approval.decision_note}”` : ''}
        </p>
      )}
    </article>
  )
}
