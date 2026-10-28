import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { DEFAULT_PASSWORD_POLICY, passwordViolations } from './password-policy.js'

const STRICT = {
  min_length: 12,
  max_length: 40,
  require_digit: true,
  require_special: true,
  require_uppercase: true,
}

describe('passwordViolations', () => {
  it('needs eight characters by default', () => {
    assert.deepEqual(passwordViolations('short'), ['min_length'])
    assert.deepEqual(passwordViolations('longenough', DEFAULT_PASSWORD_POLICY), [])
  })

  it('reports every missing character class of a strict policy', () => {
    assert.deepEqual(passwordViolations('lowercaseonlyx', STRICT), ['digit', 'uppercase', 'special'])
    assert.deepEqual(passwordViolations('Valid-Passw0rd', STRICT), [])
  })

  it('counts bytes for the bcrypt limit', () => {
    assert.ok(passwordViolations('ä'.repeat(40)).includes('max_bytes'))
  })
})
