/**
 * An agent asking the operator something, with buttons for the answers it offered.
 *
 * This is the piece that was missing. Faced with a decision only the operator can
 * make, a model used to guess and then explain its guess three phases later. Now the
 * job parks, this card appears in the stream, and the answer goes back into the
 * agent's next turn as prose.
 *
 * Deliberately shaped like a plan-mode prompt rather than a form: the options *are*
 * the buttons, one click answers, and the free-text box is an escape hatch underneath
 * rather than the primary path. Typing first and then clicking an option sends both —
 * the API takes a choice and a note together, and "Archive, but keep last month hot"
 * is a real answer that neither half expresses alone.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, api } from '../api'
import { IconAsk } from './icons'
import { Avatar, StatusPill } from './ui'
import { fullTime, since } from '../lib/format'
import { voiceOf } from '../lib/voices'
import type { Question } from '../types'

const OUTCOME: Record<string, string> = {
  answered: 'Answered',
  cancelled: 'Withdrawn — the job ended before it was answered',
  timeout: 'Timed out — the agent carried on with its own best guess',
}

export function QuestionCard({
  jobId,
  question,
  where,
  onAnswered,
}: {
  jobId: string
  question: Question
  /** Which job is asking. Given only where the job is not already implied. */
  where?: ReactNode
  onAnswered: () => void
}) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const pending = question.status === 'pending'

  const answer = async (chosen?: string) => {
    const note = text.trim()
    if (!chosen && !note) {
      setError('Type an answer, or pick one of the options.')
      return
    }
    setBusy(chosen ?? 'free')
    setError(null)
    try {
      await api.answerQuestion(jobId, question.id, {
        ...(chosen ? { chosen } : {}),
        ...(note ? { text: note } : {}),
      })
      setText('')
      onAnswered()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not send that answer')
    } finally {
      setBusy(null)
    }
  }

  return (
    <article className={`ask ask-${question.status}`} style={voiceOf(question.agent)}>
      <header className="ask-head">
        <Avatar agent={question.agent} />
        <div className="ask-who">
          <p className="ask-line">
            <span className="ask-agent">{question.agent}</span>
            <span className="ask-verb">is asking</span>
            {where ? (
              <>
                <span aria-hidden>·</span>
                {where}
              </>
            ) : null}
            {question.phase_name ? (
              <>
                <span aria-hidden>·</span>
                <span>{question.phase_name}</span>
              </>
            ) : null}
            <span aria-hidden>·</span>
            <span title={fullTime(question.created_at)}>{since(question.created_at)}</span>
          </p>
        </div>
        {pending ? (
          <span className="ask-badge" aria-hidden>
            <IconAsk />
          </span>
        ) : (
          <StatusPill status={question.status === 'answered' ? 'complete' : 'stopped'}>
            {question.status}
          </StatusPill>
        )}
      </header>

      <p className="ask-question">{question.question}</p>
      {question.detail ? <p className="ask-detail">{question.detail}</p> : null}

      {pending ? (
        <>
          {question.options.length > 0 ? (
            <div className="ask-options">
              {question.options.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className="ask-option"
                  disabled={busy !== null}
                  onClick={() => void answer(option.value)}
                >
                  <span className="ask-option-label">
                    {busy === option.value ? 'Sending…' : option.label}
                  </span>
                  {option.detail ? (
                    <span className="ask-option-detail">{option.detail}</span>
                  ) : null}
                </button>
              ))}
            </div>
          ) : null}

          {question.allow_free_text ? (
            <div className="ask-free">
              <textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder={
                  question.options.length > 0
                    ? 'Or write your own answer — typed here, it is sent with whichever option you click'
                    : 'Your answer'
                }
                rows={2}
                aria-label="Your answer"
              />
              <button
                type="button"
                className="button primary"
                disabled={busy !== null || text.trim() === ''}
                onClick={() => void answer()}
              >
                {busy === 'free' ? 'Sending…' : 'Send answer'}
              </button>
            </div>
          ) : null}

          {error ? (
            <p className="ask-error" role="alert">
              {error}
            </p>
          ) : null}
        </>
      ) : (
        <p className="ask-outcome">
          <span className="ask-outcome-kind">
            {OUTCOME[question.status] ?? question.status}
          </span>
          {question.answer ? <span className="ask-answer">“{question.answer}”</span> : null}
          {question.answered_at ? (
            <span className="ask-when" title={fullTime(question.answered_at)}>
              {since(question.answered_at)}
            </span>
          ) : null}
        </p>
      )}
    </article>
  )
}
