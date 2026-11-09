import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  classifyAuthFailure,
  clearToken,
  getToken,
  refreshOnce,
  setToken,
  subscribe,
} from './session-token.js'

describe('session token store', () => {
  it('starts empty and round-trips a token', () => {
    clearToken()
    assert.equal(getToken(), null)
    setToken('access-1')
    assert.equal(getToken(), 'access-1')
    clearToken()
    assert.equal(getToken(), null)
  })

  it('notifies each subscriber on every change and stops after unsubscribe', () => {
    clearToken()
    const seen = []
    const unsubscribe = subscribe((next) => seen.push(next))
    setToken('a')
    setToken('b')
    unsubscribe()
    setToken('c')
    assert.deepEqual(seen, ['a', 'b'])
    clearToken()
  })

  it('collapses concurrent refreshes into one call and resets after settling', async () => {
    clearToken()
    let calls = 0
    let release
    const requestRefresh = () => {
      calls += 1
      return new Promise((resolve) => {
        release = () => resolve('fresh-token')
      })
    }

    const attempts = [
      refreshOnce(requestRefresh),
      refreshOnce(requestRefresh),
      refreshOnce(requestRefresh),
      refreshOnce(requestRefresh),
      refreshOnce(requestRefresh),
    ]
    assert.equal(calls, 1, 'five concurrent callers must share one refresh call')
    release()
    const results = await Promise.all(attempts)
    assert.deepEqual(results, Array(5).fill('fresh-token'))
    assert.equal(getToken(), 'fresh-token')

    let secondCalls = 0
    await refreshOnce(async () => {
      secondCalls += 1
      return 'next-token'
    })
    assert.equal(secondCalls, 1, 'the single-flight promise is cleared once it settles')
    assert.equal(getToken(), 'next-token')
    clearToken()
  })

  it('leaves the store usable after a rejected refresh', async () => {
    clearToken()
    setToken('still-valid')

    await assert.rejects(
      refreshOnce(async () => {
        throw new Error('refresh refused')
      }),
      /refresh refused/,
    )
    assert.equal(getToken(), 'still-valid', 'a rejected refresh must not clear the token by itself')

    let calls = 0
    const next = await refreshOnce(async () => {
      calls += 1
      return 'later-token'
    })
    assert.equal(calls, 1, 'a later attempt must actually run, not reuse the rejected promise')
    assert.equal(next, 'later-token')
    clearToken()
  })
})

describe('classifyAuthFailure', () => {
  it('renews a plain 401', () => {
    assert.equal(classifyAuthFailure(401, {}), 'renew')
  })

  it('never renews a request marked _skipAuthRetry', () => {
    assert.equal(classifyAuthFailure(401, { _skipAuthRetry: true }), 'reject')
  })

  it('never renews a request that already spent its retry', () => {
    assert.equal(classifyAuthFailure(401, { _authRetried: true }), 'reject')
  })

  it('passes a 503 through untouched (fail-closed blocklist is not a logout)', () => {
    assert.equal(classifyAuthFailure(503, {}), 'pass')
  })

  it('passes a 429 through untouched (rate limit is not a logout)', () => {
    assert.equal(classifyAuthFailure(429, {}), 'pass')
  })

  it('passes every other status, including an absent one', () => {
    assert.equal(classifyAuthFailure(500, {}), 'pass')
    assert.equal(classifyAuthFailure(undefined, {}), 'pass')
  })
})
