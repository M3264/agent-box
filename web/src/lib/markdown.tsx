/**
 * Markdown to React, without a dependency and without `dangerouslySetInnerHTML`.
 *
 * Agents write markdown. Every one of them: headings, bullet lists, `**Severity:**
 * Medium`, backticked paths, fenced code. The UI used to print that verbatim, so a
 * finished job's result read as a wall of hashes and asterisks.
 *
 * Why hand-written rather than `react-markdown`:
 *
 * - **No HTML path.** This parser emits React elements only. There is no point in the
 *   pipeline where a string becomes markup, so a model that writes `<script>` gets a
 *   visible `<script>` and nothing else. Sanitiser configuration cannot be got wrong
 *   because there is no sanitiser.
 * - **The committed bundle is what the service serves.** Adding a dependency means the
 *   build output grows and `node_modules` has to be present at build time on whatever
 *   machine next rebuilds `static/`.
 *
 * What it supports is what agents actually emit: ATX headings, fenced and indented
 * code, ordered and unordered lists (nested), block quotes, tables, horizontal rules,
 * and inline code, bold, italic, strikethrough, links and bare URLs. Anything it does
 * not recognise falls through as text, which is the same thing the UI did before —
 * so an unsupported construct is never worse than the status quo.
 */

import type { ReactNode } from 'react'

// --------------------------------------------------------------------------- blocks

type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'code'; lang: string | null; code: string }
  | { kind: 'list'; ordered: boolean; start: number; items: string[] }
  | { kind: 'quote'; text: string }
  | { kind: 'table'; header: string[]; align: Align[]; rows: string[][] }
  | { kind: 'rule' }
  | { kind: 'para'; text: string }

type Align = 'left' | 'center' | 'right' | null

