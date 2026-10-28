// Reverses backend/app/validators/__init__.py's `html.escape(s, quote=True)`.
// That validator escapes EscapedStr fields (e.g. Object.name) at ingestion time;
// JSX never decodes entities when rendering text children, so displaying an
// EscapedStr value via `{value}` shows the raw "&amp;"/"&#x27;" sequences
// instead of the original characters. Decoding here is safe — it runs before
// the string reaches JSX, which always renders it as inert text, never HTML.
const ENTITY_MAP = [
  ['&lt;', '<'],
  ['&gt;', '>'],
  ['&quot;', '"'],
  ['&#x27;', "'"],
  ['&amp;', '&'], // must be last — &amp; is a prefix of no other entity above, but decoding it first would corrupt e.g. "&amp;lt;" into "<"
]

export function unescapeHtmlEntities(text) {
  if (typeof text !== 'string' || !text) return text
  return ENTITY_MAP.reduce((acc, [entity, char]) => acc.split(entity).join(char), text)
}
