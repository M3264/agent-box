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
 *
 * The provider form asks two separate questions that used to be one. *Which protocol*
 * does the endpoint speak — answered from the short list of adapters this build has,
 * because guessing it from a URL is what made detection unreliable. And *which
 * models* does it serve — a list, not a field, because one endpoint serves many and
 * a profile pinned to a single model made "use the cheap one for this agent"
 * impossible to express. A vendor template answers both at once for the common cases.
 */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api } from '../api'
import { Empty, ErrorNote, Spinner } from '../components/ui'
import {
  notificationsPossible,
  notificationsWanted,
  setNotificationsWanted,
} from '../hooks/useAttention'
import { usePoll } from '../hooks/usePoll'
import { fullTime } from '../lib/format'
import { disablePush, enablePush, pushSupported } from '../lib/push'
import type {
  DiscoveredModels,
  Provider,
  ProviderKind,
  ProviderModel,
  ProviderTemplate,
  PushInfo,
  Team,
  TeamRole,
} from '../types'

interface ModelDraft {
  model: string
  label: string
  supports_tools: boolean
  /**
   * Prices are held as strings, not numbers.
   *
   * Empty has to mean "no price on file" rather than free, and a number input that
   * round-trips through `Number('')` turns that into 0 — which would make the cost
   * estimate confidently claim a job cost nothing.
   */
  price_in: string
  price_out: string
}

interface ProviderDraft {
  id: string
  label: string
  kind: string
  base_url: string
  /** The default model. Always one of `models`; the radio column sets it. */
  model: string
  models: ModelDraft[]
  secret_ref: string
  headers: string
  enabled: boolean
  supports_tools: boolean
  /** False for a profile being created, which is why discovery is unavailable. */
  existing: boolean
}

const BLANK_PROVIDER: ProviderDraft = {
  id: '',
  label: '',
  kind: 'openai_compatible',
  base_url: 'https://',
  model: '',
  models: [],
  secret_ref: '',
  headers: '{}',
  enabled: true,
  supports_tools: true,
  existing: false,
}

const BLANK_ROLE: TeamRole = { id: '', name: '', instructions: '', orchestrator: false }

function toDraft(profile: Provider): ProviderDraft {
  return {
    id: profile.id,
    label: profile.label,
    kind: profile.kind,
    base_url: profile.base_url,
    model: profile.model,
    models: profile.models.map((entry) => ({
      model: entry.model,
      label: entry.label ?? '',
      supports_tools: entry.supports_tools,
      price_in: entry.price_in === null ? '' : String(entry.price_in),
      price_out: entry.price_out === null ? '' : String(entry.price_out),
    })),
    secret_ref: profile.secret_ref ?? '',
    headers: JSON.stringify(profile.headers, null, 2),
    enabled: profile.enabled,
    supports_tools: profile.supports_tools,
    existing: true,
  }
}

/** Fill a blank draft from a vendor preset. Nothing here is unchangeable afterwards. */
function fromTemplate(template: ProviderTemplate, current: ProviderDraft): ProviderDraft {
  return {
    ...current,
    // Only fill an id and label the operator has not typed: picking a template to
    // correct the base URL of a half-filled form should not rename it.
    id: current.id || template.id,
    label: current.label || template.label,
    kind: template.kind,
    base_url: template.base_url,
    secret_ref: template.secret_ref ?? '',
    headers: JSON.stringify(template.headers, null, 2),
    models: template.models.map((model) => ({
      model,
      label: '',
      supports_tools: true,
      price_in: '',
      price_out: '',
    })),
    model: template.models[0] ?? '',
  }
}

function secretNote(profile: Provider): { text: string; tone: string } {
  if (!profile.secret_ref) return { text: 'no secret needed', tone: 'muted' }
  if (profile.secret_ok) return { text: `${profile.secret_ref} resolves`, tone: 'good' }
  return { text: `${profile.secret_ref} does not resolve`, tone: 'bad' }
}

/**
 * A typed price cell: a number, `null` for "not priced", `undefined` for nonsense.
 *
 * Blank and zero are different answers and both are legitimate — a free model costs
 * nothing, an unpriced one cannot be estimated at all — so this cannot collapse to a
 * truthiness check.
 */
