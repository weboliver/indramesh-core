/**
 * The single derivation of the initials shown in the avatar circles.
 *
 * Both surfaces (the top-nav trigger and the profile section header) call this
 * one function, so the two circles can never disagree. The module is pure — no
 * DOM, no React, no imports — which is what lets `node --test` load it in plain
 * Node.
 *
 * Derivation:
 *   first + last name -> two letters   (Anna / Neumann -> AN)
 *   exactly one name  -> one letter    (Anna / ''      -> A)
 *   neither, username -> one letter    (oliver         -> O)
 *   neither, no user  -> '?'           (nothing left to show)
 *
 * Each part is NFC-normalised and trimmed first, so a decomposed diacritic
 * yields one letter and whitespace-only input counts as absent. The first
 * character is taken as a code point (never `text[0]`, which would split a
 * surrogate pair and render a broken emoji).
 */

const MAX_INITIALS = 2

function firstCodePoint(value) {
  const text = String(value ?? '').normalize('NFC').trim()
  if (!text) return ''
  return [...text][0].toUpperCase()
}

export function getInitials({ first_name, last_name, username } = {}) {
  const first = firstCodePoint(first_name)
  const last = firstCodePoint(last_name)

  let initials
  if (first && last) initials = `${first}${last}`
  else if (first) initials = first
  else if (last) initials = last
  else initials = firstCodePoint(username) || '?'

  // Clamp by code point so a two-code-unit emoji is never cut in half.
  return [...initials].slice(0, MAX_INITIALS).join('')
}
