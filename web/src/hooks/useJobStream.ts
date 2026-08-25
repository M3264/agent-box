/**
 * Live job state over one WebSocket.
 *
 * The v1 UI fetched once on open and never subscribed, so progress only appeared
 * if you navigated away and back. Here the snapshot supplies the starting state
 * *and* the cursor to open the stream at, so there is no gap between the two, and
 * every reconnect replays from the last id seen rather than from scratch.
 *
 * Event payloads are applied locally where they carry enough to do so — status,
 * phase transitions, agent state — and a coalesced snapshot refetch covers the
 * rest, where the server holds ids or joins the client does not (a `plan` event
 * announces phases whose ids only the database knows).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from '../api'
import { socketUrl } from '../api'
import type { Agent, JobEvent, JobSnapshot, Phase } from '../types'

export type Connection = 'connecting' | 'live' | 'reconnecting' | 'closed' | 'gone'

const TERMINAL = new Set(['complete', 'error', 'stopped'])
const RECONNECT_STEPS = [500, 1000, 2000, 5000, 10_000]
const REFETCH_ON = new Set(['plan', 'phase', 'approval', 'artifact', 'guidance', 'message', 'tool_call'])

interface Frame {
  type: 'hello' | 'event' | 'ping' | 'end' | 'error'
  event?: JobEvent
  status?: string
  detail?: string
}

export interface JobStream {
  job: JobSnapshot | null
  events: JobEvent[]
  connection: Connection
  error: string | null
  /** Re-read the job from the server; used after operator actions. */
  refresh: () => Promise<void>
}

/** Whether a job can still change on its own. */
export function isLive(status: string | undefined): boolean {
  return status !== undefined && !TERMINAL.has(status)
}

