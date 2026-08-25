/**
 * The agents tab, with the inspector as a drawer (PLAN.md §4).
 *
 * Each card is one row of `job_agents`; the drawer adds the per-agent slice of the
 * event log and the phases that agent owns, which is the context needed to answer
 * "what is this one actually doing".
 */

import { useState } from 'react'
import { Drawer } from '../../components/Drawer'
import { Avatar, Empty, StatusPill } from '../../components/ui'
import { since, text, time } from '../../lib/format'
import type { Agent, JobEvent, Phase } from '../../types'

interface Props {
  team: Agent[]
  plan: Phase[]
  events: JobEvent[]
}

export function AgentsView({ team, plan, events }: Props) {
  const [selected, setSelected] = useState<string | null>(null)
  const agent = team.find((member) => member.agent === selected) ?? null

  const owned = agent ? plan.filter((phase) => phase.owner === agent.agent) : []
  const trail = agent ? events.filter((event) => event.source === agent.agent).slice(-40) : []

  return (
    <>
      {team.length === 0 ? (
        <Empty title="No agents have run yet" hint="They appear as the manager brings them in." />
      ) : (
        <ul className="agents">
          {team.map((member) => (
            <li key={member.agent}>
              <button type="button" className="agent-card" onClick={() => setSelected(member.agent)}>
                <Avatar agent={member.agent} />
                <div className="agent-main">
                  <p className="agent-name">{member.agent}</p>
                  <p className="agent-action">{member.current_action ?? 'idle'}</p>
                </div>
                <div className="agent-side">
                  <StatusPill status={member.status} />
                  <span className="agent-time">{since(member.updated_at)}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      <Drawer
        open={agent !== null}
        title={agent?.agent ?? ''}
        subtitle={agent ? `${agent.status} · updated ${since(agent.updated_at)}` : undefined}
        onClose={() => setSelected(null)}
      >
        {agent ? (
          <>
            <section className="drawer-section">
              <h3>Current action</h3>
              <p className="drawer-text">{agent.current_action ?? 'idle'}</p>
            </section>

            <section className="drawer-section">
              <h3>Phases owned ({owned.length})</h3>
              {owned.length === 0 ? (
                <p className="drawer-text muted">None assigned.</p>
              ) : (
                <ul className="drawer-list">
                  {owned.map((phase) => (
                    <li key={phase.id}>
                      <span className="drawer-list-main">
                        {phase.seq}. {phase.name}
                      </span>
                      <StatusPill status={phase.status} />
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="drawer-section">
              <h3>Recent activity</h3>
              {trail.length === 0 ? (
                <p className="drawer-text muted">Nothing recorded.</p>
              ) : (
                <ol className="drawer-trail">
                  {trail.map((event) => (
                    <li key={event.id}>
                      <span className="drawer-trail-time">{time(event.created_at)}</span>
                      <span className="drawer-trail-kind">{event.kind}</span>
                      <span className="drawer-trail-text">
                        {text(
                          event.payload.current_action ??
                            event.payload.status ??
                            event.payload.name ??
                            event.payload.content ??
                            '',
                        ).slice(0, 200)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </>
        ) : null}
      </Drawer>
    </>
  )
}
