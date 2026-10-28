import { clsx } from "clsx"
import { twMerge } from "tailwind-merge"
import i18next from 'i18next'

function t(key, defaultValue) {
  return i18next.isInitialized ? i18next.t(key, defaultValue) : defaultValue
}

export function cn(...inputs) {
  return twMerge(clsx(inputs))
}

// FastAPI validation errors (422) return `detail` as an array of
// {type, loc, msg, input, ctx} objects, not a string — rendering that array
// directly as a React child crashes with "Objects are not valid as a React
// child". Normalize to a single human-readable string for single-message
// error banners (see EditObjectModal.jsx for the per-field variant).
function defaultErrorMessage() {
  return t('common.unexpected_error', 'An unexpected error occurred.')
}

export function extractErrorMessage(err, fallback = defaultErrorMessage()) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(d => d?.msg).filter(Boolean).join('; ') || fallback
  }
  // Structured object detail (the saved-query 422 payload: {path, message,
  // code, limit, actual, allowed}) — without this branch axios' generic
  // "Request failed with status code 422" would hide the server's reason.
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  return err?.message || fallback
}
