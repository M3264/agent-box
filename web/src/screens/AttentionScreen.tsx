/**
 * Everything waiting on the operator, across every job.
 *
 * This replaces the Approvals inbox, which answered a question nobody asks. With five
 * jobs running the question is never "show me approvals" — it is "is anything waiting
 * for me", and the three things that can wait used to live on three different tabs of
 * five different screens.
 *
 * So gates and questions are interleaved into one queue, oldest first, because a job
 * that has been parked for twenty minutes matters more than one that parked four
 * seconds ago and the *kind* of wait is not what decides that. Queued messages are
 * listed separately below: they are pending operator intent rather than blocked work,
 * and mixing them in would make the count at the top a lie.
 *
 * Every item is answerable in place. Opening the job to approve something is exactly
 * the trip this screen exists to save.
 */

import { Link } from 'react-router-dom'
import { GateCard } from '../components/GateCard'
import { PendingMessage } from '../components/PendingMessage'
import { QuestionCard } from '../components/QuestionCard'
import { Empty, ErrorNote, Spinner } from '../components/ui'
import { useAttention } from '../hooks/useAttention'
import type { InboxApproval, InboxQuestion } from '../types'

type Wait =
  | { kind: 'gate'; at: number; key: string; gate: InboxApproval }
  | { kind: 'ask'; at: number; key: string; ask: InboxQuestion }

function JobLink({ id, task }: { id: string; task: string }) {
  return (
    <Link className="wait-job" to={`/jobs/${id}`} title={task}>
      {task}
    </Link>
  )
}

export function AttentionScreen() {
  const { data, error, counts, reload } = useAttention()

  const waits: Wait[] = data
    ? [
        ...data.approvals.map<Wait>((gate) => ({
          kind: 'gate',
          at: gate.created_at,
          key: `gate-${gate.id}`,
          gate,
        })),
        ...data.questions.map<Wait>((ask) => ({
          kind: 'ask',
          at: ask.created_at,
          key: `ask-${ask.id}`,
          ask,
        })),
      ].sort((a, b) => a.at - b.at)
    : []

  return (
    <section className="screen attention">
      <header className="screen-head">
        <div>
          <p className="eyebrow">The queue</p>
          <h1>
            {counts.blocking === 0
              ? 'Nothing is blocked'
              : `${counts.blocking} ${counts.blocking === 1 ? 'thing' : 'things'} waiting on you`}
          </h1>
        </div>
        <p className="screen-note">
          Oldest first. Answering here unblocks the job where it stands — there is no
          need to open it.
        </p>
      </header>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {data === null && !error ? <Spinner label="Checking every job" /> : null}

      {data !== null ? (
        <>
          {waits.length === 0 ? (
            <Empty
              title="Every job is either running or done"
              hint="Gates and questions land here the moment an agent raises one, and this screen keeps checking while you are elsewhere."
            />
          ) : (
            <div className="waits">
              {waits.map((wait) =>
                wait.kind === 'gate' ? (
                  <GateCard
                    key={wait.key}
                    jobId={wait.gate.job_id}
                    approval={wait.gate}
                    where={<JobLink id={wait.gate.job_id} task={wait.gate.job_task} />}
                    onDecided={() => void reload()}
                  />
                ) : (
                  <QuestionCard
                    key={wait.key}
                    jobId={wait.ask.job_id}
                    question={wait.ask}
                    where={<JobLink id={wait.ask.job_id} task={wait.ask.job_task} />}
                    onAnswered={() => void reload()}
                  />
                ),
              )}
            </div>
          )}

          {data.messages.length > 0 ? (
            <section className="panel queued-panel">
              <header className="panel-head">
                <h2>Not sent yet</h2>
                <p className="panel-note">
                  {data.messages.length} message{data.messages.length === 1 ? '' : 's'} waiting for
                  a phase to end. Nothing is blocked by these — but this is the last moment
                  they can be changed.
                </p>
              </header>
              <div className="queued-list">
                {data.messages.map((message) => (
                  <PendingMessage
                    key={`${message.job_id}-${message.id}`}
                    jobId={message.job_id}
                    message={message}
                    where={<JobLink id={message.job_id} task={message.job_task} />}
                    onChanged={() => void reload()}
                  />
                ))}
              </div>
            </section>
          ) : null}
        </>
      ) : null}
    </section>
  )
}