function price(raw: string): number | null | undefined {
  const text = raw.trim()
  if (text === '') return null
  const value = Number(text)
  return Number.isFinite(value) && value >= 0 ? value : undefined
}

export function SettingsScreen() {
  const health = usePoll(api.health, 15_000)

  return (
    <section className="screen settings">
      <header className="screen-head">
        <div>
          <p className="eyebrow">Configuration</p>
          <h1>Settings</h1>
        </div>
      </header>

      <Providers />
      <Teams />
      <Tools />
      <Notifications />

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

/**
 * Whether this browser should reach out when a job parks — now even with no tab open.
 *
 * Two mechanisms behind one switch. The in-tab `announce()` (in `useAttention`) covers
 * a tab that is open but in the background. This panel adds Web Push: a service worker
 * plus a per-browser subscription the server fans out to, so a closed laptop or a phone
 * is told too. The permission prompt has to follow the click — Chrome ignores one that
 * did not, and a browser told "block" once cannot be asked again from script — so the
 * state is reported plainly instead of retried.
 */
function Notifications() {
  const possible = notificationsPossible()
  const canPush = pushSupported()
  const [wanted, setWanted] = useState(notificationsWanted)
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(
    possible ? Notification.permission : 'unsupported',
  )
  const [asking, setAsking] = useState(false)
  const [info, setInfo] = useState<PushInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [tested, setTested] = useState<'idle' | 'sending' | 'sent' | 'empty' | 'failed'>('idle')

  const refreshInfo = () => {
    void api
      .push()
      .then(setInfo)
      .catch(() => setInfo(null))
  }
  useEffect(refreshInfo, [])

  const serverOff = info !== null && !info.configured
  const subscribers = info?.subscribers ?? 0

  const toggle = async (on: boolean) => {
    setProblem(null)
    setTested('idle')
    setNotificationsWanted(on)
    setWanted(on)

    if (!on) {
      // Untick means "stop reaching me here" — drop the background subscription so the
      // server stops fanning out to this browser, not just the in-tab toast.
      setBusy(true)
      try {
        await disablePush()
      } catch {
        /* best effort; a dead row is pruned server-side on its next failed send */
      } finally {
        setBusy(false)
        refreshInfo()
      }
      return
    }

    // Turning on. The permission prompt must ride on this click.
    let granted = possible && Notification.permission === 'granted'
    if (possible && Notification.permission === 'default') {
      setAsking(true)
      try {
        const result = await Notification.requestPermission()
        setPermission(result)
        granted = result === 'granted'
      } finally {
        setAsking(false)
      }
    }
    // In-tab notifications now work off `wanted` alone. Background push is the extra
    // step, and only where the browser and server both support it.
    if (!granted || !canPush || serverOff) return

    setBusy(true)
    try {
      const reason = await enablePush()
      if (reason) setProblem(reason)
    } catch {
      setProblem('Could not subscribe this browser for push. Try again, or reload the page.')
    } finally {
      setBusy(false)
      refreshInfo()
    }
  }

  const sendTest = async () => {
    setTested('sending')
    try {
      const result = await api.pushTest()
      setTested(result.sent > 0 ? 'sent' : 'empty')
    } catch {
      setTested('failed')
    } finally {
      refreshInfo()
    }
  }

  const on = wanted && permission === 'granted'
  const background = on && canPush && !serverOff

  const note = !possible
    ? 'This browser has no notification API, so nothing can be sent.'
    : permission === 'denied'
      ? 'The browser is blocking notifications for this site. That has to be undone in its site settings — a page cannot ask again.'
      : permission === 'granted'
        ? !wanted
          ? 'Granted, but switched off here.'
          : !canPush
            ? 'On for an open tab. This browser cannot hold a background subscription, so it will not be told once every tab is closed.'
            : serverOff
              ? 'On for an open tab. Background delivery is switched off on the server (AGENT_HUB_PUSH), so a closed browser cannot be reached.'
              : 'On. This browser is told the moment a job needs an approval or an answer, even with every tab closed — the message names the job and opens it when tapped. On iPhone, add this site to the home screen first, or it cannot receive them.'
        : 'The browser will ask once you switch this on.'

  const testNote =
    tested === 'sent'
      ? `Sent to ${subscribers} subscribed ${subscribers === 1 ? 'browser' : 'browsers'}.`
      : tested === 'empty'
        ? 'Nothing was sent — no browser is subscribed yet.'
        : tested === 'failed'
          ? 'The test could not be sent.'
          : `${subscribers} ${subscribers === 1 ? 'browser is' : 'browsers are'} subscribed.`

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Tell me when a job stops</h2>
        <span className={`pill pill-${on ? 'good' : 'muted'}`}>{on ? 'on' : 'off'}</span>
      </header>

      <p className="panel-note">
        A job that parks itself on a question at two in the morning is invisible until
        someone looks at this app. Switch this on and this browser — laptop or phone — is
        told the moment a job needs you, even with the site closed. It fires on that one
        signal only, never for anything else, and anyone who opens this site can turn it
        on for their own device. The message lands on the lock screen, which everyone here
        is already behind the site&apos;s password to reach.
      </p>

      <label className="check">
        <input
          type="checkbox"
          checked={wanted}
          disabled={!possible || asking || busy || permission === 'denied'}
          onChange={(event) => void toggle(event.target.checked)}
        />
        <span>Notify me when a job needs an answer</span>
      </label>

      {problem ? <ErrorNote>{problem}</ErrorNote> : null}
      <p className="field-note">{note}</p>

      {background ? (
        <div className="form-foot">
          <button
            type="button"
            className="button ghost"
            onClick={() => void sendTest()}
            disabled={tested === 'sending'}
          >
            {tested === 'sending' ? 'Sending…' : 'Send a test'}
          </button>
          <span className="muted">{testNote}</span>
        </div>
      ) : null}
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
        their job&apos;s workspace. Every call appears inline in that job&apos;s stream with
        its output; commands matching a guardrail wait for an approval in both modes.
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
  const [kinds, setKinds] = useState<ProviderKind[]>([])
  const [templates, setTemplates] = useState<ProviderTemplate[]>([])
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [found, setFound] = useState<DiscoveredModels | null>(null)
  const [discovering, setDiscovering] = useState(false)

  const providers = data ?? []

  // The catalogues change with a deploy, not with a click, so one fetch is enough.
  useEffect(() => {
    void Promise.all([api.providerKinds(), api.providerTemplates()])
      .then(([loadedKinds, loadedTemplates]) => {
        setKinds(loadedKinds)
        setTemplates(loadedTemplates)
      })
      .catch(() => undefined)
  }, [])

  const kind = kinds.find((entry) => entry.id === draft?.kind) ?? null

  const open = (next: ProviderDraft | null) => {
    setDraft(next)
    setFound(null)
    setFormError(null)
  }

  const patchModel = (index: number, changes: Partial<ModelDraft>) =>
    setDraft((current) => {
      if (!current) return current
      const models = current.models.map((entry, position) =>
        position === index ? { ...entry, ...changes } : entry,
      )
      // Renaming the model that was the default keeps it the default, rather than
      // silently pointing the profile at a model id that no longer exists.
      const renamed =
        changes.model !== undefined && current.models[index]?.model === current.model
          ? changes.model
          : current.model
      return { ...current, models, model: renamed }
    })

  const removeModel = (index: number) =>
    setDraft((current) => {
      if (!current) return current
      const gone = current.models[index]?.model
      const models = current.models.filter((_, position) => position !== index)
      return {
        ...current,
        models,
        model: gone === current.model ? (models[0]?.model ?? '') : current.model,
      }
    })

  const addModels = (names: string[]) =>
    setDraft((current) => {
      if (!current) return current
      const have = new Set(current.models.map((entry) => entry.model))
      const added = names
        .filter((name) => !have.has(name))
        .map((name) => ({
          model: name,
          label: '',
          supports_tools: true,
          price_in: '',
          price_out: '',
        }))
      const models = [...current.models, ...added]
      return { ...current, models, model: current.model || (models[0]?.model ?? '') }
    })

  const discover = async () => {
    if (!draft) return
    setDiscovering(true)
    setFormError(null)
    try {
      setFound(await api.discoverModels(draft.id))
    } catch (cause) {
      setFormError(
        cause instanceof ApiError
          ? `discovery failed: ${cause.message}`
          : 'could not reach that endpoint',
      )
    } finally {
      setDiscovering(false)
    }
  }

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

    const models: ProviderModel[] = []
    let unparseable = false
    for (const entry of draft.models) {
      const model = entry.model.trim()
      if (model === '') continue
      const priceIn = price(entry.price_in)
      const priceOut = price(entry.price_out)
      if (priceIn === undefined || priceOut === undefined) {
        unparseable = true
        continue
      }
      models.push({
        model,
        label: entry.label.trim() || null,
        supports_tools: entry.supports_tools,
        price_in: priceIn,
        price_out: priceOut,
      })
    }

    if (unparseable) {
      setFormError('a price must be dollars per million tokens, or blank for "not priced"')
      setBusy(false)
      return
    }

    if (models.length === 0) {
      // The server enforces this too, but a profile with no model saves cleanly and
      // then fails on the first call of every job that uses it — worth catching here.
      setFormError('add at least one model; a profile with none cannot run a job')
      setBusy(false)
      return
    }

    try {
      await api.saveProvider({
        id: draft.id.trim(),
        label: draft.label.trim(),
        kind: draft.kind,
        base_url: draft.base_url.trim(),
        model: draft.model.trim() || models[0].model,
        models,
        secret_ref: draft.secret_ref.trim() || null,
        headers,
        enabled: draft.enabled,
        supports_tools: draft.supports_tools,
      })
      open(null)
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
          onClick={() => open(draft === null ? { ...BLANK_PROVIDER } : null)}
        >
          {draft === null ? 'Add profile' : 'Cancel'}
        </button>
      </header>

      {error ? <ErrorNote>{error}</ErrorNote> : null}
      {formError ? <ErrorNote>{formError}</ErrorNote> : null}

      {draft !== null ? (
        <form className="form" onSubmit={save}>
          {!draft.existing ? (
            <label className="field">
              <span>Start from</span>
              <select
                value=""
                onChange={(event) => {
                  const template = templates.find((entry) => entry.id === event.target.value)
                  if (template) setDraft((current) => (current ? fromTemplate(template, current) : current))
                }}
              >
                <option value="">A preset, or fill it in by hand…</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.label}
                    {template.models.length > 0 ? ` — ${template.models.length} models` : ''}
                  </option>
                ))}
              </select>
              <small>
                Presets only fill the form. Every field stays editable, and nothing is saved
                until you say so.
              </small>
            </label>
          ) : null}

          <div className="field-row">
            <label className="field">
              <span>Id</span>
              <input
                value={draft.id}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
                placeholder="agentrouter"
                pattern="[A-Za-z0-9._\-]+"
                readOnly={draft.existing}
                required
              />
              {draft.existing ? <small>Ids are permanent — jobs reference them.</small> : null}
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
              <span>API type</span>
              <select
                value={draft.kind}
                onChange={(event) => setDraft({ ...draft, kind: event.target.value })}
              >
                {kinds.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                  </option>
                ))}
              </select>
              {kind ? (
                <small>
                  {kind.detail} Auth: <code>{kind.auth}</code>
                </small>
              ) : null}
            </label>
            <label className="field">
              <span>Base URL</span>
              <input
                value={draft.base_url}
                onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
                placeholder="https://api.example.com/v1"
                required
              />
              <small>
                No trailing path beyond the version prefix — the adapter appends its own
                endpoint.
              </small>
            </label>
          </div>

          <fieldset className="models-editor">
            <legend>
              Models
              <span className="muted">
                {draft.models.length === 0
                  ? 'none yet'
                  : `${draft.models.length} listed · default is ${draft.model || '—'}`}
              </span>
            </legend>

            {draft.models.length === 0 ? (
              <p className="field-note">
                A provider serves many models and a job picks one per agent. Add the ones you
                want offered.
              </p>
            ) : (
              <table className="models-table">
                <thead>
                  <tr>
                    <th scope="col" className="right">
                      Default
                    </th>
                    <th scope="col">Model id</th>
                    <th scope="col">Label (optional)</th>
                    <th scope="col" className="right">
                      $/Mtok in
                    </th>
                    <th scope="col" className="right">
                      $/Mtok out
                    </th>
                    <th scope="col">Tools</th>
                    <th scope="col" aria-label="Remove" />
                  </tr>
                </thead>
                <tbody>
                  {draft.models.map((entry, index) => (
                    <tr key={index}>
                      <td className="right">
                        <input
                          type="radio"
                          name="default-model"
                          aria-label={`Make ${entry.model || 'this model'} the default`}
                          checked={draft.model === entry.model && entry.model !== ''}
                          onChange={() => setDraft({ ...draft, model: entry.model })}
                        />
                      </td>
                      <td>
                        <input
                          className="mono"
                          value={entry.model}
                          onChange={(event) => patchModel(index, { model: event.target.value })}
                          placeholder="gpt-5.2-mini"
                          aria-label={`Model id ${index + 1}`}
                        />
                      </td>
                      <td>
                        <input
                          value={entry.label}
                          onChange={(event) => patchModel(index, { label: event.target.value })}
                          placeholder="Cheap and fast"
                          aria-label={`Label for model ${index + 1}`}
                        />
                      </td>
                      <td className="right">
                        <input
                          className="mono price"
                          value={entry.price_in}
                          onChange={(event) => patchModel(index, { price_in: event.target.value })}
                          placeholder="—"
                          inputMode="decimal"
                          aria-label={`Prompt price for ${entry.model || `model ${index + 1}`}`}
                        />
                      </td>
                      <td className="right">
                        <input
                          className="mono price"
                          value={entry.price_out}
                          onChange={(event) => patchModel(index, { price_out: event.target.value })}
                          placeholder="—"
                          inputMode="decimal"
                          aria-label={`Completion price for ${entry.model || `model ${index + 1}`}`}
                        />
                      </td>
                      <td>
                        <label className="check tight">
                          <input
                            type="checkbox"
                            checked={entry.supports_tools}
                            disabled={!draft.supports_tools}
                            onChange={(event) =>
                              patchModel(index, { supports_tools: event.target.checked })
                            }
                          />
                          <span className="sr-only">
                            {entry.model || `model ${index + 1}`} can call tools
                          </span>
                        </label>
                      </td>
                      <td className="right">
                        <button
                          type="button"
                          className="icon-button"
                          aria-label={`Remove ${entry.model || `model ${index + 1}`}`}
                          onClick={() => removeModel(index)}
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <p className="field-note">
              Prices are dollars per million tokens, as the vendor lists them. Leave one
              blank and this build will not guess: that model's tokens are reported as
              unpriced rather than folded in at zero, so a cost estimate is never quietly
              too low.
            </p>

            <div className="models-foot">
              <button
                type="button"
                className="button ghost"
                onClick={() => addModels([''])}
                disabled={draft.models.some((entry) => entry.model === '')}
              >
                Add model
              </button>
              <button
                type="button"
                className="button ghost"
                onClick={() => void discover()}
                disabled={!draft.existing || discovering}
                title={
                  draft.existing
                    ? `Ask the endpoint what it serves${kind?.models_path ? ` (GET ${kind.models_path})` : ''}`
                    : 'Save the profile first — discovery calls the endpoint with its stored credentials'
                }
              >
                {discovering ? 'Asking the endpoint…' : 'Discover models'}
              </button>
              {!draft.existing ? (
                <span className="muted">Discovery needs a saved profile with its secret.</span>
              ) : null}
            </div>

            {found ? (
              <div className="discovered">
                <p className="discovered-head">
                  {found.count} model{found.count === 1 ? '' : 's'} on the endpoint.
                  {found.models.some((entry) => !entry.known) ? (
                    <button
                      type="button"
                      className="button ghost"
                      onClick={() =>
                        addModels(
                          found.models.filter((entry) => !entry.known).map((entry) => entry.model),
                        )
                      }
                    >
                      Add all {found.models.filter((entry) => !entry.known).length} new
                    </button>
                  ) : null}
                  <button type="button" className="button ghost" onClick={() => setFound(null)}>
                    Dismiss
                  </button>
                </p>
                <ul className="discovered-list">
                  {found.models.map((entry) => {
                    const listed =
                      entry.known || draft.models.some((row) => row.model === entry.model)
                    return (
                      <li key={entry.model}>
                        <button
                          type="button"
                          className={`chip ${listed ? 'chip-on' : ''}`}
                          disabled={listed}
                          onClick={() => addModels([entry.model])}
                          title={listed ? 'Already listed' : 'Add to this profile'}
                        >
                          <span className="mono">{entry.model}</span>
                          {listed ? ' ✓' : ' +'}
                        </button>
                      </li>
                    )
                  })}
                </ul>
                <p className="field-note">
                  Nothing was saved. Pick what you want offered — a gateway listing hundreds of
                  models would turn the job form into a haystack.
                </p>
              </div>
            ) : null}
          </fieldset>

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

          <label className="check">
            <input
              type="checkbox"
              checked={draft.supports_tools}
              onChange={(event) => setDraft({ ...draft, supports_tools: event.target.checked })}
            />
            <span>
              Endpoint accepts tool calls — off for one that rejects the <code>tools</code>{' '}
              parameter outright, which makes its agents talk instead of run
            </span>
          </label>

          <div className="form-foot">
            <button type="button" className="button ghost" onClick={() => open(null)}>
              Cancel
            </button>
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
              <th scope="col">Models</th>
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
              const others = profile.models.filter((entry) => entry.model !== profile.model)
              return (
                <tr key={profile.id}>
                  <th scope="row">
                    <span className="cell-title">{profile.label}</span>
                    <span className="cell-sub mono">
                      {profile.id} · {profile.kind}
                    </span>
                  </th>
                  <td>
                    <span className="cell-title mono">{profile.base_url}</span>
                    <span className="cell-sub">
                      {profile.supports_tools ? 'tool calling' : 'text only — agents cannot run commands'}
                    </span>
                  </td>
                  <td>
                    <span className="cell-title mono">{profile.model}</span>
                    <span className="cell-sub">
                      {others.length === 0
                        ? 'the only one listed'
                        : `+ ${others.length} more: ${others
                            .slice(0, 3)
                            .map((entry) => entry.model)
                            .join(', ')}${others.length > 3 ? '…' : ''}`}
                    </span>
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
                      onClick={() => open(toDraft(profile))}
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
  const [providers, setProviders] = useState<Provider[]>([])
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const teams = data ?? []

  // A template can pin a role to a provider, so the editor needs the list. Failure is
  // survivable: the selects fall back to "the job's provider", which is the default.
  useEffect(() => {
    void api
      .providers()
      .then((loaded) => setProviders(loaded.filter((profile) => profile.enabled)))
      .catch(() => undefined)
  }, [])

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
                <div className="field-row">
                  <label className="field">
                    <span>Provider</span>
                    <select
                      value={role.provider_id ?? ''}
                      onChange={(event) =>
                        patch(index, { provider_id: event.target.value || null, model: null })
                      }
                    >
                      <option value="">The job&apos;s provider</option>
                      {providers.map((profile) => (
                        <option key={profile.id} value={profile.id}>
                          {profile.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>Model</span>
                    <select
                      value={role.model ?? ''}
                      onChange={(event) => patch(index, { model: event.target.value || null })}
                    >
                      <option value="">
                        {role.provider_id ? "That provider's default" : "The job's model"}
                      </option>
                      {(
                        providers.find((profile) => profile.id === role.provider_id)?.models ?? []
                      ).map((entry) => (
                        <option key={entry.model} value={entry.model}>
                          {entry.label ?? entry.model}
                        </option>
                      ))}
                    </select>
                    <small>
                      A pin the template carries, so a mixed-model team does not have to be
                      re-picked on every job. New Job can still override it.
                    </small>
                  </label>
                </div>
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
                  {role.provider_id || role.model ? (
                    <span className="role-pin mono">
                      {role.model ?? role.provider_id}
                      {role.model && role.provider_id ? ` on ${role.provider_id}` : ''}
                    </span>
                  ) : null}
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
