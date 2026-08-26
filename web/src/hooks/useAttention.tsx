/**
 * One poll of "is anything waiting for me", shared by everything that asks.
 *
 * The rail badge, the Attention screen and the command palette all want the same
 * answer, and three independent polls would let the badge disagree with the list it
 * links to. So it is fetched once here and read from context.
 *
 * This is also where browser notifications live, for one reason: the rise of
 * `counts.blocking` is the only signal in the app that means "a job stopped and cannot
 * continue without you", and it is only observable by comparing two polls. A job that
 * parks itself at 02:00 on a question is otherwise invisible until someone looks.
 */

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import { usePoll } from './usePoll'
import type { Attention } from '../types'

const EMPTY_COUNTS = { approvals: 0, questions: 0, messages: 0, blocking: 0 }

/** Opt-in, and remembered. Notifications nobody asked for are worse than none. */
const NOTIFY_KEY = 'agent-hub.notify'

export interface AttentionState {
  data: Attention | null
  error: string | null
  counts: Attention['counts']
  reload: () => Promise<void>
}

const Context = createContext<AttentionState>({
  data: null,
  error: null,
  counts: EMPTY_COUNTS,
  reload: async () => undefined,
})

export function notificationsWanted(): boolean {
  return window.localStorage.getItem(NOTIFY_KEY) === '1'
}

export function setNotificationsWanted(on: boolean): void {
  window.localStorage.setItem(NOTIFY_KEY, on ? '1' : '0')
}

/** Whether the browser can notify at all, so Settings can say why not. */
export function notificationsPossible(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window
}

/** One line naming the oldest thing that is actually blocking a job. */
function headline(data: Attention): string {
  const gate = data.approvals[0]
  const ask = data.questions[0]
  if (ask && (!gate || ask.created_at <= gate.created_at)) {
    return `${ask.agent} is asking: ${ask.question}`
  }
  if (gate) return `${gate.agent ?? 'An agent'} needs approval: ${gate.action}`
  return 'Something is waiting on you.'
}

function announce(data: Attention): void {
  if (!notificationsPossible() || !notificationsWanted()) return
  if (Notification.permission !== 'granted') return
  // Only when the operator is not already looking at the app: a notification for
  // something visible on screen is pure noise.
  if (!document.hidden) return
  const count = data.counts.blocking
  new Notification(count === 1 ? 'A job needs you' : `${count} jobs need you`, {
    body: headline(data),
    // One tag, so five polls in a row replace each other in the tray instead of
    // stacking into a wall of near-identical notifications.
    tag: 'agent-hub-attention',
  })
}

export function AttentionProvider({ children }: { children: ReactNode }) {
  const poll = usePoll(() => api.attention(), 8000)
  const [mirror, setMirror] = useState<Attention | null>(null)
  /** The previous blocking count. Null until the first poll lands. */
  const previous = useRef<number | null>(null)

  useEffect(() => {
    if (poll.data === null) return
    setMirror(poll.data)
    const blocking = poll.data.counts.blocking
    const before = previous.current
    previous.current = blocking
    // A rise, not a level: a job that has been blocked for an hour should not
    // re-notify every eight seconds, and the first poll after a page load is not news.
    if (before !== null && blocking > before) announce(poll.data)
  }, [poll.data])

  const { error, reload } = poll
  const value = useMemo(
    () => ({ data: mirror, error, counts: mirror?.counts ?? EMPTY_COUNTS, reload }),
    [mirror, error, reload],
  )

  return <Context.Provider value={value}>{children}</Context.Provider>
}

export function useAttention(): AttentionState {
  return useContext(Context)
}
