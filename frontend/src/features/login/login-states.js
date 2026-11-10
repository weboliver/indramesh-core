// Pure and dependency-free (no React, no DOM) so plain Node can load it: the
// login screen's notice conditionality is decided in exactly one place and is a
// test assertion rather than a screenshot (UI-SPEC §2.2).

export const SLOW_LOGIN_HINT_MS = 2000

// `?session=` names the *cause* of a landing, never "is there a cookie" — the
// browser session carries no JS-readable state. Only the two known causes render
// a notice; anything else (absent, empty, padded, differently cased, unknown)
// renders no element at all, never an empty box.
export function noticeKeyForSessionParam(value) {
  if (value === 'expired') return 'auth.session_expired'
  if (value === 'password-changed') return 'auth.password_changed_sign_in'
  return null
}

// The English default for each notice key, for the same reason every other
// `t()` call in this app carries one: the browser-side English bundle is empty
// by design (`shared/lib/i18n.js` establishes an empty `en` baseline, and
// `use-ui-language.jsx` fetches a dictionary only for a *non-English* profile
// language), so a bare `t(key)` renders the raw key. Without these the notice
// reads "auth.session_expired" instead of a sentence. A loaded dictionary still
// wins, so a translated profile keeps its own copy.
export const NOTICE_DEFAULTS = {
  'auth.session_expired': 'Your session has expired. Please sign in again.',
  'auth.password_changed_sign_in': 'Password changed. Please sign in with your new password.',
}
