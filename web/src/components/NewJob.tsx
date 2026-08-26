/**
 * Job creation, and re-running an existing job.
 *
 * The per-agent table is the substance here. A provider is not one model, and a team
 * is not one provider: the operator can run the manager on a large model and the
 * specialists on a cheap one, or point one role at a different endpoint entirely.
 * Every choice is optional and falls through to the job default, so the simple case
 * is still "type a task and press start".
 *
 * `seed` turns the same form into "run again": the fields arrive prefilled from an
 * existing job and submission goes to `/rerun`, which records `forked_from` and
 * inherits anything left untouched.
 */

import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api'
import type {
  AgentProvider,
  JobSeed,
  Mode,
  Provider,
  SandboxInfo,
  SandboxKind,
  Team,
  TeamRole,
} from '../types'
import { ErrorNote } from './ui'

/** A row of the per-agent table. Empty strings mean "inherit the job default". */
interface Assignment {
  provider_id: string
  model: string
}

const BLANK: Assignment = { provider_id: '', model: '' }

interface Props {
  open: boolean
  onClose: () => void
  /** Prefill from an existing job and submit as a re-run of it. */
  seed?: JobSeed | null
}

export function NewJob({ open, onClose, seed }: Props) {
  const navigate = useNavigate()
  const [task, setTask] = useState('')
  const [mode, setMode] = useState<Mode>('controlled')
  const [teamId, setTeamId] = useState<number | ''>('')
  const [providerId, setProviderId] = useState('')
  const [model, setModel] = useState('')
  const [sandbox, setSandbox] = useState<SandboxKind | ''>('')
  const [assignments, setAssignments] = useState<Record<string, Assignment>>({})
  const [showAgents, setShowAgents] = useState(false)
  const [teams, setTeams] = useState<Team[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [sandboxes, setSandboxes] = useState<SandboxInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let alive = true
    void Promise.all([api.teams(), api.providers(), api.sandbox()])
      .then(([loadedTeams, loadedProviders, loadedSandbox]) => {
        if (!alive) return
        setTeams(loadedTeams)
        setProviders(loadedProviders.filter((profile) => profile.enabled))
        setSandboxes(loadedSandbox)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [open])

  // Reset on close rather than on open, so a failed submit keeps what was typed and
  // reopening after a successful one starts clean.
  useEffect(() => {
    if (open) return
    setTask('')
    setModel('')
    setSandbox('')
    setAssignments({})
    setShowAgents(false)
    setError(null)
    setBusy(false)
  }, [open])

  // Escape closes, as it does for the drawer. Without this the scrim was the only way
  // out — and the scrim covers the nav, so an accidental open was a dead end.
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // Apply the seed once the modal opens. Deliberately keyed on the seed's job id: a
  // second "run again" on a different job must refill, and re-running the same one
  // twice must not stomp edits made in between.
  useEffect(() => {
    if (!open || !seed) return
    setTask(seed.task)
    setMode(seed.mode)
    setTeamId(seed.team_id)
    setProviderId(seed.provider_id ?? '')
    setSandbox(seed.sandbox ?? '')
    const seeded: Record<string, Assignment> = {}
    for (const entry of seed.agents) {
      seeded[entry.agent] = { provider_id: entry.provider_id ?? '', model: entry.model ?? '' }
    }
    setAssignments(seeded)
    setShowAgents(seed.agents.length > 0)
  }, [open, seed?.from]) // eslint-disable-line react-hooks/exhaustive-deps

  const team = useMemo(() => {
    if (teamId !== '') return teams.find((candidate) => candidate.id === teamId) ?? null
    return teams.find((candidate) => candidate.is_default) ?? teams[0] ?? null
  }, [teamId, teams])

  const roles: TeamRole[] = team?.roles ?? []

  // `/api/providers` is ordered by rowid and the server picks the first enabled row as
  // its default, so the head of this list genuinely is what an unset provider resolves
  // to — worth naming rather than showing a blank "server default".
  const serverDefault = providers[0] ?? null
  const jobProvider = providers.find((profile) => profile.id === providerId) ?? serverDefault
  const jobModel = model || jobProvider?.model || ''

  /** The models to offer for a row, given whichever provider that row resolves to. */
  const modelsFor = (assignment: Assignment): Provider | null =>
    providers.find((profile) => profile.id === (assignment.provider_id || jobProvider?.id)) ?? null

  const patch = (role: string, changes: Partial<Assignment>) =>
    setAssignments((current) => {
      const next = { ...(current[role] ?? BLANK), ...changes }
      // Changing provider invalidates a model the new one may not serve; the backend
      // would reject it with a 400 and this is a friendlier place to notice.
      if (changes.provider_id !== undefined) {
        const target = providers.find((profile) => profile.id === changes.provider_id)
        if (next.model && target && !target.models.some((entry) => entry.model === next.model)) {
          next.model = ''
        }
      }
      return { ...current, [role]: next }
    })

  const setAll = (changes: Partial<Assignment>) =>
    setAssignments((current) => {
      const next = { ...current }
      for (const role of roles) next[role.id] = { ...(next[role.id] ?? BLANK), ...changes }
      return next
    })

  const overrides = roles.filter((role) => {
    const entry = assignments[role.id]
    return Boolean(entry?.provider_id || entry?.model)
  }).length

  if (!open) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!task.trim() || busy) return
    setBusy(true)
    setError(null)

    // Only rows the operator actually set are sent. A job-level model is expressed the
    // same way, per agent, because that is where the engine reads it — and it means the
    // stored assignment says exactly what each agent ran on.
    const agents: AgentProvider[] = []
    for (const role of roles) {
      const entry = assignments[role.id]
      const chosenModel = entry?.model || (model && !entry?.provider_id ? model : '')
      if (!entry?.provider_id && !chosenModel) continue
      agents.push({
        agent: role.id,
        provider_id: entry?.provider_id || null,
        model: chosenModel || null,
      })
    }

    try {
      const created = seed
        ? // Every field is sent explicitly, including nulls: the form shows the
          // inherited settings, so leaving one as "server default" has to actually
          // clear it rather than silently inherit the original's.
          await api.rerunJob(seed.from, {
            task: task.trim(),
            mode,
            ...(teamId === '' ? {} : { team_id: teamId }),
            provider_id: providerId || null,
            sandbox: sandbox || null,
            agents,
          })
        : await api.createJob({
            task: task.trim(),
            mode,
            ...(teamId === '' ? {} : { team_id: teamId }),
            ...(providerId ? { provider_id: providerId } : {}),
            ...(sandbox ? { sandbox } : {}),
            ...(agents.length > 0 ? { agents } : {}),
          })
      onClose()
      void navigate(`/jobs/${created.id}`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'could not create the job')
      setBusy(false)
    }
  }

  const chosen = sandbox || sandboxes?.default
  const chosenState = sandboxes?.backends.find((backend) => backend.id === chosen)

  return (
    <div className="modal-root">
      <button type="button" className="drawer-scrim" aria-label="Cancel" onClick={onClose} />
      <form className="modal" onSubmit={submit} aria-label={seed ? 'Run again' : 'New job'}>
        <header className="modal-head">
          <div>
            <h2>{seed ? 'Run again' : 'New job'}</h2>
            {seed ? (
              <p className="modal-sub">
                Forked from <code>{seed.from}</code> — change what should differ, the rest is
                inherited.
              </p>
            ) : null}
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body">
          <label className="field">
            <span>Task</span>
            <textarea
              value={task}
              onChange={(event) => setTask(event.target.value)}
              placeholder="What should the team deliver?"
              rows={5}
              autoFocus
              required
            />
          </label>

          <div className="field-row">
            <label className="field">
              <span>Mode</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as Mode)}>
                <option value="controlled">Controlled — risky steps wait for approval</option>
                <option value="yolo">Yolo — auto-approve, recorded in the audit trail</option>
              </select>
            </label>

            <label className="field">
              <span>Team</span>
              <select
                value={teamId}
                onChange={(event) =>
                  setTeamId(event.target.value === '' ? '' : Number(event.target.value))
                }
              >
                <option value="">Default template</option>
                {teams.map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.name} ({candidate.roles.length} roles)
                    {candidate.is_default ? ' — default' : ''}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="field-row">
            <label className="field">
              <span>Provider</span>
              <select
                value={providerId}
                onChange={(event) => {
                  setProviderId(event.target.value)
                  setModel('')
                }}
              >
                <option value="">
                  {serverDefault ? `Server default — ${serverDefault.label}` : 'Server default'}
                </option>
                {providers.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.label}
                    {profile.models.length > 1 ? ` (${profile.models.length} models)` : ''}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>Model</span>
              <select
                value={model}
                onChange={(event) => setModel(event.target.value)}
                disabled={!jobProvider}
              >
                <option value="">
                  {jobProvider ? `Profile default — ${jobProvider.model}` : 'No provider configured'}
                </option>
                {(jobProvider?.models ?? []).map((entry) => (
                  <option key={entry.model} value={entry.model}>
                    {entry.label ?? entry.model}
                    {entry.supports_tools ? '' : ' — no tool calling'}
                  </option>
                ))}
              </select>
              {jobProvider && jobProvider.models.length === 0 ? (
                <span className="field-note">
                  No models listed for this profile. Add or discover them in Settings.
                </span>
              ) : null}
            </label>

            {sandboxes?.tools_enabled ? (
              <label className="field">
                <span>Confinement</span>
                <select
                  value={sandbox}
                  onChange={(event) => setSandbox(event.target.value as SandboxKind | '')}
                >
                  <option value="">Server default ({sandboxes.default})</option>
                  {sandboxes.backends.map((backend) => (
                    <option
                      key={backend.id}
                      value={backend.id}
                      disabled={!backend.available}
                      title={backend.available ? backend.reason : `Unavailable: ${backend.reason}`}
                    >
                      {backend.label}
                      {!backend.available ? ' (unavailable)' : ''}
                      {backend.id === 'unconfined'
                        ? ' — runs as the service user, no credential masking'
                        : ''}
                    </option>
                  ))}
                </select>
                {chosenState && !chosenState.available ? (
                  <span className="field-note error">{chosenState.reason}</span>
                ) : chosenState?.id === 'unconfined' ? (
                  <span className="field-note warn">
                    Unconfined: the service user&apos;s files, credentials, and sudo access are
                    reachable.
                  </span>
                ) : null}
              </label>
            ) : null}
          </div>

          <section className="agent-assign">
            <button
              type="button"
              className="agent-assign-toggle"
              aria-expanded={showAgents}
              onClick={() => setShowAgents(!showAgents)}
            >
              <span className="agent-assign-caret" aria-hidden>
                {showAgents ? '▾' : '▸'}
              </span>
              Per-agent provider and model
              <span className="muted">
                {overrides > 0
                  ? `${overrides} of ${roles.length} overridden`
                  : `all ${roles.length} on ${jobProvider?.label ?? 'the default'} · ${jobModel || 'profile default'}`}
              </span>
            </button>

            {showAgents ? (
              roles.length === 0 ? (
                <p className="field-note">Pick a team to see its roles.</p>
              ) : (
                <>
                  <table className="assign-table">
                    <thead>
                      <tr>
                        <th scope="col">Agent</th>
                        <th scope="col">Provider</th>
                        <th scope="col">Model</th>
                      </tr>
                    </thead>
                    <tbody>
                      {roles.map((role) => {
                        const entry = assignments[role.id] ?? BLANK
                        const rowProvider = modelsFor(entry)
                        return (
                          <tr key={role.id}>
                            <th scope="row">
                              <span className="cell-title">{role.name}</span>
                              <span className="cell-sub mono">
                                {role.id}
                                {role.orchestrator ? ' · orchestrator' : ''}
                              </span>
                            </th>
                            <td>
                              <select
                                aria-label={`Provider for ${role.name}`}
                                value={entry.provider_id}
                                onChange={(event) =>
                                  patch(role.id, { provider_id: event.target.value })
                                }
                              >
                                <option value="">
                                  Job default{jobProvider ? ` — ${jobProvider.label}` : ''}
                                </option>
                                {providers.map((profile) => (
                                  <option key={profile.id} value={profile.id}>
                                    {profile.label}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td>
                              <select
                                aria-label={`Model for ${role.name}`}
                                value={entry.model}
                                onChange={(event) => patch(role.id, { model: event.target.value })}
                                disabled={!rowProvider}
                              >
                                <option value="">
                                  {entry.provider_id
                                    ? `Profile default — ${rowProvider?.model ?? '?'}`
                                    : `Job default — ${jobModel || '?'}`}
                                </option>
                                {(rowProvider?.models ?? []).map((option) => (
                                  <option key={option.model} value={option.model}>
                                    {option.label ?? option.model}
                                  </option>
                                ))}
                              </select>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>

                  <div className="assign-foot">
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() => setAssignments({})}
                      disabled={overrides === 0}
                    >
                      Reset to job default
                    </button>
                    <label className="field inline">
                      <span>Set every agent to</span>
                      <select
                        value=""
                        onChange={(event) => {
                          if (event.target.value) setAll({ provider_id: event.target.value, model: '' })
                        }}
                      >
                        <option value="">Choose a provider…</option>
                        {providers.map((profile) => (
                          <option key={profile.id} value={profile.id}>
                            {profile.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                </>
              )
            ) : null}
          </section>

          {error ? <ErrorNote>{error}</ErrorNote> : null}
        </div>

        <footer className="modal-foot">
          <button type="button" className="button ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy || !task.trim()}>
            {busy ? 'Starting…' : seed ? 'Run again' : 'Start job'}
          </button>
        </footer>
      </form>
    </div>
  )
}
