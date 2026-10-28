// Quote-aware token-boundary scanner + suggestion insertion for the AQL
// autocomplete dropdown (D-01/D-02/D-03). The AQL grammar's STRING terminal
// (backend/app/aql/grammar.lark) is `"` (/[^"]+/)? `"` — no escape mechanism —
// so a doubled quote can only mean a closed string. Quote parity over the
// pre-cursor text therefore tells us whether the caret sits inside an
// unterminated STRING literal, which may legitimately contain spaces
// (`"My Server"` is ONE token, not two).
//
// This is a pure text-editing heuristic (replacement-range math), NOT a
// re-encoding of the grammar — suggestion correctness stays server-side
// (D-193-01). Pure ESM: no imports, no React, no DOM.

// Kinds whose insertText is a bare token: a trailing space must be appended
// after insertion so the user can keep typing the next token (D-02, Pitfall 2
// — switch on KIND, never on inspecting insertText's last character).
export const UNQUOTED_KINDS = new Set(['syntax-keyword', 'boolean-keyword', 'operator', 'ref-fn'])

// Quote-parity check (odd count of `"` before cursorPos means the caret
// sits inside an unterminated STRING literal). Shared by findTokenStart and
// the suggest hook's whitespace-boundary gate.
export function isInsideQuote(text, cursorPos) {
  const pre = text.slice(0, cursorPos)
  const quoteCount = (pre.match(/"/g) || []).length
  return quoteCount % 2 === 1
}

// Returns the index where the in-progress token starts. Outside a quote the
// token boundary is the previous whitespace, quote, or paren; inside an
// unterminated string the boundary is the opening double-quote (included in
// the replacement range so an accepted value replaces the partial value).
export function findTokenStart(text, cursorPos) {
  const pre = text.slice(0, cursorPos)
  const insideQuote = isInsideQuote(text, cursorPos)
  if (insideQuote) {
    return pre.lastIndexOf('"')
  }
  const match = pre.match(/[\s"()]?([^\s"()]*)$/)
  return match ? cursorPos - match[1].length : cursorPos
}

// The in-progress partial token at cursorPos — the fuse-filter pattern for
// the suggestion hook and the replacement range for applySuggestion.
export function getPartialToken(text, cursorPos) {
  return text.slice(findTokenStart(text, cursorPos), cursorPos)
}

// Applies an accepted suggestion: replaces [tokenStart..cursorPos] with the
// suggestion's insertText, appends a trailing space for unquoted kinds only,
// and lands the caret after the inserted text (D-03 — for 'ref-fn' that is
// after the closing paren, e.g. `inR( `). Text after cursorPos is untouched,
// so mid-query insertion edits stay correct (AC-09).
export function applySuggestion(text, cursorPos, suggestion) {
  const start = findTokenStart(text, cursorPos)
  const insert = UNQUOTED_KINDS.has(suggestion.kind)
    ? suggestion.insertText + ' '
    : suggestion.insertText
  return {
    value: text.slice(0, start) + insert + text.slice(cursorPos),
    cursor: start + insert.length,
  }
}
