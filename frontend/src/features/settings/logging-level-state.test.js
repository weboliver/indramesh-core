import test from 'node:test'
import assert from 'node:assert/strict'

import {
  LOG_LEVEL_NAMES,
  DEFAULT_EXPIRY_MINUTES,
  parseExpiryMinutes,
  requiresExpiry,
  buildLevelUpdate,
  remainingDuration,
  formatRemaining,
  expiryRefetchKey,
} from './logging-level-state.js'

test('LOG_LEVEL_NAMES keeps the frozen severity order and spelling', () => {
  assert.deepEqual(LOG_LEVEL_NAMES, ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
})

test('parseExpiryMinutes accepts only whole minutes in 1..1440', () => {
  assert.equal(parseExpiryMinutes('1'), 1)
  assert.equal(parseExpiryMinutes('30'), 30)
  assert.equal(parseExpiryMinutes('1440'), 1440)
  assert.equal(parseExpiryMinutes(30), 30)
  assert.equal(parseExpiryMinutes(' 30 '), 30)

  assert.equal(parseExpiryMinutes('0'), null)
  assert.equal(parseExpiryMinutes('1441'), null)
  assert.equal(parseExpiryMinutes('1.5'), null)
  assert.equal(parseExpiryMinutes('-1'), null)
  assert.equal(parseExpiryMinutes(''), null)
  assert.equal(parseExpiryMinutes('abc'), null)
  assert.equal(parseExpiryMinutes(null), null)
  assert.equal(parseExpiryMinutes(undefined), null)
  assert.equal(DEFAULT_EXPIRY_MINUTES, 30)
})

test('requiresExpiry compares severity against the server threshold, not the base level', () => {
  // db without env: base INFO (20), threshold WARNING (30)
  assert.equal(requiresExpiry('DEBUG', 30), true)
  assert.equal(requiresExpiry('INFO', 30), true)
  assert.equal(requiresExpiry('WARNING', 30), false)
  assert.equal(requiresExpiry('ERROR', 30), false)
  // a threshold equal to the base makes only stricter picks draft-free
  assert.equal(requiresExpiry('DEBUG', 20), true)
  assert.equal(requiresExpiry('INFO', 20), false)
  // an unknown level or non-numeric threshold never demands a draft
  assert.equal(requiresExpiry('NOTSET', 30), false)
  assert.equal(requiresExpiry('DEBUG', undefined), false)
})

test('buildLevelUpdate picks the base level back to an explicit clear', () => {
  assert.deepEqual(
    buildLevelUpdate({ level: 'INFO', baseLevel: 'INFO', ttlRequiredBelow: 20 }),
    { action: 'clear', value: null }
  )
})

test('buildLevelUpdate opens a draft below the threshold and sends nothing', () => {
  assert.deepEqual(
    buildLevelUpdate({ level: 'DEBUG', baseLevel: 'INFO', ttlRequiredBelow: 30 }),
    { action: 'draft', value: null }
  )
})

test('buildLevelUpdate above the threshold updates immediately without a TTL', () => {
  assert.deepEqual(
    buildLevelUpdate({ level: 'WARNING', baseLevel: 'INFO', ttlRequiredBelow: 20 }),
    { action: 'update', value: { level: 'WARNING' } }
  )
})

test('buildLevelUpdate apply sends strict ttl_seconds minutes*60 for 1/30/1440', () => {
  const base = { level: 'DEBUG', baseLevel: 'INFO', ttlRequiredBelow: 30 }

  assert.deepEqual(buildLevelUpdate({ ...base, minutes: '1' }), {
    action: 'update',
    value: { level: 'DEBUG', ttl_seconds: 60 },
  })
  assert.deepEqual(buildLevelUpdate({ ...base, minutes: '30' }), {
    action: 'update',
    value: { level: 'DEBUG', ttl_seconds: 1800 },
  })
  assert.deepEqual(buildLevelUpdate({ ...base, minutes: '1440' }), {
    action: 'update',
    value: { level: 'DEBUG', ttl_seconds: 86400 },
  })
})

test('buildLevelUpdate never depends on the client clock', () => {
  const base = { level: 'DEBUG', baseLevel: 'INFO', ttlRequiredBelow: 30, minutes: '30' }
  const originalNow = Date.now
  try {
    Date.now = () => 0
    const early = buildLevelUpdate(base)
    Date.now = () => 1_700_000_000_000
    const late = buildLevelUpdate(base)
    assert.deepEqual(early, late)
    assert.deepEqual(early.value, { level: 'DEBUG', ttl_seconds: 1800 })
  } finally {
    Date.now = originalNow
  }
})

test('buildLevelUpdate rejects invalid apply minutes without producing a body', () => {
  assert.deepEqual(
    buildLevelUpdate({ level: 'DEBUG', baseLevel: 'INFO', ttlRequiredBelow: 30, minutes: '0' }),
    { action: 'invalid', value: null }
  )
  assert.deepEqual(
    buildLevelUpdate({ level: 'DEBUG', baseLevel: 'INFO', ttlRequiredBelow: 30, minutes: '1.5' }),
    { action: 'invalid', value: null }
  )
})

test('remainingDuration decrements from the server-observed seconds', () => {
  assert.equal(remainingDuration({ remainingSeconds: 120, loadedAt: 1000, now: 1000 }), 120)
  assert.equal(remainingDuration({ remainingSeconds: 120, loadedAt: 1000, now: 5000 }), 116)
  assert.equal(remainingDuration({ remainingSeconds: 120, loadedAt: 1000, now: 121_000 }), 0)
  assert.equal(remainingDuration({ remainingSeconds: null, loadedAt: 1000, now: 5000 }), null)
})

test('formatRemaining uses MM:SS under an hour and HH:MM:SS above', () => {
  assert.equal(formatRemaining(0), '00:00')
  assert.equal(formatRemaining(9), '00:09')
  assert.equal(formatRemaining(65), '01:05')
  assert.equal(formatRemaining(3599), '59:59')
  assert.equal(formatRemaining(3600), '01:00:00')
  assert.equal(formatRemaining(3661), '01:01:01')
  assert.equal(formatRemaining(-5), '00:00')
})

test('expiryRefetchKey identifies one category+expiry+server-snapshot target', () => {
  const a = expiryRefetchKey('db', '2026-10-05T10:00:00Z', '2026-10-05T09:59:00Z')
  assert.equal(a, expiryRefetchKey('db', '2026-10-05T10:00:00Z', '2026-10-05T09:59:00Z'))
  assert.notEqual(a, expiryRefetchKey('db', '2026-10-05T10:01:00Z', '2026-10-05T09:59:00Z'))
  assert.notEqual(a, expiryRefetchKey('db', '2026-10-05T10:00:00Z', '2026-10-05T10:00:00Z'))
  assert.notEqual(a, expiryRefetchKey('llm', '2026-10-05T10:00:00Z', '2026-10-05T09:59:00Z'))
})
