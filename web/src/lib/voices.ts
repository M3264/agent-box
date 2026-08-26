/**
 * A colour per speaker, kept strictly apart from the six status tones.
 *
 * The status tones answer "how is this going" and they are the only colours allowed to
 * do that. But a transcript has a second question — "who is talking" — and reusing
 * green for the coder would make a passing build and a particular agent the same
 * signal. So this is a separate, quieter palette, spent only on an avatar ring, an
 * agent's name, and the hairline down the left edge of its turn. Nothing here ever
 * carries state.
 *
 * The hues are deliberately chosen away from `--good`, `--warn` and `--bad`: violets,
 * blues, teals and dusty pinks, at a lightness that reads on `--panel` without
 * competing with `--ink`.
 */

import type { CSSProperties } from 'react'

const VOICES: Record<string, string> = {
  operator: '#f6f1e8',
  manager: '#c9b3f2',
  orchestrator: '#c9b3f2',
  architect: '#9fc2ef',
  coder: '#8ed3c0',
  developer: '#8ed3c0',
  tester: '#f0b0c0',
  reviewer: '#d8cf94',
  researcher: '#e3b48f',
  system: '#6f6a63',
}

/** For roles a custom team invented. Same list, minus the two that read as operator. */
const SPARE = ['#c9b3f2', '#9fc2ef', '#8ed3c0', '#f0b0c0', '#d8cf94', '#e3b48f']

/**
 * The colour for a speaker, as a CSS colour.
 *
 * Unknown names are hashed rather than defaulted, because a team template can name its
 * roles anything and "two agents in the same grey" loses the only thing this palette is
 * for. The hash is stable, so a role keeps its colour across reloads and across jobs.
 */
export function voice(agent: string | null | undefined): string {
  if (!agent) return 'var(--ink-2)'
  const known = VOICES[agent.toLowerCase()]
  if (known) return known
  let hash = 0
  for (let index = 0; index < agent.length; index += 1) {
    hash = (hash * 31 + agent.charCodeAt(index)) >>> 0
  }
  return SPARE[hash % SPARE.length]
}

/**
 * The inline style that hands the voice to CSS, which reads it as `var(--who)`.
 *
 * The cast is unavoidable: `CSSProperties` has no index signature for custom
 * properties, so a `--who` key is a type error without it. One cast here beats one at
 * every call site.
 */
export function voiceOf(agent: string | null | undefined): CSSProperties {
  return { '--who': voice(agent) } as CSSProperties
}
