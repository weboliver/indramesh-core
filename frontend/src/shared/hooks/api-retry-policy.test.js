import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  RATE_LIMIT_BASE_DELAY_MS,
  RATE_LIMIT_MAX_RETRIES,
  shouldRetryRateLimit,
} from './api-retry-policy.js'

describe('shouldRetryRateLimit (D-07/D-08 explicit-request opt-out)', () => {
  it('replays an ordinary 429 up to the existing retry cap and not beyond', () => {
    assert.equal(shouldRetryRateLimit({}, 0), true)
    assert.equal(shouldRetryRateLimit({}, RATE_LIMIT_MAX_RETRIES - 1), true)
    assert.equal(shouldRetryRateLimit({}, RATE_LIMIT_MAX_RETRIES), false)
    assert.equal(shouldRetryRateLimit({}, RATE_LIMIT_MAX_RETRIES + 1), false)
  })

  it('never replays when the caller opts out, even on the first attempt', () => {
    assert.equal(shouldRetryRateLimit({ skipRateLimitRetry: true }, 0), false)
    assert.equal(shouldRetryRateLimit({ skipRateLimitRetry: true }, 1), false)
  })

  it('never replays an already-aborted request', () => {
    const aborted = new AbortController()
    aborted.abort()
    assert.equal(shouldRetryRateLimit({ signal: aborted.signal }, 0), false)

    const live = new AbortController()
    assert.equal(shouldRetryRateLimit({ signal: live.signal }, 0), true)
  })

  it('treats a missing/odd config as an ordinary request', () => {
    assert.equal(shouldRetryRateLimit(undefined, 0), true)
    assert.equal(shouldRetryRateLimit(null, 0), true)
    assert.equal(shouldRetryRateLimit({ skipRateLimitRetry: 'truthy-string' }, 0), true)
  })

  it('keeps the unchanged 3-retry default and 500ms backoff base', () => {
    assert.equal(RATE_LIMIT_MAX_RETRIES, 3)
    assert.equal(RATE_LIMIT_BASE_DELAY_MS, 500)
  })
})
