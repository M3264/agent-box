import type { ReactNode } from 'react'
import { label, tone } from '../lib/format'

export function StatusPill({ status, children }: { status: string; children?: ReactNode }) {
  return (
    <span className={`pill pill-${tone(status)}`}>
      <span className="dot" aria-hidden />
      {children ?? label(status)}
    </span>
  )
}

export function Progress({ done, total }: { done: number; total: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  return (
    <div
      className="progress"
      role="progressbar"
      aria-valuenow={done}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-label={`${done} of ${total} phases complete`}
    >
      <div className="progress-fill" style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {hint ? <p className="empty-hint">{hint}</p> : null}
    </div>
  )
}

export function Spinner({ label: text = 'Loading' }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden />
      {text}
    </div>
  )
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="error-note" role="alert">
      {children}
    </div>
  )
}

export function Avatar({ agent }: { agent: string }) {
  return (
    <span className={`avatar avatar-${agent}`} aria-hidden>
      {agent.slice(0, 1).toUpperCase()}
    </span>
  )
}
