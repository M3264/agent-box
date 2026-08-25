/** Periodic fetch for the screens that are lists rather than one live job.
 *
 * A socket per job would be the wrong tool for the jobs list and the approvals
 * inbox: they aggregate across jobs, and a several-second refresh is indistinguishable
 * from live at that granularity. Polling pauses while the tab is hidden.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'

export interface Poll<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => Promise<void>
}

export function usePoll<T>(fetcher: () => Promise<T>, intervalMs = 4000): Poll<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)
  const load = useRef(fetcher)
  load.current = fetcher

  const reload = useCallback(async () => {
    try {
      const value = await load.current()
      if (!alive.current) return
      setData(value)
      setError(null)
    } catch (cause) {
      if (!alive.current) return
      setError(cause instanceof ApiError ? cause.message : 'request failed')
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void reload()

    const tick = () => {
      if (document.visibilityState === 'visible') void reload()
    }
    const timer = window.setInterval(tick, intervalMs)
    document.addEventListener('visibilitychange', tick)

    return () => {
      alive.current = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [intervalMs, reload])

  return { data, error, loading, reload }
}
