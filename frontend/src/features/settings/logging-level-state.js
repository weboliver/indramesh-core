// Pure, dependency-free state helpers for the Logging section of the Settings
// page (Phase 299). Keeping the decision rules, the draft-minutes parser, the
// countdown math and the expiry-refetch identity here makes every rule
// testable with plain node:test — no React, no timers, no network, no clock.

// The frozen severity vocabulary (CONTEXT D-02): Python level names, rendered
// as-is and never translated. Neither the order nor the spelling may change.
export const LOG_LEVEL_NAMES = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

// Numeric logging severities used by the backend's `ttl_required_below`.
const LEVEL_SEVERITY = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
}

export const DEFAULT_EXPIRY_MINUTES = 30
const MIN_EXPIRY_MINUTES = 1
const MAX_EXPIRY_MINUTES = 1440

/**
 * Accepts only a whole number of minutes in 1..1440 (Mirrors the backend's
 * 60..86400 second range and the `parseWholePositiveMb` precedent). Returns
 * the integer, or null for anything else (empty, fraction, 0, 1441, text).
 */
export function parseExpiryMinutes(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const trimmed = String(value).trim()
  if (!/^\d+$/.test(trimmed)) return null
  const minutes = Number(trimmed)
  if (!Number.isInteger(minutes) || minutes < MIN_EXPIRY_MINUTES || minutes > MAX_EXPIRY_MINUTES) {
    return null
  }
  return minutes
}

/**
 * D-22: a level is "below the TTL threshold" when its severity is lower than
 * the server-provided `ttl_required_below`. This is deliberately not a
 * comparison against `base_level`: the threshold can be higher (e.g. `db`
 * base INFO, threshold WARNING because of the noisy sqlalchemy member).
 */
export function requiresExpiry(level, ttlRequiredBelow) {
  const severity = LEVEL_SEVERITY[level]
  if (severity === undefined || typeof ttlRequiredBelow !== 'number') return false
  return severity < ttlRequiredBelow
}

/**
 * The select-change decision (D-11/D-22), in priority order:
 *   1. picking the base level itself  -> explicit clear (`null`, D-02)
 *   2. picking below `ttl_required_below` -> open a draft, send nothing yet
 *   3. any other level                 -> immediate `{ level }`
 *
 * When `minutes` is supplied the below-threshold branch becomes the draft's
 * Apply: it validates the minutes and returns the strict
 * `{ level, ttl_seconds: minutes * 60 }` value (D-06/D-23) — never an
 * absolute `expires_at`, and with no reference to the client clock.
 *
 * Returns `{ action, value }` where action is
 * `'clear' | 'draft' | 'update' | 'invalid'`.
 */
export function buildLevelUpdate({ level, baseLevel, ttlRequiredBelow, minutes }) {
  if (level === baseLevel) {
    return { action: 'clear', value: null }
  }
  if (requiresExpiry(level, ttlRequiredBelow)) {
    if (minutes === undefined || minutes === null) {
      return { action: 'draft', value: null }
    }
    const parsed = parseExpiryMinutes(minutes)
    if (parsed === null) {
      return { action: 'invalid', value: null }
    }
    return { action: 'update', value: { level, ttl_seconds: parsed * 60 } }
  }
  return { action: 'update', value: { level } }
}

/**
 * D-07: remaining time is anchored to the server's `remaining_seconds` and
 * then decremented by the client's monotonic elapsed time since the snapshot
 * arrived — never recomputed from a client wall clock against `expires_at`.
 */
export function remainingDuration({ remainingSeconds, loadedAt, now }) {
  if (remainingSeconds === null || remainingSeconds === undefined) return null
  const elapsed = Math.max(0, Math.floor((now - loadedAt) / 1000))
  return Math.max(0, remainingSeconds - elapsed)
}

/**
 * Fixed countdown format: `HH:MM:SS`, or `MM:SS` under one hour. Padded so
 * ticking digits do not shift width.
 */
export function formatRemaining(totalSeconds) {
  const safe = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const seconds = safe % 60
  const pad = (value) => String(value).padStart(2, '0')
  if (hours > 0) return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
  return `${pad(minutes)}:${pad(seconds)}`
}

/**
 * Identifies one expiry target: a category whose override shares an
 * `expires_at` within one server snapshot. Used to refetch exactly once per
 * snapshot/expiry and to stop an identical already-expired response from
 * looping.
 */
export function expiryRefetchKey(category, expiresAt, serverTime) {
  return `${category}|${expiresAt || ''}|${serverTime || ''}`
}
