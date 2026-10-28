import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { fetchAppVersion, formatVersionLabel, readVersionPayload } from './app-version.js'

// A resolved HTTP answer, shaped like the two fields the module reads.
const response = (body, ok = true) => ({
  ok,
  json: async () => body,
})

describe('readVersionPayload', () => {
  it('returns the version of a well-formed payload', () => {
    assert.equal(readVersionPayload({ status: 'healthy', version: '1.0.0' }), '1.0.0')
  })

  it('trims surrounding whitespace off the version', () => {
    assert.equal(readVersionPayload({ version: '  1.0.0  ' }), '1.0.0')
  })

  it('returns null when the payload carries no version', () => {
    assert.equal(readVersionPayload({ status: 'healthy' }), null)
  })

  it('returns null for an empty or whitespace-only version', () => {
    assert.equal(readVersionPayload({ version: '' }), null)
    assert.equal(readVersionPayload({ version: '   ' }), null)
  })

  it('returns null for a non-string version', () => {
    assert.equal(readVersionPayload({ version: 100 }), null)
    assert.equal(readVersionPayload({ version: null }), null)
  })

  it('returns null for anything that is not a plain object', () => {
    assert.equal(readVersionPayload(null), null)
    assert.equal(readVersionPayload(undefined), null)
    assert.equal(readVersionPayload('1.0.0'), null)
    assert.equal(readVersionPayload([{ version: '1.0.0' }]), null)
  })
})

describe('formatVersionLabel', () => {
  it('prefixes a bare version with a single v', () => {
    assert.equal(formatVersionLabel('1.0.0'), 'v1.0.0')
  })

  it('does not double the prefix of an already prefixed version', () => {
    assert.equal(formatVersionLabel('v1.0.0'), 'v1.0.0')
    assert.equal(formatVersionLabel('V1.0.0'), 'V1.0.0')
  })

  it('trims a padded version before prefixing it', () => {
    assert.equal(formatVersionLabel('  1.0.0  '), 'v1.0.0')
  })

  it('returns the empty string for a missing or blank version', () => {
    assert.equal(formatVersionLabel(''), '')
    assert.equal(formatVersionLabel('   '), '')
    assert.equal(formatVersionLabel(null), '')
    assert.equal(formatVersionLabel(undefined), '')
  })
})

describe('fetchAppVersion', () => {
  it('reads the version from /health and requests it only once', async () => {
    const requested = []
    const impl = async (path) => {
      requested.push(path)
      return response({ status: 'healthy', version: '1.0.0' })
    }

    assert.equal(await fetchAppVersion(impl), '1.0.0')
    assert.equal(await fetchAppVersion(impl), '1.0.0')
    assert.deepEqual(requested, ['/health'])
  })

  it('returns null for a non-ok response', async () => {
    const impl = async () => response({ version: '1.0.0' }, false)
    assert.equal(await fetchAppVersion(impl), null)
  })

  it('returns null when the body is not JSON', async () => {
    const impl = async () => ({
      ok: true,
      json: async () => {
        throw new SyntaxError('Unexpected token <')
      },
    })
    assert.equal(await fetchAppVersion(impl), null)
  })

  it('retries after a thrown request instead of caching the failure', async () => {
    let attempts = 0
    const impl = async () => {
      attempts += 1
      if (attempts === 1) throw new Error('network down')
      return response({ version: '1.0.1' })
    }

    await assert.rejects(() => fetchAppVersion(impl), /network down/)
    assert.equal(await fetchAppVersion(impl), '1.0.1')
    assert.equal(attempts, 2)
  })
})
