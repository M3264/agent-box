/**
 * Settings: provider profiles and team templates.
 *
 * Both tables existed in v1 and changed nothing — the runtime read
 * `~/.codex/config.toml` directly and used a hardcoded role list. They are now the
 * actual source of configuration, which is what makes this screen worth having.
 *
 * Secrets are referenced, never entered: a profile stores the *name* of an
 * environment variable and the server resolves it at call time. `secret_ok` is how
 * this screen can say "that reference resolves" without the value ever leaving the
 * server.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api } from '../api'
import { Empty, ErrorNote, Spinner } from '../components/ui'
import { usePoll } from '../hooks/usePoll'
import { fullTime } from '../lib/format'
import type { Provider, Team, TeamRole } from '../types'

interface ProviderDraft {
  id: string
  label: string
  base_url: string
  model: string
  secret_ref: string
  headers: string
  enabled: boolean
}

const BLANK_PROVIDER: ProviderDraft = {
  id: '',
  label: '',
  base_url: 'https://',
  model: '',
  secret_ref: '',
  headers: '{}',
  enabled: true,
}

const BLANK_ROLE: TeamRole = { id: '', name: '', instructions: '', orchestrator: false }

function toDraft(profile: Provider): ProviderDraft {
  return {
    id: profile.id,
    label: profile.label,
    base_url: profile.base_url,
    model: profile.model,
    secret_ref: profile.secret_ref ?? '',
    headers: JSON.stringify(profile.headers, null, 2),
    enabled: profile.enabled,
  }
}

function secretNote(profile: Provider): { text: string; tone: string } {
  if (!profile.secret_ref) return { text: 'no secret needed', tone: 'muted' }
  if (profile.secret_ok) return { text: `${profile.secret_ref} resolves`, tone: 'good' }
  return { text: `${profile.secret_ref} does not resolve`, tone: 'bad' }
}

export function SettingsScreen() {
  const health = usePoll(api.health, 15_000)

  return (
    <section className="screen settings">
      <Providers />
      <Teams />
      <Tools />

      <section className="panel">
        <header className="panel-head">
          <h2>Service</h2>
        </header>
        {health.error ? (
          <ErrorNote>{health.error}</ErrorNote>
        ) : health.data ? (
          <dl className="facts">
            <div>
              <dt>Status</dt>
              <dd>{health.data.status}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{health.data.version}</dd>
            </div>
            <div>
              <dt>Schema</dt>
              <dd>{health.data.schema_version}</dd>
            </div>
            <div>
              <dt>Active jobs</dt>
              <dd>{health.data.active_jobs}</dd>
            </div>
            <div>
              <dt>Stream subscribers</dt>
              <dd>{health.data.detail.subscribers ?? 0}</dd>
            </div>
            <div>
              <dt>Pending approvals</dt>
              <dd>{health.data.detail.pending_approvals ?? 0}</dd>
            </div>
            <div className="wide">
              <dt>Database</dt>
              <dd className="mono">{health.data.detail.db_path ?? '—'}</dd>
            </div>
          </dl>
        ) : (
          <Spinner label="Checking the service" />
        )}
      </section>
    </section>
  )
}

function Tools() {
  const { data, error, loading } = usePoll(api.sandbox, 30_000)

  if (error) {
    return (
      <section className="panel">
        <header className="panel-head">
          <h2>Agent tools</h2>
        </header>
        <ErrorNote>{error}</ErrorNote>
      </section>
    )
  }
  if (!data) {
    return (
      <section className="panel">
        <header className="panel-head">
          <h2>Agent tools</h2>
        </header>
        {loading ? <Spinner label="Reading the sandbox probe" /> : null}
      </section>
    )
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Agent tools</h2>
        <span className={`pill pill-${data.tools_enabled ? 'good' : 'muted'}`}>
          {data.tools_enabled ? 'enabled' : 'disabled'}
        </span>
      </header>

      <p className="panel-note">
        Specialists can run shell commands, read and write files, and fetch URLs inside
        their job&apos;s workspace. Every call is recorded in the job&apos;s Commands tab;
        commands matching a guardrail wait for an approval in both modes.
      </p>

      <table className="table">
        <thead>
          <tr>
            <th scope="col">Confinement</th>
            <th scope="col">State</th>
            <th scope="col">What the probe found</th>
          </tr>
        </thead>
        <tbody>
          {data.backends.map((backend) => (
            <tr key={backend.id}>
              <th scope="row">
                <span className="cell-title">{backend.label}</span>
                <span className="cell-sub mono">
                  {backend.id}
                  {backend.id === data.default ? ' · default' : ''}
                </span>
              </th>
              <td>
                <span className={`pill pill-${backend.available ? 'good' : 'bad'}`}>
                  {backend.available ? 'available' : 'unavailable'}
                </span>
              </td>
              <td>{backend.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.default_available === false ? (
        <ErrorNote>
          The default confinement ({data.default}) is unavailable on this host, so jobs that
          do not pick another one will fail rather than run unconfined. Set
          AGENT_HUB_SANDBOX to a working backend, or fix the one above.
        </ErrorNote>
      ) : null}

      <dl className="facts">
        <div>
          <dt>Default</dt>
          <dd className="mono">{data.default}</dd>
        </div>
        <div>
          <dt>Network</dt>
          <dd>{data.network ? 'reachable' : 'blocked'}</dd>
        </div>
        <div>
          <dt>Turns per phase</dt>
          <dd className="mono">{data.limits.max_turns}</dd>
        </div>
        <div>
          <dt>Per command</dt>
          <dd className="mono">{data.limits.command_timeout}s</dd>
        </div>
        <div>
          <dt>Per phase</dt>
          <dd className="mono">{data.limits.wall_clock}s</dd>
        </div>
        <div>
          <dt>Kept per stream</dt>
          <dd className="mono">{data.limits.output_limit} chars</dd>
        </div>
        <div className="wide">
          <dt>Where these come from</dt>
          <dd>
            The service environment, not this screen — AGENT_HUB_SANDBOX,
            AGENT_HUB_TOOLS, AGENT_HUB_TOOL_NETWORK and the AGENT_HUB_TOOL_* limits.
            Confinement is a property of the host, so a job overrides it at creation
            rather than an operator changing it under running jobs.
          </dd>
        </div>
      </dl>
    </section>
  )
}

function Providers() {
  const { data, error, loading, reload } = usePoll(api.providers, 20_000)
  const [draft, setDraft] = useState<ProviderDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const providers = data ?? []

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!draft || busy) return
    setBusy(true)
    setFormError(null)

    let headers: Record<string, string>
    try {
      const parsed = JSON.parse(draft.headers || '{}') as unknown
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new Error('headers must be a JSON object')
      }
      headers = parsed as Record<string, string>
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : 'headers must be valid JSON')
      setBusy(false)
      return
    }

    try {
      await api.saveProvider({
        id: draft.id.trim(),
        label: draft.label.trim(),
        kind: 'openai_compatible',
        base_url: draft.base_url.trim(),
        model: draft.model.trim(),
        secret_ref: draft.secret_ref.trim() || null,
        headers,
        enabled: draft.enabled,
      })
      setDraft(null)
      await reload()
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : 'could not save that profile')
    } finally {
      setBusy(false)
    }
  }

  const remove = async (profile: Provider) => {
    setFormError(null)
    try {
      const result = await api.deleteProvider(profile.id)
      if (!result.deleted) {
        // History wins: a profile referenced by jobs is disabled rather than
        // deleted, so the audit trail of what ran a job stays intact.
        setFormError(
          `${profile.id} is used by ${result.jobs} job${result.jobs === 1 ? '' : 's'}, so it was disabled instead of deleted.`,
        )
      }
      await reload()
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : 'could not delete that profile')
    }
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Providers</h2>
        <button
          type="button"
          className="button"
          onClick={() => setDraft(draft === null ? { ...BLANK_PROVIDER } : null)}
        >
          {draft === null ? 'Add profile' : 'Cancel'}
        </button>
      </header>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {formError ? <ErrorNote>{formError}</ErrorNote> : null}

      {draft !== null ? (
        <form className="form" onSubmit={save}>
          <div className="field-row">
            <label className="field">
              <span>Id</span>
              <input
                value={draft.id}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
                placeholder="agentrouter"
                pattern="[A-Za-z0-9._\-]+"
                required
              />
            </label>
            <label className="field">
              <span>Label</span>
              <input
                value={draft.label}
                onChange={(event) => setDraft({ ...draft, label: event.target.value })}
                placeholder="Agent Router"
                required
              />
            </label>
          </div>

          <div className="field-row">
            <label className="field">
              <span>Base URL</span>
              <input
                value={draft.base_url}
                onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
                placeholder="https://api.example.com/v1"
                required
              />
            </label>
            <label className="field">
              <span>Model</span>
              <input
                value={draft.model}
                onChange={(event) => setDraft({ ...draft, model: event.target.value })}
                placeholder="gpt-5.6-sol"
                required
              />
            </label>
          </div>

          <div className="field-row">
            <label className="field">
              <span>Secret reference</span>
              <input
                value={draft.secret_ref}
                onChange={(event) => setDraft({ ...draft, secret_ref: event.target.value })}
                placeholder="AGENT_HUB_API_KEY"
              />
              <small>
                The name of an environment variable. The value is read server-side and never
                stored or returned.
              </small>
            </label>
            <label className="field">
              <span>Extra headers (JSON)</span>
              <textarea
                value={draft.headers}
                onChange={(event) => setDraft({ ...draft, headers: event.target.value })}
                rows={3}
                spellCheck={false}
              />
            </label>
          </div>

          <label className="check">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
            />
            <span>Enabled — selectable when creating a job</span>
          </label>

          <div className="form-foot">
            <span className="muted">kind: openai_compatible</span>
            <button type="submit" className="button primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save profile'}
            </button>
          </div>
        </form>
      ) : null}

      {loading && providers.length === 0 ? <Spinner label="Loading providers" /> : null}
      {!loading && providers.length === 0 ? (
        <Empty title="No provider profiles" hint="Add one to give jobs somewhere to run." />
      ) : null}

      {providers.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Profile</th>
              <th scope="col">Endpoint</th>
              <th scope="col">Secret</th>
              <th scope="col">State</th>
              <th scope="col" className="right">
                Actions
              </th>
            </tr>
          </thead>
          <tbody>
            {providers.map((profile) => {
              const secret = secretNote(profile)
              return (
                <tr key={profile.id}>
                  <th scope="row">
                    <span className="cell-title">{profile.label}</span>
                    <span className="cell-sub mono">{profile.id}</span>
                  </th>
                  <td>
                    <span className="cell-title mono">{profile.model}</span>
                    <span className="cell-sub mono">{profile.base_url}</span>
                  </td>
                  <td>
                    <span className={`pill pill-${secret.tone}`}>{secret.text}</span>
                  </td>
                  <td>
                    <span className={`pill pill-${profile.enabled ? 'good' : 'muted'}`}>
                      {profile.enabled ? 'enabled' : 'disabled'}
                    </span>
                  </td>
                  <td className="right nowrap">
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() => setDraft(toDraft(profile))}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="button ghost danger"
                      onClick={() => void remove(profile)}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : null}
    </section>
  )
}

function Teams() {
  const { data, error, loading, reload } = usePoll(api.teams, 20_000)
  const [name, setName] = useState('')
  const [roles, setRoles] = useState<TeamRole[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const teams = data ?? []

  // Templates are append-only, so "edit" is really "start from this one".
  const duplicate = (team: Team) => {
    setName(`${team.name} (copy)`)
    setRoles(team.roles.map((role) => ({ ...role, orchestrator: role.orchestrator === true })))
  }

  const create = async (event: FormEvent) => {
    event.preventDefault()
    if (!roles || busy) return
    setBusy(true)
    setFormError(null)
    try {
      await api.createTeam({ name: name.trim(), roles })
      setRoles(null)
      setName('')
      await reload()
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : 'could not create that template')
    } finally {
      setBusy(false)
    }
  }

  const makeDefault = async (team: Team) => {
    setFormError(null)
    try {
      await api.setDefaultTeam(team.id)
      await reload()
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : 'could not change the default')
    }
  }

  const patch = (index: number, changes: Partial<TeamRole>) => {
    setRoles((current) =>
      current === null
        ? current
        : current.map((role, position) => (position === index ? { ...role, ...changes } : role)),
    )
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Team templates</h2>
        <button
          type="button"
          className="button"
          onClick={() =>
            setRoles(
              roles === null
                ? [
                    { id: 'manager', name: 'Manager', instructions: '', orchestrator: true },
                    { ...BLANK_ROLE },
                  ]
                : null,
            )
          }
        >
          {roles === null ? 'New template' : 'Cancel'}
        </button>
      </header>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {formError ? <ErrorNote>{formError}</ErrorNote> : null}

      {roles !== null ? (
        <form className="form" onSubmit={create}>
          <label className="field">
            <span>Template name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Manager, architect, coder, tester"
              required
            />
          </label>

          <ol className="role-editor">
            {roles.map((role, index) => (
              <li key={index} className="role-row">
                <div className="field-row">
                  <label className="field">
                    <span>Role id</span>
                    <input
                      value={role.id}
                      onChange={(event) => patch(index, { id: event.target.value })}
                      pattern="[a-z0-9_]+"
                      placeholder="coder"
                      required
                    />
                  </label>
                  <label className="field">
                    <span>Display name</span>
                    <input
                      value={role.name}
                      onChange={(event) => patch(index, { name: event.target.value })}
                      placeholder="Coder"
                      required
                    />
                  </label>
                  <label className="check role-lead">
                    <input
                      type="radio"
                      name="orchestrator"
                      checked={role.orchestrator === true}
                      onChange={() =>
                        setRoles((current) =>
                          current === null
                            ? current
                            : current.map((candidate, position) => ({
                                ...candidate,
                                orchestrator: position === index,
                              })),
                        )
                      }
                    />
                    <span>Orchestrator</span>
                  </label>
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Remove role ${index + 1}`}
                    disabled={roles.length <= 2}
                    onClick={() =>
                      setRoles((current) =>
                        current === null ? current : current.filter((_, position) => position !== index),
                      )
                    }
                  >
                    ✕
                  </button>
                </div>
                <label className="field">
                  <span>Instructions</span>
                  <textarea
                    value={role.instructions}
                    onChange={(event) => patch(index, { instructions: event.target.value })}
                    rows={2}
                    placeholder="What this role is responsible for, and what it must not do."
                    required
                  />
                </label>
              </li>
            ))}
          </ol>

          <div className="form-foot">
            <button
              type="button"
              className="button ghost"
              disabled={roles.length >= 12}
              onClick={() => setRoles([...roles, { ...BLANK_ROLE }])}
            >
              Add role
            </button>
            <button type="submit" className="button primary" disabled={busy}>
              {busy ? 'Creating…' : 'Create template'}
            </button>
          </div>
        </form>
      ) : null}

      {loading && teams.length === 0 ? <Spinner label="Loading templates" /> : null}

      <ul className="teams">
        {teams.map((team) => (
          <li key={team.id} className="team">
            <div className="team-head">
              <div>
                <h3>
                  {team.name}
                  {team.is_default ? <span className="pill pill-good">default</span> : null}
                </h3>
                <p className="team-meta">
                  <code>#{team.id}</code>
                  <span>·</span>
                  <span>v{team.version}</span>
                  <span>·</span>
                  <span>created {fullTime(team.created_at)}</span>
                </p>
              </div>
              <div className="team-actions">
                <button type="button" className="button ghost" onClick={() => duplicate(team)}>
                  Duplicate
                </button>
                {team.is_default ? null : (
                  <button type="button" className="button" onClick={() => void makeDefault(team)}>
                    Make default
                  </button>
                )}
              </div>
            </div>
            <ul className="roles">
              {team.roles.map((role) => (
                <li key={role.id} className="role">
                  <span className={`role-name ${role.orchestrator ? 'role-lead-tag' : ''}`}>
                    {role.name}
                    {role.orchestrator ? ' · orchestrator' : ''}
                  </span>
                  <span className="role-instructions">{role.instructions}</span>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  )
}
