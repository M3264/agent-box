/**
 * The composer, docked to the bottom of the console and never anywhere else.
 *
 * In the old shell this was the tail of one tab, so typing to the team meant first
 * navigating back to the tab where the box lived. A conversation you can only join from
 * one of nine screens is not a conversation. It is docked now: whatever the inspector
 * is showing, the way to say something is in the same place.
 *
 * Two things it does that the old one could not:
 *
 * - **Choose when a message lands.** `boundary` waits for the phase to finish, which is
 *   the polite default and how guidance used to work. `immediate` goes into the running
 *   agent's very next turn, which is what you want when you have just watched it start
 *   down the wrong path and do not intend to wait for it to finish.
 * - **Show what is still unsent, and let it be changed.** A queued message used to
 *   vanish into the transcript the moment you pressed send, with the wrong bucket name
 *   in it and nothing to do but watch.
 *
 * It also stays open on a finished job, where it switches from "send" to "continue" — a
 * finished job is not a finished conversation.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api } from '../../api'
import { PendingMessage } from '../../components/PendingMessage'
import { IconSend } from '../../components/icons'
import { ErrorNote } from '../../components/ui'
import type { Delivery, JobMessage } from '../../types'

interface Props {
  jobId: string
  /** True while the engine still holds the job: a message can reach the team. */
  live: boolean
  /** True on a terminal job: another round can be added to it. */
  canContinue: boolean
  /** Operator messages the team has not read yet. */
  queued: JobMessage[]
  onSent: () => void
  /** A round was started, so the caller can reopen a stream it had let close. */
  onContinued: () => void
  onRerun: () => void
}

export function Composer({
  jobId,
  live,
  canContinue,
  queued,
  onSent,
  onContinued,
  onRerun,
}: Props) {
  const [draft, setDraft] = useState('')
  const [delivery, setDelivery] = useState<Delivery>('boundary')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showQueue, setShowQueue] = useState(false)

  const send = async () => {
    const content = draft.trim()
    if (!content || sending) return
    setSending(true)
    setError(null)
    try {
      if (live) {
        await api.sendMessage(jobId, content, delivery)
        setDraft('')
        onSent()
      } else {
        await api.continueJob(jobId, content)
        setDraft('')
        onContinued()
      }
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : live
            ? 'could not send that message'
            : 'could not continue this job',
      )
    } finally {
      setSending(false)
    }
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    void send()
  }

  const open = live || canContinue

  return (
    <div className="composer">
      {queued.length > 0 ? (
        <div className="composer-queue">
          <button
            type="button"
            className="queue-toggle"
            aria-expanded={showQueue}
            onClick={() => setShowQueue((current) => !current)}
          >
            <span className="queue-dot" aria-hidden />
            {queued.length} message{queued.length === 1 ? '' : 's'} not read yet
            <span className="queue-toggle-verb">{showQueue ? 'hide' : 'edit or withdraw'}</span>
          </button>
          {showQueue ? (
            <div className="queue-list">
              {queued.map((message) => (
                <PendingMessage
                  key={message.id}
                  jobId={jobId}
                  message={message}
                  onChanged={onSent}
                />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <form className="composer-form" onSubmit={onSubmit}>
        <textarea
          className="composer-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter makes a new line — the usual chat contract.
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              void send()
            }
          }}
          placeholder={
            live
              ? delivery === 'immediate'
                ? 'Interrupt the agent — this lands in its very next turn…'
                : 'Say something to the team — picked up when this phase ends…'
              : canContinue
                ? 'This job finished. Say what should happen next…'
                : 'This job is winding down; try again in a moment.'
          }
          rows={1}
          disabled={!open}
          aria-label={live ? 'Message the team' : 'Continue this job'}
        />

        <div className="composer-side">
          {live ? (
            <div className="composer-when" role="group" aria-label="When it lands">
              <button
                type="button"
                className={`when${delivery === 'boundary' ? ' when-on' : ''}`}
                aria-pressed={delivery === 'boundary'}
                onClick={() => setDelivery('boundary')}
              >
                when this phase ends
              </button>
              <button
                type="button"
                className={`when${delivery === 'immediate' ? ' when-on' : ''}`}
                aria-pressed={delivery === 'immediate'}
                onClick={() => setDelivery('immediate')}
              >
                right now
              </button>
            </div>
          ) : (
            <p className="composer-note">
              {canContinue
                ? 'The team picks up where it left off; earlier rounds stay as they are.'
                : 'Waiting for the engine to let go of this job.'}
            </p>
          )}

          <div className="composer-actions">
            <button type="button" className="button ghost" onClick={onRerun}>
              Run again
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={!open || sending || draft.trim() === ''}
            >
              <IconSend />
              {sending ? (live ? 'Sending…' : 'Starting…') : live ? 'Send' : 'Continue'}
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}
