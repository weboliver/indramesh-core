// Pure 429 retry DECISION for the shared authenticated client. Kept free of
// Axios and `window` so node:test can import it directly. The host replays a
// 429 with capped exponential backoff; a caller may deliberately opt out for a
// costly explicit request, and an already-aborted signal must never replay.

export const RATE_LIMIT_MAX_RETRIES = 3
export const RATE_LIMIT_BASE_DELAY_MS = 500

// The single decision the 429 interceptor branch delegates to. Returns true only
// when the request should be replayed:
// - an explicit `skipRateLimitRetry` opt-out wins, even before the first retry;
// - an aborted signal never replays (Axios would reject without sending anyway);
// - otherwise the unchanged capped retry budget applies.
export function shouldRetryRateLimit(config, retryCount) {
  if (config?.skipRateLimitRetry === true) return false
  if (config?.signal?.aborted) return false
  return retryCount < RATE_LIMIT_MAX_RETRIES
}
