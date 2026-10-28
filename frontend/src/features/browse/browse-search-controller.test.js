import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { resolveQueryAction } from './browse-search-controller.js'

describe('resolveQueryAction', () => {
  it('sets a query of two or more characters', () => {
    assert.deepEqual(resolveQueryAction('ab', ''), { type: 'set', value: 'ab' })
  })

  it('sets an empty value (clearing)', () => {
    assert.deepEqual(resolveQueryAction('', 'ab'), { type: 'set', value: '' })
  })

  it('does nothing for a one-character query when no q is committed', () => {
    assert.deepEqual(resolveQueryAction('a', ''), { type: 'none' })
  })

  it('treats a one-character backspace from a committed q as clearing', () => {
    assert.deepEqual(resolveQueryAction('a', 'ab'), { type: 'set', value: '' })
  })
})