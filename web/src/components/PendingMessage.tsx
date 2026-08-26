/**
 * A message the operator has queued and the team has not read yet.
 *
 * That window used to be invisible and one-way: you typed, it vanished into the
 * transcript, and if you had the wrong bucket name in it you got to watch the agent act
 * on it. So the window is now a thing on screen with all four verbs on it — reword it,
 * send it now instead of at the phase boundary, put it back to the boundary, or
 * withdraw it entirely.
 *
 * Withdrawing is two clicks on purpose. It is the only one of the four that destroys
 * what you wrote, and it is next to three that do not.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, api } from '../api'
import { since } from '../lib/format'
import type { JobMessage } from '../types'

export function PendingMessage({
  jobId,
  message,
  where,
  onChanged,
}: {
  jobId: string
  message: JobMessage
  /** Which job it is queued on. Given only where the job is not already implied. */
  where?: ReactNode
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(message.content)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const immediate = message.delivery === 'immediate'

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await work()
      setEditing(false)
      setConfirming(false)
      onChanged()
    } catch (cause) {
      // 409 here is the interesting one: the team read it while this was open. The
      // server's wording explains that better than anything generic would.
      setError(cause instanceof ApiError ? cause.message : 'could not change that message')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={`queued${immediate ? ' queued-now' : ''}`}>
      <header className="queued-head">
        <span className={`chip chip-${immediate ? 'busy' : 'idle'}`}>
          {immediate ? 'interrupting' : 'at the next phase'}
        </span>
        {where ? where : null}
        <span className="queued-when">
          queued {since(message.created_at)}
          {message.updated_at ? ' · edited' : ''}
        </span>
      </header>

      {editing ? (
        <textarea
          className="queued-edit"
          value={draft}
          rows={3}
          onChange={(event) => setDraft(event.target.value)}
          aria-label="Edit the queued message"
        />
      ) : (
        <p className="queued-body">{message.content}</p>
      )}

      {error ? (
        <p className="queued-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="queued-actions">
        {editing ? (
          <>
            <button
              type="button"
              className="button ghost"
              onClick={() => {
                setDraft(message.content)
                setEditing(false)
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="button primary"
              disabled={busy || draft.trim() === '' || draft === message.content}
              onClick={() => void run(() => api.editMessage(jobId, message.id, { content: draft }))}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
          </>
        ) : (
          <>
            <button type="button" className="button ghost" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button
              type="button"
              className="button ghost"
              disabled={busy}
              onClick={() =>
                void run(() =>
                  api.editMessage(jobId, message.id, {
                    delivery: immediate ? 'boundary' : 'immediate',
                  }),
                )
              }
            >
              {immediate ? 'Hold until the phase ends' : 'Send it now'}
            </button>
            {confirming ? (
              <button
                type="button"
                className="button danger"
                disabled={busy}
                onClick={() => void run(() => api.cancelMessage(jobId, message.id))}
              >
                {busy ? 'Withdrawing…' : 'Really withdraw'}
              </button>
            ) : (
              <button type="button" className="button ghost" onClick={() => setConfirming(true)}>
                Withdraw
              </button>
            )}
          </>
        )}
      </div>
    </article>
  )
}
