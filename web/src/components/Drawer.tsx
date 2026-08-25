/**
 * A side panel that opens over the content instead of replacing it.
 *
 * PLAN.md §4: "Agent inspector opens deliberately instead of occupying the entire
 * screen." On narrow screens it becomes a bottom sheet, which is the same idea
 * where there is no room for a side panel.
 */

import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'

interface DrawerProps {
  open: boolean
  title: string
  subtitle?: ReactNode
  onClose: () => void
  children: ReactNode
}

export function Drawer({ open, title, subtitle, onClose, children }: DrawerProps) {
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    // Focus the panel so Escape and screen readers land in the right place.
    panel.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="drawer-root">
      <button type="button" className="drawer-scrim" aria-label="Close panel" onClick={onClose} />
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
      >
        <header className="drawer-head">
          <div>
            <h2>{title}</h2>
            {subtitle ? <p className="drawer-sub">{subtitle}</p> : null}
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </div>
    </div>
  )
}
