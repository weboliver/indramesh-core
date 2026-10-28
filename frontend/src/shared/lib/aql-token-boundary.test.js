import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  findTokenStart,
  getPartialToken,
  applySuggestion,
  UNQUOTED_KINDS,
} from './aql-token-boundary.js'

const Q = '"'

describe('getPartialToken', () => {
  it('returns the partial token at a whitespace boundary', () => {
    assert.equal(getPartialToken('Name = foo', 10), 'foo')
  })

  it('treats a quoted value with an interior space as one token (AC-07/AC-09)', () => {
    assert.equal(getPartialToken(`objectType = ${Q}My Ser`, 20), `${Q}My Ser`)
  })
})

describe('findTokenStart', () => {
  it('starts after a paren/space boundary', () => {
    assert.equal(findTokenStart('(NOT Nam', 8), 5)
  })

  it('returns 0 for empty text', () => {
    assert.equal(findTokenStart('', 0), 0)
  })

  it('includes the opening quote of an unterminated string', () => {
    assert.equal(findTokenStart(`objectType = ${Q}My Ser`, 20), 13)
  })
})

describe('applySuggestion', () => {
  it('inserts a quoted field with NO trailing space', () => {
    const r = applySuggestion('obj', 3, { kind: 'field', insertText: `${Q}Name${Q}` })
    assert.equal(r.value, `${Q}Name${Q}`)
    assert.equal(r.cursor, 6)
  })

  it('inserts an operator with a trailing space', () => {
    const r = applySuggestion('Name ', 5, { kind: 'operator', insertText: '=' })
    assert.equal(r.value, 'Name = ')
    assert.equal(r.cursor, 7)
  })

  it('inserts a ref-fn with a trailing space, caret after the closing paren (D-03)', () => {
    const r = applySuggestion('object HAVING ', 14, { kind: 'ref-fn', insertText: 'inR(' })
    assert.equal(r.value, 'object HAVING inR( ')
    assert.equal(r.cursor, 19)
  })

  it('preserves text after cursorPos untouched (mid-query insertion, AC-09)', () => {
    const r = applySuggestion(`Nam = ${Q}X${Q}`, 3, { kind: 'field', insertText: `${Q}Name${Q}` })
    assert.equal(r.value, `${Q}Name${Q} = ${Q}X${Q}`)
    assert.equal(r.cursor, 6)
  })
})

describe('UNQUOTED_KINDS', () => {
  it('contains the four unquoted kinds and excludes quoted ones', () => {
    assert.ok(UNQUOTED_KINDS.has('syntax-keyword'))
    assert.ok(UNQUOTED_KINDS.has('boolean-keyword'))
    assert.ok(UNQUOTED_KINDS.has('operator'))
    assert.ok(UNQUOTED_KINDS.has('ref-fn'))
    assert.ok(!UNQUOTED_KINDS.has('field'))
    assert.ok(!UNQUOTED_KINDS.has('objectType-value'))
  })
})
