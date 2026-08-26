/**
 * The plan tab.
 *
 * These rows are the engine's real state machine, not decoration: v1 inserted one
 * phase per hardcoded role at creation and never updated them, so every job showed
 * five permanently-queued phases — including completed jobs.
 *
 * Phases are grouped by round. A continued job appends a second planning phase and
 * its own work behind the first round's, and a flat list of nine phases hides the
 * one thing the operator wants to see: which of them answered the follow-up.
 */

import { Empty, StatusPill } from '../../components/ui'
import { duration } from '../../lib/format'
import { Markdown } from '../../lib/markdown'
import type { Phase } from '../../types'

function byRound(plan: Phase[]): [round: number, phases: Phase[]][] {
  const groups = new Map<number, Phase[]>()
  for (const phase of plan) {
    const round = phase.round || 1
    const existing = groups.get(round)
    if (existing) existing.push(phase)
    else groups.set(round, [phase])
  }
  return [...groups.entries()].sort((a, b) => a[0] - b[0])
}

function PhaseRow({ phase }: { phase: Phase }) {
  return (
    <li className={`phase phase-${phase.status}`}>
      <div className="phase-head">
        <span className="phase-seq">{phase.seq}</span>
        <div className="phase-title">
          <h3>{phase.name}</h3>
          <p className="phase-meta">
            <span className="owner">{phase.owner}</span>
            <span>·</span>
            <span>{phase.kind}</span>
            {phase.requires_approval ? (
              <>
                <span>·</span>
                <span className="pill pill-warn">gated</span>
              </>
            ) : null}
            {phase.attempts > 1 ? (
              <>
                <span>·</span>
                <span>{phase.attempts} attempts</span>
              </>
            ) : null}
            {phase.started_at ? (
              <>
                <span>·</span>
                <span>{duration(phase.started_at, phase.finished_at)}</span>
              </>
            ) : null}
            {phase.total_tokens > 0 ? (
              <>
                <span>·</span>
                <span title={`${phase.prompt_tokens} in, ${phase.completion_tokens} out`}>
                  {phase.total_tokens.toLocaleString()} tok
                </span>
              </>
            ) : null}
          </p>
        </div>
        <StatusPill status={phase.status} />
      </div>

      {phase.acceptance ? (
        <p className="phase-acceptance">
          <span>Acceptance</span> {phase.acceptance}
        </p>
      ) : null}

      {phase.error ? <p className="phase-error">{phase.error}</p> : null}

      {phase.output ? (
        <details className="phase-output">
          <summary>Output ({phase.output.length.toLocaleString()} chars)</summary>
          <Markdown source={phase.output} className="phase-prose" />
        </details>
      ) : null}
    </li>
  )
}

export function PlanView({ plan }: { plan: Phase[] }) {
  if (plan.length === 0) return <Empty title="No plan yet" hint="The manager is still planning." />

  const rounds = byRound(plan)
  if (rounds.length === 1) {
    return (
      <ol className="phases">
        {plan.map((phase) => (
          <PhaseRow key={phase.id} phase={phase} />
        ))}
      </ol>
    )
  }

  return (
    <div className="rounds">
      {rounds.map(([round, phases]) => (
        <section key={round} className="round">
          <h2 className="round-head">
            Round {round}
            <span className="round-count">
              {phases.filter((phase) => phase.status === 'complete').length}/{phases.length} complete
            </span>
          </h2>
          <ol className="phases">
            {phases.map((phase) => (
              <PhaseRow key={phase.id} phase={phase} />
            ))}
          </ol>
        </section>
      ))}
    </div>
  )
}
