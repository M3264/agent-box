/** Job creation. Mode and provider are real choices now, not fields that get dropped. */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../api'
import type { Mode, Provider, SandboxInfo, SandboxKind, Team } from '../types'
import { ErrorNote } from './ui'

export function NewJob({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const [task, setTask] = useState('')
  const [mode, setMode] = useState<Mode>('controlled')
  const [teamId, setTeamId] = useState<number | ''>('')
  const [providerId, setProviderId] = useState('')
  const [sandbox, setSandbox] = useState<SandboxKind | ''>('')
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

  useEffect(() => {
    if (open) return
    setTask('')
    setSandbox('')
    setError(null)
    setBusy(false)
  }, [open])

  if (!open) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!task.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createJob({
        task: task.trim(),
        mode,
        ...(teamId === '' ? {} : { team_id: teamId }),
        ...(providerId ? { provider_id: providerId } : {}),
        ...(sandbox ? { sandbox } : {}),
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
      <form className="modal" onSubmit={submit} aria-label="New job">
        <header className="modal-head">
          <h2>New job</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

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
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name} ({team.roles.length} roles){team.is_default ? ' — default' : ''}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span>Provider</span>
            <select value={providerId} onChange={(event) => setProviderId(event.target.value)}>
              <option value="">Server default</option>
              {providers.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.label} — {profile.model}
                </option>
              ))}
            </select>
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
                  Unconfined: the service user's files, credentials, and sudo access are reachable.
                </span>
              ) : null}
            </label>
          ) : null}
        </div>

        {error ? <ErrorNote>{error}</ErrorNote> : null}

        <footer className="modal-foot">
          <button type="button" className="button ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy || !task.trim()}>
            {busy ? 'Starting…' : 'Start job'}
          </button>
        </footer>
      </form>
    </div>
  )
}
