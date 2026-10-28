import axios from 'axios'
import { RATE_LIMIT_BASE_DELAY_MS, shouldRetryRateLimit } from './api-retry-policy.js'

const api = axios.create({
  baseURL: '/api/v1/',
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('indramesh_token')
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

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('indramesh_token')
      window.location.href = '/login'
      return Promise.reject(error)
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
