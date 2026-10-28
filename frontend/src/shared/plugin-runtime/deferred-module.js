// D-14/D-16 host deferred-module helper. A plugin's lightweight registration
// bundle may declare deferred assets; the host — never arbitrary contribution
// code — builds the owner-contained credentialed asset URL and imports it only
// when the contribution is actually used.
//
// The helper resolves the current runtime owner/version, accepts only asset ids
// the discovery entry declares (a missing `deferred_assets` field fails closed,
// per R03-3), dedupes concurrent loads, revokes the import URL, removes a
// rejected promise so an explicit retry can re-fetch, and generation-guards
// completion so a late import cannot resurrect a disabled owner.
//
// fetch/import/URL/Blob are injectable (R03-1) so node:test can exercise the
// lifecycle with small fakes; the defaults are the browser/authenticated host
// APIs.

import api from '../hooks/use-api.js'
import { buildPluginBundleUrl } from './loader.js'

async function defaultFetchSource(url) {
  const { data } = await api.get(url, {
    baseURL: '',
    responseType: 'text',
    headers: { 'Cache-Control': 'no-store' },
  })
  return data
}

async function defaultImportModule(url) {
  return import(/* @vite-ignore */ url)
}

// The provider installs the current runtime snapshot (owner -> { version,
// deferred_assets }) here so this plain helper can resolve owner/version without
// importing the React context.
let runtimeSource = null

export function setDeferredRuntimeSource(source) {
  runtimeSource = typeof source === 'function' ? source : null
}

function defaultResolveRuntime(pluginName) {
  if (!runtimeSource) return null
  const snapshot = runtimeSource()
  if (snapshot instanceof Map) return snapshot.get(pluginName) ?? null
  return snapshot?.[pluginName] ?? null
}

// The backend projects `deferred_assets` as an id -> declared-path map
// (`[frontend.deferred_assets] reference = "static/..."`). Accept that shape and
// the legacy array-of-ids shape (id resolved to itself) so a caller never has to
// know which side produced the descriptor. Returns null when nothing is declared
// — fail closed (R03-3).
function normalizeDeclaredAssets(value) {
  if (Array.isArray(value)) {
    const map = {}
    value.forEach((id) => {
      if (typeof id === 'string' && id) map[id] = id
    })
    return map
  }
  if (value && typeof value === 'object') {
    const map = {}
    for (const [id, path] of Object.entries(value)) {
      if (typeof id === 'string' && id && typeof path === 'string' && path) {
        map[id] = path
      }
    }
    return map
  }
  return null
}

export function createDeferredModuleLoader(deps = {}) {
  const {
    resolveRuntime = defaultResolveRuntime,
    fetchSource = defaultFetchSource,
    importModule = defaultImportModule,
    createObjectURL = (blob) => URL.createObjectURL(blob),
    revokeObjectURL = (url) => URL.revokeObjectURL(url),
    isCurrentGeneration = () => true,
    BlobCtor = globalThis.Blob,
  } = deps

  // key -> in-flight promise. One in-flight promise per owner/version/asset.
  const inFlight = new Map()

  function cacheKey(pluginName, version, assetId) {
    return `${pluginName}\u0000${version ?? ''}\u0000${assetId}`
  }

  async function loadPluginModule(pluginName, assetId, options = {}) {
    if (!pluginName || !assetId) return { ok: false, reason: 'invalid_request' }

    const runtime = resolveRuntime(pluginName)
    const declared = normalizeDeclaredAssets(runtime?.deferred_assets)
    // R03-3: a missing deferred_assets field means "no declared assets" — fail
    // closed rather than fetching an arbitrary id.
    if (!declared || !Object.prototype.hasOwnProperty.call(declared, assetId)) {
      return { ok: false, reason: 'undeclared_asset' }
    }

    const version = runtime?.version ?? null
    const assetPath = declared[assetId]
    const { generation } = options
    const key = cacheKey(pluginName, version, assetId)
    if (inFlight.has(key)) return inFlight.get(key)

    const promise = (async () => {
      try {
        const url = buildPluginBundleUrl({
          name: pluginName,
          version,
          frontend: { bundle: assetPath },
        })
        const source = await fetchSource(url)
        const objectUrl = createObjectURL(new BlobCtor([source], { type: 'text/javascript' }))
        let module
        try {
          module = await importModule(objectUrl)
        } finally {
          revokeObjectURL(objectUrl)
        }
        // Effective generation guard without a caller-supplied token: the owner
        // must still be present in the runtime snapshot AND at the version this
        // load captured, so a late import cannot resurrect a removed/upgraded
        // owner (WR-01). Evict the in-flight entry on staleness so a later retry
        // is not permanently cached as stale.
        const current = resolveRuntime(pluginName)
        const ownerGone =
          !current || (current.version ?? null) !== version
        if (
          ownerGone ||
          (generation !== undefined &&
            !isCurrentGeneration(pluginName, generation))
        ) {
          inFlight.delete(key)
          return { ok: false, reason: 'stale_generation' }
        }
        return { ok: true, module }
      } catch (error) {
        // Remove the rejected promise so an explicit retry can fetch again.
        inFlight.delete(key)
        return { ok: false, reason: 'load_failed', error: error?.message || String(error) }
      }
    })()

    inFlight.set(key, promise)
    return promise
  }

  return loadPluginModule
}

export const loadPluginModule = createDeferredModuleLoader()
