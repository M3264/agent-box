/**
 * The plan tab.
 *
 * These rows are the engine's real state machine, not decoration: v1 inserted one
 * phase per hardcoded role at creation and never updated them, so every job showed
 * five permanently-queued phases — including completed jobs.
 */

import { Empty, StatusPill } from '../../components/ui'
import { duration } from '../../lib/format'
import type { Phase } from '../../types'

export function PlanView({ plan }: { plan: Phase[] }) {
  if (plan.length === 0) return <Empty title="No plan yet" hint="The manager is still planning." />

  return (
    <ol className="phases">
      {plan.map((phase) => (
        <li key={phase.id} className={`phase phase-${phase.status}`}>
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
              <div className="prose">{phase.output}</div>
            </details>
          ) : null}
        </li>
      ))}
    </ol>
  )
}
