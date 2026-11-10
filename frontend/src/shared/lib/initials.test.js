import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { getInitials } from './initials.js'

describe('getInitials', () => {
  it('takes the first letter of both names', () => {
    assert.equal(getInitials({ first_name: 'Anna', last_name: 'Neumann' }), 'AN')
  })

  it('takes the single available name', () => {
    assert.equal(getInitials({ first_name: 'Anna', last_name: '' }), 'A')
    assert.equal(getInitials({ first_name: '', last_name: 'Neumann' }), 'N')
    assert.equal(getInitials({ first_name: null, last_name: 'Neumann' }), 'N')
  })

  it('falls back to the username when no name is set', () => {
    assert.equal(getInitials({ username: 'oliver' }), 'O')
    assert.equal(getInitials({ first_name: '', last_name: '', username: 'oliver' }), 'O')
  })

  it('renders a question mark only when nothing is set', () => {
    assert.equal(getInitials({}), '?')
    assert.equal(getInitials({ first_name: '', last_name: '', username: '' }), '?')
    assert.equal(getInitials(), '?')
  })

  it('treats whitespace-only input as absent', () => {
    assert.equal(getInitials({ first_name: '   ', last_name: ' \t ', username: 'oliver' }), 'O')
    assert.equal(getInitials({ first_name: '  ', last_name: '   ' }), '?')
  })

  it('normalises a decomposed diacritic to one letter', () => {
    assert.equal(getInitials({ first_name: 'a\u0308' }), 'Ä')
    assert.equal(getInitials({ first_name: 'a\u0308', last_name: 'Neumann' }), 'ÄN')
  })

  it('never renders more than two characters', () => {
    assert.equal([...getInitials({ first_name: 'Anna', last_name: 'Neumann' })].length, 2)
    assert.equal(getInitials({ first_name: 'ß' }), 'SS')
    assert.equal(getInitials({ first_name: 'ß', last_name: 'Neumann' }), 'SS')
    assert.equal(getInitials({ first_name: 'A'.repeat(100) }), 'A')
    assert.equal([...getInitials({ first_name: 'A'.repeat(100), last_name: 'B'.repeat(100) })].length, 2)
  })

  it('takes a whole code point instead of a lone surrogate', () => {
    const emoji = getInitials({ first_name: '😀' })
    assert.equal(emoji, '😀')
    assert.equal([...emoji].length, 1, 'a lone surrogate would split into two code points')
    assert.equal(getInitials({ first_name: '😀Anna' }), '😀')

    const astralCjk = getInitials({ first_name: '\u{20000}name' })
    assert.equal([...astralCjk].length, 1)
    assert.equal(astralCjk, '\u{20000}')
  })
})
