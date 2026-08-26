/**
 * What the job spent.
 *
 * Three cuts of the same number, because "how many tokens" is never the real
 * question. By agent answers "which role is expensive"; by model answers "is the
 * cheap model actually being used where I put it"; by round answers "what did that
 * follow-up cost me". Totals alone would answer none of them.
 *
 * Counts come from the provider's own usage block, accumulated as each call returns,
 * so a running job's numbers are live and a job that died mid-phase still reports
 * what it burned before it died.
 */

import type { CSSProperties } from 'react'
import { Empty } from '../../components/ui'
import type { JobSnapshot, Phase } from '../../types'

interface Props {
  job: JobSnapshot
}

/** Compact for headline figures, exact in the cells. */
function tokens(count: number): string {
  return count.toLocaleString()
}

/** Share of the whole, for the inline bars. Guards a zero total. */
function share(part: number, whole: number): number {
  if (whole <= 0) return 0
  return Math.min(100, Math.round((part / whole) * 100))
}

function byRound(plan: Phase[]): { round: number; phases: number; total: number }[] {
  const rounds = new Map<number, { round: number; phases: number; total: number }>()
  for (const phase of plan) {
    const key = phase.round || 1
    const entry = rounds.get(key) ?? { round: key, phases: 0, total: 0 }
    entry.phases += 1
    entry.total += phase.total_tokens
    rounds.set(key, entry)
  }
  return [...rounds.values()].sort((a, b) => a.round - b.round)
}

export function UsageView({ job }: Props) {
  const { totals, by_agent, by_model } = job.usage

  if (totals.calls === 0) {
    return (
      <Empty
        title="Nothing spent yet"
        hint="Counts appear as soon as the first provider call returns."
      />
    )
  }

  const rounds = byRound(job.plan)
  // A phase's tokens are attributed when it finishes, so mid-job the per-phase sum
  // trails the job total. Saying so is better than showing two numbers that disagree.
  const attributed = rounds.reduce((sum, entry) => sum + entry.total, 0)

  return (
    <div className="usage-view">
      <div className="usage-totals">
        <div className="usage-figure">
          <span className="usage-figure-value">{tokens(totals.total)}</span>
          <span className="usage-figure-label">total tokens</span>
        </div>
        <div className="usage-figure">
          <span className="usage-figure-value">{tokens(totals.prompt)}</span>
          <span className="usage-figure-label">prompt</span>
        </div>
        <div className="usage-figure">
          <span className="usage-figure-value">{tokens(totals.completion)}</span>
          <span className="usage-figure-label">completion</span>
        </div>
        <div className="usage-figure">
          <span className="usage-figure-value">{totals.calls.toLocaleString()}</span>
          <span className="usage-figure-label">
            provider {totals.calls === 1 ? 'call' : 'calls'}
          </span>
        </div>
        <div className="usage-figure">
          <span className="usage-figure-value">
            {tokens(Math.round(totals.total / Math.max(1, totals.calls)))}
          </span>
          <span className="usage-figure-label">avg per call</span>
        </div>
      </div>

      <section className="usage-section">
        <h2>By agent</h2>
        <table className="usage-table">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">Calls</th>
              <th scope="col">Prompt</th>
              <th scope="col">Completion</th>
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {by_agent.map((row) => (
              <tr key={row.agent ?? 'unattributed'}>
                <th scope="row">
                  <span className="usage-name">{row.agent ?? 'unattributed'}</span>
                  <span
                    className="usage-bar"
                    style={{ '--share': `${share(row.total, totals.total)}%` } as CSSProperties}
                    aria-hidden
                  />
                </th>
                <td>{row.calls.toLocaleString()}</td>
                <td>{tokens(row.prompt)}</td>
                <td>{tokens(row.completion)}</td>
                <td className="usage-strong">{tokens(row.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="usage-section">
        <h2>By provider and model</h2>
        <table className="usage-table">
          <thead>
            <tr>
              <th scope="col">Provider</th>
              <th scope="col">Model</th>
              <th scope="col">Calls</th>
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {by_model.map((row) => (
              <tr key={`${row.provider_id ?? '?'}:${row.model ?? '?'}`}>
                <th scope="row">{row.provider_id ?? 'default'}</th>
                <td className="mono">{row.model ?? 'unrecorded'}</td>
                <td>{row.calls.toLocaleString()}</td>
                <td className="usage-strong">{tokens(row.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {rounds.length > 0 ? (
        <section className="usage-section">
          <h2>By round</h2>
          <table className="usage-table">
            <thead>
              <tr>
                <th scope="col">Round</th>
                <th scope="col">Phases</th>
                <th scope="col">Total</th>
              </tr>
            </thead>
            <tbody>
              {rounds.map((entry) => (
                <tr key={entry.round}>
                  <th scope="row">Round {entry.round}</th>
                  <td>{entry.phases}</td>
                  <td className="usage-strong">{tokens(entry.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {attributed < totals.total ? (
            <p className="usage-note">
              {tokens(totals.total - attributed)} tokens are not attributed to a phase yet —
              planning and any phase still in flight are counted in the totals above but only
              land in a round once the phase commits.
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  )
}
