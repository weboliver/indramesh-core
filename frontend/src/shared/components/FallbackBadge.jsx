import { LucideIcon } from "../lib/lucide-icons"

// D-16: render a fallback badge only when the active language differs from
// the schema default AND the value isn't actually served in the active
// language. `payload.resolved_language` (crud.py:467 and friends) is set to
// the language that actually served the content — the schema default when a
// translation exists there but not in the active language, or left
// null/undefined when no translation resolved at all (base value). Both
// cases mean "not served in `lang`" and should show the badge.
export function isFallingBack(payload, lang, defaultCode) {
  if (!lang || !defaultCode || lang === defaultCode) return false
  return (payload?.resolved_language ?? null) !== lang
}

// UX-06, D-13..D-16: presentational fallback indicator. The caller decides
// visibility (D-16 condition) and the click target (D-15); this component is
// a dumb button that renders the globe + language-code pill.
export function FallbackBadge({ langCode, langName, onClick }) {
  if (!langCode) return null

  const displayName = langName || langCode

  return (
    <button
      type="button"
      data-testid="fallback-badge"
      title={`Showing ${displayName} — no translation yet. Click to add one.`}
      onClick={onClick}
      className="inline-flex items-center gap-1 h-5 px-2 text-xs rounded-full bg-muted text-muted-foreground hover:bg-accent/50 transition-colors focus:outline-none focus:ring-1 focus:ring-primary"
    >
      <LucideIcon name="globe" size={12} />
      <span>{langCode}</span>
    </button>
  )
}
