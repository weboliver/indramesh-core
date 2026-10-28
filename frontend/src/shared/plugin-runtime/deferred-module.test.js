import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { createDeferredModuleLoader } from './deferred-module.js'

// A Blob stand-in so node:test can drive the lifecycle without a real blob.
class FakeBlob {
  constructor(parts, options) {
    this.parts = parts
    this.options = options
  }
}

function deferred() {
  let resolve
  const promise = new Promise((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function makeLoader(overrides = {}) {
  const state = { fetched: [], revoked: [], created: [] }
  const loader = createDeferredModuleLoader({
    resolveRuntime: (name) =>
      name === 'alpha' ? { version: '1.0.0', deferred_assets: ['heavy'] } : null,
    fetchSource: async (url) => {
      state.fetched.push(url)
      return 'export default {}'
    },
    importModule: async () => ({ default: {} }),
    createObjectURL: () => {
      const url = `blob:${state.created.length}`
      state.created.push(url)
      return url
    },
    revokeObjectURL: (url) => state.revoked.push(url),
    BlobCtor: FakeBlob,
    ...overrides,
  })
  return { loader, state }
}

describe('loadPluginModule (D-14 deferred host helper)', () => {
  it('fails closed for an undeclared asset id or a missing deferred_assets field', async () => {
    const { loader, state } = makeLoader()

    assert.deepEqual(await loader('alpha', 'nope'), { ok: false, reason: 'undeclared_asset' })
    assert.deepEqual(await loader('missing', 'heavy'), { ok: false, reason: 'undeclared_asset' })
    assert.deepEqual(await loader('', 'heavy'), { ok: false, reason: 'invalid_request' })
    assert.equal(state.fetched.length, 0)
  })

  it('fetches the owner-contained asset URL, imports it and revokes the import URL', async () => {
    const { loader, state } = makeLoader()

    const result = await loader('alpha', 'heavy')

    assert.equal(result.ok, true)
    assert.equal(state.fetched.length, 1)
    assert.match(state.fetched[0], /\/api\/v1\/plugins\/alpha\/assets\/heavy\?v=1\.0\.0/)
    assert.equal(state.created.length, 1)
    assert.deepEqual(state.revoked, state.created)
  })

  it('dedupes concurrent loads for the same owner/version/asset', async () => {
    const gate = deferred()
    let fetchCount = 0
    const loader = createDeferredModuleLoader({
      resolveRuntime: () => ({ version: '1.0.0', deferred_assets: ['heavy'] }),
      fetchSource: async () => {
        fetchCount += 1
        await gate.promise
        return 'source'
      },
      importModule: async () => ({ default: {} }),
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
    })

    const first = loader('alpha', 'heavy')
    const second = loader('alpha', 'heavy')
    gate.resolve()

    const [a, b] = await Promise.all([first, second])
    assert.equal(fetchCount, 1)
    assert.equal(a.ok, true)
    assert.equal(b.ok, true)
  })

  it('removes a rejected promise so an explicit retry can fetch again', async () => {
    let attempts = 0
    const loader = createDeferredModuleLoader({
      resolveRuntime: () => ({ version: '1.0.0', deferred_assets: ['heavy'] }),
      fetchSource: async () => 'source',
      importModule: async () => {
        attempts += 1
        if (attempts === 1) throw new Error('bad module')
        return { default: {} }
      },
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
    })

    const failed = await loader('alpha', 'heavy')
    assert.equal(failed.ok, false)
    assert.equal(failed.reason, 'load_failed')

    const retried = await loader('alpha', 'heavy')
    assert.equal(retried.ok, true)
    assert.equal(attempts, 2)
  })

  it('drops a completion whose owner generation changed', async () => {
    const loader = createDeferredModuleLoader({
      resolveRuntime: () => ({ version: '1.0.0', deferred_assets: ['heavy'] }),
      fetchSource: async () => 'source',
      importModule: async () => ({ default: {} }),
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
      isCurrentGeneration: () => false,
    })

    const result = await loader('alpha', 'heavy', { generation: 1 })
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'stale_generation')
  })

  it('resolves the URL through the runtime version so a version bump refetches', async () => {
    const urls = []
    let version = '1.0.0'
    const loader = createDeferredModuleLoader({
      resolveRuntime: () => ({ version, deferred_assets: ['heavy'] }),
      fetchSource: async (url) => {
        urls.push(url)
        return 'source'
      },
      importModule: async () => ({ default: {} }),
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
    })

    await loader('alpha', 'heavy')
    version = '2.0.0'
    await loader('alpha', 'heavy')

    assert.equal(urls.length, 2)
    assert.match(urls[0], /v=1\.0\.0/)
    assert.match(urls[1], /v=2\.0\.0/)
  })

  it('drops a completion whose owner left the runtime and does not cache it as stale', async () => {
    let calls = 0
    let fetches = 0
    const loader = createDeferredModuleLoader({
      // 1st resolve = pre-fetch declared check; 2nd = post-import owner check
      // (owner gone); 3rd/4th = the retry.
      resolveRuntime: () => {
        calls += 1
        if (calls === 2) return null
        return { version: '1.0.0', deferred_assets: ['heavy'] }
      },
      fetchSource: async () => {
        fetches += 1
        return 'source'
      },
      importModule: async () => ({ default: {} }),
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
    })

    const first = await loader('alpha', 'heavy')
    assert.equal(first.ok, false)
    assert.equal(first.reason, 'stale_generation')

    // The stale result is not cached: a later call refetches.
    const second = await loader('alpha', 'heavy')
    assert.equal(second.ok, true)
    assert.equal(fetches, 2)
  })

  it('drops a completion whose owner version changed mid-load', async () => {
    let version = '1.0.0'
    let release
    const gate = new Promise((resolve) => {
      release = resolve
    })
    const loader = createDeferredModuleLoader({
      resolveRuntime: () => ({ version, deferred_assets: ['heavy'] }),
      fetchSource: async () => 'source',
      importModule: async () => {
        await gate
        return { default: {} }
      },
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
      BlobCtor: FakeBlob,
    })

    const pending = loader('alpha', 'heavy')
    version = '2.0.0'
    release()

    const result = await pending
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'stale_generation')
  })
})