const HEADING = /^(#{1,6})\s+(.*)$/
const FENCE = /^(?:```|~~~)\s*([\w+-]*)\s*$/
const BULLET = /^(\s*)[-*+]\s+(.*)$/
const NUMBER = /^(\s*)(\d{1,9})[.)]\s+(.*)$/
const QUOTE = /^\s{0,3}>\s?(.*)$/
const RULE = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/
const TABLE_DIVIDER = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/

/** Split a table row on unescaped pipes, dropping the leading and trailing ones. */
function cells(line: string): string[] {
  const parts: string[] = []
  let current = ''
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index]
    if (char === '\\' && line[index + 1] === '|') {
      current += '|'
      index += 1
    } else if (char === '|') {
      parts.push(current)
      current = ''
    } else {
      current += char
    }
  }
  parts.push(current)
  if (parts.length && parts[0].trim() === '') parts.shift()
  if (parts.length && parts[parts.length - 1].trim() === '') parts.pop()
  return parts.map((part) => part.trim())
}

function alignments(divider: string): Align[] {
  return cells(divider).map((cell) => {
    const left = cell.startsWith(':')
    const right = cell.endsWith(':')
    if (left && right) return 'center'
    if (right) return 'right'
    if (left) return 'left'
    return null
  })
}

/**
 * Group lines into blocks.
 *
 * A single pass with an explicit index rather than a line-by-line state machine: fences
 * and lists both need to consume an arbitrary run of following lines, and expressing
 * that as "advance the cursor" is what keeps the two from interfering. A fence
 * swallows everything up to its closer, which is why a `#` inside a code block stays
 * a `#`.
 */
export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let index = 0

  const flushParagraph = (buffer: string[]): void => {
    const text = buffer.join('\n').trim()
    if (text) blocks.push({ kind: 'para', text })
    buffer.length = 0
  }

  const paragraph: string[] = []

  while (index < lines.length) {
    const line = lines[index]

    if (!line.trim()) {
      flushParagraph(paragraph)
      index += 1
      continue
    }

    const fence = FENCE.exec(line)
    if (fence) {
      flushParagraph(paragraph)
      const marker = line.trim().slice(0, 3)
      const code: string[] = []
      index += 1
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        code.push(lines[index])
        index += 1
      }
      // An unterminated fence runs to the end of the document, which is what every
      // renderer does and what a truncated agent response produces.
      index += 1
      blocks.push({ kind: 'code', lang: fence[1] || null, code: code.join('\n') })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      flushParagraph(paragraph)
      blocks.push({
        kind: 'heading',
        level: heading[1].length,
        // Closing hashes are optional in ATX and agents sometimes write them.
        text: heading[2].replace(/\s+#+\s*$/, '').trim(),
      })
      index += 1
      continue
    }

    if (RULE.test(line)) {
      flushParagraph(paragraph)
      blocks.push({ kind: 'rule' })
      index += 1
      continue
    }

    // A table needs its divider on the next line, so this looks ahead before
    // committing — otherwise any prose containing a pipe becomes a one-cell table.
    if (line.includes('|') && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1])) {
      flushParagraph(paragraph)
      const header = cells(line)
      const align = alignments(lines[index + 1])
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(cells(lines[index]))
        index += 1
      }
      blocks.push({ kind: 'table', header, align, rows })
      continue
    }

    if (QUOTE.test(line)) {
      flushParagraph(paragraph)
      const quoted: string[] = []
      while (index < lines.length && lines[index].trim()) {
        const match = QUOTE.exec(lines[index])
        // A lazy continuation line (no `>`) still belongs to the quote.
        quoted.push(match ? match[1] : lines[index])
        index += 1
        if (index < lines.length && !QUOTE.test(lines[index]) && !lines[index].trim()) break
      }
      blocks.push({ kind: 'quote', text: quoted.join('\n') })
      continue
    }

    const bullet = BULLET.exec(line)
    const numbered = NUMBER.exec(line)
    if (bullet || numbered) {
      flushParagraph(paragraph)
      const ordered = Boolean(numbered)
      const baseIndent = (bullet ? bullet[1] : numbered![1]).length
      const start = numbered ? Number(numbered[2]) : 1
      const items: string[] = []
      let item: string[] = [bullet ? bullet[2] : numbered![3]]

      index += 1
      while (index < lines.length) {
        const next = lines[index]
        if (!next.trim()) {
          // One blank line inside a list is a loose list, not the end of it. Two ends
          // it, and so does a blank line followed by anything unindented.
          const after = lines[index + 1]
          if (!after || !after.trim() || after.search(/\S/) <= baseIndent) break
          item.push('')
          index += 1
          continue
        }
        const nextBullet = BULLET.exec(next)
        const nextNumber = NUMBER.exec(next)
        const nextIndent = (nextBullet ? nextBullet[1] : nextNumber ? nextNumber[1] : '').length

        if ((nextBullet || nextNumber) && nextIndent <= baseIndent) {
          items.push(item.join('\n'))
          item = [nextBullet ? nextBullet[2] : nextNumber![3]]
          index += 1
          continue
        }
        if (next.search(/\S/) > baseIndent) {
          // Nested list or a continuation paragraph: keep it, dedented by the
          // marker's indent, and let the recursive render deal with it.
          item.push(next.slice(baseIndent + 2))
          index += 1
          continue
        }
        break
      }
      items.push(item.join('\n'))
      blocks.push({ kind: 'list', ordered, start, items })
      continue
    }

    paragraph.push(line)
    index += 1
  }

  flushParagraph(paragraph)
  return blocks
}

// --------------------------------------------------------------------------- inline

/**
 * Only schemes a link can safely open. `javascript:` and `data:` are the reason this
 * list is an allowlist rather than a denylist — the text is written by a model.
 */
const SAFE_SCHEME = /^(?:https?:|mailto:)/i

function safeHref(href: string): string | null {
  const trimmed = href.trim()
  if (!trimmed) return null
  if (SAFE_SCHEME.test(trimmed)) return trimmed
  // Relative and anchor links stay usable; anything else with a scheme does not.
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return null
  if (trimmed.startsWith('/') || trimmed.startsWith('#') || trimmed.startsWith('./')) return trimmed
  return null
}