export function useJobStream(jobId: string): JobStream {
  const [job, setJob] = useState<JobSnapshot | null>(null)
  const [events, setEvents] = useState<JobEvent[]>([])
  const [connection, setConnection] = useState<Connection>('connecting')
  const [error, setError] = useState<string | null>(null)

  const cursor = useRef(0)
  const socket = useRef<WebSocket | null>(null)
  const attempt = useRef(0)
  const reconnectTimer = useRef<number | null>(null)
  const refetchTimer = useRef<number | null>(null)
  const alive = useRef(true)
  /** Set when the server says the stream is over, so onclose does not reconnect. */
  const finished = useRef(false)

  const merge = useCallback((snapshot: JobSnapshot) => {
    // Events deliberately excluded: the stream owns them, and a second copy would
    // reintroduce ids the cursor has already passed.
    setJob((current) =>
      current === null
        ? snapshot
        : {
            ...current,
            status: snapshot.status,
            paused: snapshot.paused,
            error: snapshot.error,
            result: snapshot.result,
            updated_at: snapshot.updated_at,
            team: snapshot.team,
            plan: snapshot.plan,
            artifacts: snapshot.artifacts,
            messages: snapshot.messages,
            approvals: snapshot.approvals,
            tool_calls: snapshot.tool_calls,
            sandbox: snapshot.sandbox,
          },
    )
  }, [])

  /** One refetch for a burst of events instead of one per event. */
  const scheduleRefetch = useCallback(() => {
    if (refetchTimer.current !== null) window.clearTimeout(refetchTimer.current)
    refetchTimer.current = window.setTimeout(() => {
      refetchTimer.current = null
      void api
        .job(jobId)
        .then((snapshot) => {
          if (alive.current) merge(snapshot)
        })
        .catch(() => undefined)
    }, 250)
  }, [jobId, merge])

  const apply = useCallback(
    (event: JobEvent) => {
      const payload = event.payload as Record<string, unknown>

      switch (event.kind) {
        case 'status': {
          const status = String(payload.status ?? '')
          if (status === 'paused') {
            setJob((current) => (current ? { ...current, paused: true } : current))
          } else {
            setJob((current) =>
              current
                ? { ...current, paused: false, status: status as JobSnapshot['status'] }
                : current,
            )
          }
          if (TERMINAL.has(status)) scheduleRefetch()
          break
        }
        case 'phase': {
          setJob((current) =>
            current
              ? {
                  ...current,
                  plan: current.plan.map((phase) =>
                    phase.id === payload.phase_id
                      ? { ...phase, status: String(payload.status) as Phase['status'] }
                      : phase,
                  ),
                }
              : current,
          )
          break
        }
        case 'agent_state': {
          const name = event.source
          if (!name) break
          setJob((current) => {
            if (!current) return current
            const updated: Agent = {
              job_id: current.id,
              agent: name,
              status: String(payload.status ?? ''),
              current_action: (payload.current_action as string | null) ?? null,
              updated_at: event.created_at,
            }
            const known = current.team.some((member) => member.agent === name)
            const team = known
              ? current.team.map((member) => (member.agent === name ? updated : member))
              : [...current.team, updated].sort((a, b) => a.agent.localeCompare(b.agent))
            return { ...current, team }
          })
          break
        }
        case 'result': {
          setJob((current) =>
            current ? { ...current, result: { content: String(payload.content ?? '') } } : current,
          )
          break
        }
        case 'error': {
          setJob((current) => (current ? { ...current, error: String(payload.error ?? '') } : current))
          break
        }
        default:
          break
      }

      if (REFETCH_ON.has(event.kind)) scheduleRefetch()
    },
    [scheduleRefetch],
  )

  const connect = useCallback(() => {
    if (!alive.current || finished.current) return
    setConnection((current) => (current === 'live' ? current : 'connecting'))

    const ws = new WebSocket(socketUrl(`/ws/jobs/${jobId}?after=${cursor.current}`))
    socket.current = ws

    ws.onopen = () => {
      if (!alive.current) return
      attempt.current = 0
      setConnection('live')
    }

    ws.onmessage = (message: MessageEvent<string>) => {
      if (!alive.current) return
      let frame: Frame
      try {
        frame = JSON.parse(message.data) as Frame
      } catch {
        return
      }

      if (frame.type === 'error') {
        finished.current = true
        setConnection('gone')
        setError(frame.detail ?? 'job not found')
        return
      }
      if (frame.type === 'end') {
        finished.current = true
        setConnection('closed')
        return
      }
      if (frame.type === 'event' && frame.event) {
        const incoming = frame.event
        if (incoming.id <= cursor.current) return // overlap from a replay
        cursor.current = incoming.id
        setEvents((current) => [...current, incoming])
        apply(incoming)
      }
    }

    ws.onclose = () => {
      if (!alive.current || socket.current !== ws) return
      socket.current = null
      if (finished.current) return

      const delay = RECONNECT_STEPS[Math.min(attempt.current, RECONNECT_STEPS.length - 1)] ?? 10_000
      attempt.current += 1
      setConnection('reconnecting')
      reconnectTimer.current = window.setTimeout(connect, delay)
    }
  }, [apply, jobId])

  const refresh = useCallback(async () => {
    try {
      const snapshot = await api.job(jobId)
      if (!alive.current) return
      setError(null)
      if (cursor.current === 0) {
        // First load: the snapshot's cursor is exactly where the stream resumes.
        cursor.current = snapshot.cursor
        setEvents(snapshot.events)
        setJob(snapshot)
      } else {
        merge(snapshot)
      }
    } catch (cause) {
      if (!alive.current) return
      setError(cause instanceof ApiError ? cause.message : 'could not load this job')
      if (cause instanceof ApiError && cause.status === 404) {
        finished.current = true
        setConnection('gone')
      }
    }
  }, [jobId, merge])

  useEffect(() => {
    alive.current = true
    finished.current = false
    cursor.current = 0
    attempt.current = 0
    setJob(null)
    setEvents([])
    setError(null)
    setConnection('connecting')

    void refresh().then(() => {
      if (alive.current) connect()
    })

    return () => {
      alive.current = false
      if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current)
      if (refetchTimer.current !== null) window.clearTimeout(refetchTimer.current)
      const ws = socket.current
      socket.current = null
      ws?.close()
    }
  }, [connect, refresh])

  return useMemo(
    () => ({ job, events, connection, error, refresh }),
    [job, events, connection, error, refresh],
  )
}
