// In-memory access-token store (D-01). The access token is never written to
// web storage and never handed to plugin code: `use-api.js`
// attaches it per request, and plugin bundles reach it only through the shared
// axios identity that `host-api.js` re-exports (D-02).
//
// This module is deliberately free of DOM APIs (`window`, `document`, cookies)
// so plain-Node tests — including the plugin-runtime suite — can import it.
// Nulling/refreshing is a single-flight promise rather than a lock with timers
// (RESEARCH Pattern 1): 20 parallel 401s must produce exactly one refresh call.

let token = null
let refreshInFlight = null
const listeners = new Set()

export function getToken() {
  return token
}

export function setToken(next) {
  token = next ?? null
  listeners.forEach((listener) => listener(token))
}

export function clearToken() {
  setToken(null)
}

// Returns an unsubscribe function. A listener that unsubscribes during
// notification still receives the change it is being notified of; it simply
// receives no later ones (the Set is snapshotted by `forEach`).
export function subscribe(listener) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

// Single-flight: concurrent callers share the one in-flight promise instead of
// starting a second refresh. The store is only written when the refresh
// resolves, so a rejected refresh leaves the previous token untouched (the
// caller decides whether that is a session end) and the next attempt starts
// fresh once the promise has settled.
export function refreshOnce(requestRefresh) {
  if (refreshInFlight) return refreshInFlight
  // The request starts synchronously (before the first `await`), so concurrent
  // callers in the same tick already see the in-flight promise.
  const attempt = (async () => {
    const next = await requestRefresh()
    setToken(next)
    return next
  })()
  refreshInFlight = attempt.finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

// The interceptor's entire policy, in one testable place:
// - 'reject': a 401 on a request that must not be renewed (`_skipAuthRetry`) or
//   that already spent its one retry (`_authRetried`) — propagate untouched.
// - 'renew': any other 401 — the access token expired; refresh once, retry once.
// - 'pass': everything else, including 503 (fail-closed blocklist, SESS-05) and
//   429 (rate limit). A transient store outage must never look like a logout.
export function classifyAuthFailure(status, config = {}) {
  if (status !== 401) return 'pass'
  if (config._skipAuthRetry || config._authRetried) return 'reject'
  return 'renew'
}
