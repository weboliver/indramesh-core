import { useMemo } from 'react'
import { unescapeHtmlEntities } from './html-entities'

/**
 * Safely renders text containing <mark> tags from the backend.
 * Strips all HTML first, then re-applies only <mark> tags.
 * No dangerouslySetInnerHTML — uses string splitting to isolate tagged segments.
 *
 * Per D-08: XSS-safe — strips ALL HTML, re-applies only <mark>.
 *
 * `escaped`: set when `text` originates from a backend EscapedStr field (e.g. the
 * "name" highlight field) — those values are HTML-entity-escaped at ingestion time,
 * so this decodes them for display (after tag-stripping, so nothing "un-strips").
 * Leave false for fields that were never escaped (e.g. attr_* / value_simple).
 */
export default function HighlightedText({ text, className = '', escaped = false }) {
  // Hooks must be unconditional (rules-of-hooks) — the empty-text guard lives
  // inside the memo (returns null) instead of an early return before the hook.
  const segments = useMemo(() => {
    if (!text) return null

    // 1. Strip ALL HTML tags (backup safety — backend already strips non-<mark> tags)
    const stripped = text.replace(/<[^>]*>/g, '')

    // 2. If there were no <mark> tags in the original, return plain text
    if (!text.includes('<mark>')) {
      return [{ text: stripped, highlight: false }]
    }

    // 3. Parse mark-tagged segments: split on <mark>...</mark>
    //    Pattern: plain text, then <mark>highlighted</mark>, then more plain text, etc.
    const parts = []
    const markRegex = /<mark>(.*?)<\/mark>/g
    let lastIndex = 0
    let match

    while ((match = markRegex.exec(text)) !== null) {
      // Text before the match (strip any remaining HTML from it)
      if (match.index > lastIndex) {
        const before = text.slice(lastIndex, match.index).replace(/<[^>]*>/g, '')
        if (before) parts.push({ text: before, highlight: false })
      }
      // The highlighted match content (strip any HTML inside <mark> as safety measure)
      const highlighted = match[1].replace(/<[^>]*>/g, '')
      if (highlighted) parts.push({ text: highlighted, highlight: true })
      lastIndex = match.index + match[0].length
    }

    // Remaining text after last match
    if (lastIndex < text.length) {
      const after = text.slice(lastIndex).replace(/<[^>]*>/g, '')
      if (after) parts.push({ text: after, highlight: false })
    }

    const result = parts.length > 0 ? parts : [{ text: stripped, highlight: false }]
    // Decode entities last — after tag-stripping is done — so an entity that
    // decodes into something tag-shaped (e.g. "&lt;b&gt;") is never mistaken
    // for markup to strip.
    return escaped
      ? result.map((seg) => ({ ...seg, text: unescapeHtmlEntities(seg.text) }))
      : result
  }, [text, escaped])

  if (!segments) return null

  return (
    <span className={className}>
      {segments.map((seg, i) =>
        seg.highlight ? (
          <mark key={i} className="bg-yellow-200 dark:bg-yellow-800 rounded px-0.5">
            {seg.text}
          </mark>
        ) : (
          <span key={i}>{seg.text}</span>
        )
      )}
    </span>
  )
}
