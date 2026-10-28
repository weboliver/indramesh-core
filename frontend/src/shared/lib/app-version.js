// D-06/REL-01: the running version, read from the API at runtime — never baked
// into the bundle. `/health` answers `{"status": ..., "version": "..."}` and is
// proxied same-origin by nginx (the SPA's `connect-src 'self'` forbids calling
// the backend port directly), so a plain `fetch('/health')` is the whole client.
//
// T-318-05: the payload is treated as data. Only a string `version` on a plain
// object survives; everything else yields null and the caller renders nothing.
// T-318-07: one promise per fetch implementation (i.e. one per page load for the
// global fetch) caps the request count at one however often the menu toggles. A
// thrown request clears that entry so a later open retries instead of showing a
// permanent failure; a non-ok or unparsable answer is a value (null), not a
// retry, because the endpoint is the same for the whole page load.

const HEALTH_PATH = '/health'

// Keyed on the fetch implementation rather than a single slot so a caller that
// injects an implementation (tests) never shares a cached read with the app.
const readsByFetch = new WeakMap()

export function readVersionPayload(payload) {
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) return null
  const { version } = payload
  if (typeof version !== 'string') return null
  const trimmed = version.trim()
  return trimmed === '' ? null : trimmed
}

export function formatVersionLabel(version) {
  const value = typeof version === 'string' ? version.trim() : ''
  if (value === '') return ''
  return /^v/i.test(value) ? value : `v${value}`
}

export function fetchAppVersion(fetchImpl = globalThis.fetch) {
  const impl = fetchImpl ?? globalThis.fetch
  if (typeof impl !== 'function') return Promise.resolve(null)

  let pending = readsByFetch.get(impl)
  if (!pending) {
    pending = readVersion(impl)
    readsByFetch.set(impl, pending)
    pending.catch(() => {
      readsByFetch.delete(impl)
    })
  }
  return pending
}

async function readVersion(fetchImpl) {
  const response = await fetchImpl(HEALTH_PATH, { headers: { Accept: 'application/json' } })
  // A non-ok answer says nothing about a version; it is not a parse problem.
  if (!response || !response.ok) return null
  let payload = null
  try {
    payload = await response.json()
  } catch {
    // A deployment may answer `/health` with prose (a proxy error page, an SPA
    // document): that is an absent version, not an exception for the caller.
    payload = null
  }
  return readVersionPayload(payload)
}