/**
 * One pass over a line's inline markup.
 *
 * Named groups rather than numbered ones on purpose: the alternation is built from
 * separate lines for readability, and hand-counting indices across seven rules with
 * back-references is exactly the kind of thing that silently renders `**bold**`
 * literally after an unrelated edit.
 *
 * Order matters. Code comes first so `**` inside backticks stays literal; bold comes
 * before italic so `**` is not read as two `*`.
 *
 * The link target allows one level of balanced parentheses, because real URLs contain
 * them (`.../Foo_(bar)`) and because without it `[x](javascript:alert(1))` matches
 * short and leaves a stray `)` in the prose.
 */
const TARGET = '(?:[^()\\s]|\\([^()\\s]*\\))+'

const INLINE_SOURCE = [
  '(?<fence>`+)(?<code>[\\s\\S]+?)\\k<fence>',
  `!\\[(?<alt>[^\\]]*)\\]\\((?<src>${TARGET})[^)]*\\)`,
  `\\[(?<text>[^\\]]*)\\]\\((?<href>${TARGET})[^)]*\\)`,
  '(?<bmark>\\*\\*|__)(?=\\S)(?<bold>[\\s\\S]+?\\S)\\k<bmark>',
  '(?<imark>[*_])(?=\\S)(?<italic>[\\s\\S]+?\\S)\\k<imark>',
  '~~(?=\\S)(?<strike>[\\s\\S]+?\\S)~~',
  '(?<url>https?://[^\\s<>()]+)',
].join('|')

