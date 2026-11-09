import axios from 'axios'
import { RATE_LIMIT_BASE_DELAY_MS, shouldRetryRateLimit } from './api-retry-policy.js'
import { classifyAuthFailure, clearToken, getToken, refreshOnce } from './session-token.js'

const api = axios.create({
  baseURL: '/api/v1/',
  headers: { 'Content-Type': 'application/json' },
})

// The access token lives in the in-memory store (D-01) and is attached here,
// per request. `host-api.js` re-exports this same `api` by identity, so a plugin
// bundle gets the header automatically and never receives the token (D-02).
api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Nginx enforces a per-IP `api_limit` (200 req/min, burst=200) in front of the
// backend (Phase 96 HEADERS-01). A single page navigation legitimately fires
// several parallel requests (sources, schemas, object-types, tasks, attribute
// types, icons...), so bursts of 429s are an expected transient condition, not
// a hard failure — retry with capped exponential backoff before giving up.
// The decision itself lives in api-retry-policy.js so a costly explicit request
// can opt out and an aborted request is never replayed (Plan 306-03).
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// One hard navigation per session end: N simultaneous failed refreshes must
// land on the login page once, and never while it is already rendered.
//
// The flag lives on `window`, not in this module: with code-split routes the
// same handler can be evaluated in more than one module instance, each with its
// own `let redirecting`, and two of those racing produce two landings on
// `/login?session=expired` (observed by e2e/auth.spec.js). One document-wide
// flag is the only guard that covers every instance.
const SESSION_ENDING_FLAG = '__indrameshSessionEnding'

function endSession() {
  if (typeof window === 'undefined') return
  if (window[SESSION_ENDING_FLAG]) return
  if (window.location?.pathname === '/login') return
  window[SESSION_ENDING_FLAG] = true
  window.location.assign('/login?session=expired')
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    // The renew/reject/pass decision lives entirely in classifyAuthFailure, so
    // a 503 (fail-closed blocklist, SESS-05) and a 429 never take the
    // session-over path and never clear the token.
    switch (classifyAuthFailure(error.response?.status, error.config)) {
      case 'renew': {
        const config = error.config || {}
        config._authRetried = true
        try {
          // The refresh uses this same instance (never a second one) and is
          // marked `_skipAuthRetry` so a refused refresh can never re-enter
          // the retry loop. One shared promise covers parallel 401s.
          await refreshOnce(() =>
            api
              .post('auth/refresh', null, { _skipAuthRetry: true })
              .then((res) => res.data.access_token)
          )
        } catch (refreshError) {
          // Only a refused refresh (401) means the session is over. A 503 from
          // the fail-closed blocklist, or a network failure, is transient: keep
          // the token and let the caller's own error handling show it, so a
          // two-second Valkey restart cannot log everyone out (SESS-05,
          // T-321-21 / RESEARCH Pitfall 6).
          if (refreshError?.response?.status === 401) {
            clearToken()
            endSession()
          }
          return Promise.reject(error)
        }
        return api(config)
      }
      case 'reject':
      case 'pass':
      default:
        break
    }

    if (error.response?.status === 429) {
      const config = error.config || {}
      const retryCount = config._rateLimitRetryCount || 0
      if (shouldRetryRateLimit(config, retryCount)) {
        config._rateLimitRetryCount = retryCount + 1
        const delay = RATE_LIMIT_BASE_DELAY_MS * 2 ** retryCount
        await sleep(delay)
        // Re-check after the backoff: an abort during the wait must not replay.
        if (config.signal?.aborted) {
          return Promise.reject(error)
        }
        return api(config)
      }
    }
    return Promise.reject(error)
  }
)

// Axios's default array serialization produces `key[]=a&key[]=b`, which FastAPI's
// `Query(...)` List params don't recognize — it expects the key repeated bare
// (`key=a&key=b`). Use as `paramsSerializer` for any batch (`..._ids`) request.
export function serializeRepeatedParams(params) {
  const usp = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (Array.isArray(value)) {
      value.forEach(v => usp.append(key, v))
    } else if (value !== undefined && value !== null) {
      usp.append(key, value)
    }
  })
  return usp.toString()
}

export default api
