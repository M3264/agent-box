/**
 * The icon set.
 *
 * Inline SVG rather than a font or a sprite: there are a dozen of them, they are all
 * one path or two, and a webfont would be another network round trip before the first
 * paint of a screen that is mostly text anyway.
 *
 * All of them are 1.4px hairline strokes on a 24-unit grid in `currentColor`, which is
 * what keeps them at the same visual weight as the rules and borders they sit among.
 * None of them carry meaning on their own — every icon in the app is beside its label
 * or has one on the button, so they are marked `aria-hidden` here, once.
 */

import type { ReactNode } from 'react'

function Glyph({ children }: { children: ReactNode }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable="false"
    >
      {children}
    </svg>
  )
}

export function IconJobs() {
  return (
    <Glyph>
      <rect x="3.5" y="4.5" width="17" height="5" rx="1.5" />
      <rect x="3.5" y="14.5" width="17" height="5" rx="1.5" />
    </Glyph>
  )
}

/** A raised flag: something is waiting, and it is waiting for a person. */
export function IconAttention() {
  return (
    <Glyph>
      <path d="M6 3.5v17" />
      <path d="M6 4.5h11l-2.2 3.8L17 12H6" />
    </Glyph>
  )
}

export function IconSettings() {
  return (
    <Glyph>
      <path d="M3.5 7.5h17M3.5 16.5h17" />
      <circle cx="9" cy="7.5" r="2.2" />
      <circle cx="15.5" cy="16.5" r="2.2" />
    </Glyph>
  )
}

export function IconPlus() {
  return (
    <Glyph>
      <path d="M12 5v14M5 12h14" />
    </Glyph>
  )
}

export function IconSearch() {
  return (
    <Glyph>
      <circle cx="10.5" cy="10.5" r="6" />
      <path d="M15 15l4.5 4.5" />
    </Glyph>
  )
}

export function IconPlan() {
  return (
    <Glyph>
      <path d="M4 6.5l2 2 3-3.5" />
      <path d="M4 16.5l2 2 3-3.5" />
      <path d="M12.5 7h7.5M12.5 17h7.5" />
    </Glyph>
  )
}

export function IconAgents() {
  return (
    <Glyph>
      <circle cx="9" cy="9" r="3.2" />
      <path d="M3.8 19.5c0-3 2.3-4.8 5.2-4.8s5.2 1.8 5.2 4.8" />
      <path d="M15.5 6.2a3.2 3.2 0 0 1 0 6.1M17 15.2c2.1.5 3.2 2.1 3.2 4.3" />
    </Glyph>
  )
}

/** A meter, for tokens and money. */
export function IconTokens() {
  return (
    <Glyph>
      <path d="M4 19.5h16" />
      <path d="M7 19.5v-5M12 19.5V7M17 19.5v-8.5" />
    </Glyph>
  )
}

export function IconFiles() {
  return (
    <Glyph>
      <path d="M6 3.5h7l5 5v12H6z" />
      <path d="M13 3.5v5h5" />
    </Glyph>
  )
}

export function IconEvents() {
  return (
    <Glyph>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12l3 2" />
    </Glyph>
  )
}

export function IconSend() {
  return (
    <Glyph>
      <path d="M12 19.5V5" />
      <path d="M6 11l6-6 6 6" />
    </Glyph>
  )
}

export function IconClose() {
  return (
    <Glyph>
      <path d="M6 6l12 12M18 6L6 18" />
    </Glyph>
  )
}

export function IconChevron() {
  return (
    <Glyph>
      <path d="M9 6l6 6-6 6" />
    </Glyph>
  )
}

export function IconCommand() {
  return (
    <Glyph>
      <path d="M5 8.5l3.5 3.5L5 15.5" />
      <path d="M11 16h8" />
    </Glyph>
  )
}

export function IconAsk() {
  return (
    <Glyph>
      <path d="M4.5 5.5h15v10h-9l-4 3.5z" />
      <path d="M10 8.8a2 2 0 1 1 2 2v1.2" />
      <path d="M12 13.9v.1" />
    </Glyph>
  )
}

export function IconGate() {
  return (
    <Glyph>
      <path d="M12 3.5l8 3.2v5.6c0 4.1-3.2 7-8 8.2-4.8-1.2-8-4.1-8-8.2V6.7z" />
      <path d="M8.8 12l2.2 2.2 4.2-4.4" />
    </Glyph>
  )
}