/** Render inline markup. Returns an array of nodes with stable keys. */
export function renderInline(text: string, keyPrefix = 'i'): ReactNode[] {
  // Constructed per call, not shared. `renderInline` recurses into the contents of
  // every emphasis and link it finds, and a module-level `/g/` regex carries
  // `lastIndex` across those calls: the inner call finishes by resetting it to 0, the
  // outer loop then re-matches the span it had just consumed, and the two spin
  // forever. One regex per invocation is the whole fix.
  const pattern = new RegExp(INLINE_SOURCE, 'g')
  const nodes: ReactNode[] = []
  let cursor = 0
  let key = 0

  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null) {
    const groups = match.groups ?? {}

    // `snake_case_identifiers` are not italic. Underscores only open emphasis at a
    // word boundary, which is the rule every renderer applies and the reason agent
    // output full of `provider_id` does not come out mangled.
    if (groups.imark === '_' && /\w/.test(text[match.index - 1] ?? '')) {
      pattern.lastIndex = match.index + 1
      continue
    }

    if (match.index > cursor) nodes.push(text.slice(cursor, match.index))
    cursor = match.index + match[0].length
    const id = `${keyPrefix}-${key++}`

    if (groups.fence !== undefined) {
      nodes.push(
        <code key={id} className="md-code">
          {groups.code}
        </code>,
      )
    } else if (groups.src !== undefined) {
      // Images are not fetched: an agent's output should not make the browser reach
      // out to a URL the model chose. The alt text and the link are enough.
      const href = safeHref(groups.src)
      nodes.push(
        href ? (
          <a key={id} href={href} target="_blank" rel="noreferrer noopener" className="md-link">
            {groups.alt || href}
          </a>
        ) : (
          <span key={id}>{groups.alt || groups.src}</span>
        ),
      )
    } else if (groups.href !== undefined) {
      const href = safeHref(groups.href)
      nodes.push(
        href ? (
          <a key={id} href={href} target="_blank" rel="noreferrer noopener" className="md-link">
            {renderInline(groups.text ?? '', id)}
          </a>
        ) : (
          <span key={id}>{groups.text || groups.href}</span>
        ),
      )
    } else if (groups.bold !== undefined) {
      nodes.push(<strong key={id}>{renderInline(groups.bold, id)}</strong>)
    } else if (groups.italic !== undefined) {
      nodes.push(<em key={id}>{renderInline(groups.italic, id)}</em>)
    } else if (groups.strike !== undefined) {
      nodes.push(<del key={id}>{renderInline(groups.strike, id)}</del>)
    } else if (groups.url !== undefined) {
      // Trailing sentence punctuation is not part of the URL. Agents write "see
      // https://x.test/a." constantly, and a link that includes the full stop
      // usually 404s. The stripped characters go back into the prose.
      const url = groups.url.replace(/[.,;:!?'"]+$/, '')
      nodes.push(
        <a key={id} href={url} target="_blank" rel="noreferrer noopener" className="md-link">
          {url}
        </a>,
      )
      if (url.length < groups.url.length) nodes.push(groups.url.slice(url.length))
    }
  }

  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

// --------------------------------------------------------------------------- render

function renderBlock(block: Block, key: string): ReactNode {
  switch (block.kind) {
    case 'heading': {
      const Tag = `h${Math.min(block.level + 1, 6)}` as 'h2'
      return (
        <Tag key={key} className={`md-h md-h${block.level}`}>
          {renderInline(block.text, key)}
        </Tag>
      )
    }
    case 'code':
      return (
        <pre key={key} className="md-pre" data-lang={block.lang ?? undefined}>
          <code>{block.code}</code>
        </pre>
      )
    case 'list': {
      const items = block.items.map((item, position) => (
        <li key={`${key}-${position}`}>
          {/* Recursion is what makes nesting work; a single-paragraph item renders
              inline so a bullet does not gain a stray block of vertical margin. */}
          <Markdown source={item} inlineFirstParagraph />
        </li>
      ))
      return block.ordered ? (
        <ol key={key} className="md-list" start={block.start}>
          {items}
        </ol>
      ) : (
        <ul key={key} className="md-list">
          {items}
        </ul>
      )
    }
    case 'quote':
      return (
        <blockquote key={key} className="md-quote">
          <Markdown source={block.text} />
        </blockquote>
      )
    case 'table':
      return (
        <div key={key} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {block.header.map((cell, position) => (
                  <th key={position} style={{ textAlign: block.align[position] ?? undefined }}>
                    {renderInline(cell, `${key}-h${position}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {block.header.map((_, position) => (
                    <td key={position} style={{ textAlign: block.align[position] ?? undefined }}>
                      {renderInline(row[position] ?? '', `${key}-${rowIndex}-${position}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'rule':
      return <hr key={key} className="md-rule" />
    case 'para':
      return (
        <p key={key} className="md-p">
          {renderInline(block.text, key)}
        </p>
      )
  }
}

export interface MarkdownProps {
  source: string | null | undefined
  /**
   * Drop the wrapping `<p>` when the whole source is one paragraph. Used for list
   * items and for one-line messages, where a block element would add margin the
   * surrounding layout has already accounted for.
   */
  inlineFirstParagraph?: boolean
  className?: string
}

/** Render markdown as React elements. */
export function Markdown({ source, inlineFirstParagraph, className }: MarkdownProps) {
  const text = (source ?? '').trim()
  if (!text) return null

  const blocks = parseBlocks(text)
  if (inlineFirstParagraph && blocks.length === 1 && blocks[0].kind === 'para') {
    return <>{renderInline(blocks[0].text)}</>
  }

  const rendered = blocks.map((block, index) => renderBlock(block, `b${index}`))
  if (inlineFirstParagraph) {
    // Still one flow, just without the outer element: a list item that also has a
    // nested list should not nest another div inside the <li>.
    return <>{rendered}</>
  }
  return <div className={className ? `md ${className}` : 'md'}>{rendered}</div>
}

/**
 * First line of a markdown document as plain text, for a one-line summary.
 *
 * The timeline and job list show a gist, and a gist that says `## Overall conclusion`
 * is worse than one that says `Overall conclusion`.
 */
export function markdownGist(source: string | null | undefined, limit = 140): string {
  const text = (source ?? '').replace(/\r\n?/g, '\n')
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line || FENCE.test(line) || RULE.test(line)) continue
    const stripped = line
      .replace(HEADING, '$2')
      .replace(/^\s{0,3}>\s?/, '')
      .replace(BULLET, '$2')
      .replace(NUMBER, '$3')
      .replace(/[*_~`]/g, '')
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
      .trim()
    if (stripped) return stripped.length > limit ? `${stripped.slice(0, limit - 1)}…` : stripped
  }
  return ''
}
