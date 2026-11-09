import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { SLOW_LOGIN_HINT_MS, NOTICE_DEFAULTS, noticeKeyForSessionParam } from './login-states.js'

describe('noticeKeyForSessionParam', () => {
  it('maps the expired session cause to its notice key', () => {
    assert.equal(noticeKeyForSessionParam('expired'), 'auth.session_expired')
  })

  it('maps the own-password-change cause to its notice key', () => {
    assert.equal(noticeKeyForSessionParam('password-changed'), 'auth.password_changed_sign_in')
  })

  it('renders no notice for an absent or empty parameter', () => {
    assert.equal(noticeKeyForSessionParam(null), null)
    assert.equal(noticeKeyForSessionParam(undefined), null)
    assert.equal(noticeKeyForSessionParam(''), null)
  })

  it('renders no notice for a padded or differently cased value', () => {
    assert.equal(noticeKeyForSessionParam('expired '), null)
    assert.equal(noticeKeyForSessionParam(' expired'), null)
    assert.equal(noticeKeyForSessionParam('EXPIRED'), null)
    assert.equal(noticeKeyForSessionParam('Password-Changed'), null)
  })

  it('renders no notice for an unknown value', () => {
    assert.equal(noticeKeyForSessionParam('logged-out'), null)
    assert.equal(noticeKeyForSessionParam('expired=1'), null)
  })
})

describe('SLOW_LOGIN_HINT_MS', () => {
  it('is the single 2 s delay used by the slow-login hint', () => {
    assert.equal(SLOW_LOGIN_HINT_MS, 2000)
  })
})

describe('NOTICE_DEFAULTS', () => {
  it('gives every notice key a sentence, so the English render never shows the key', () => {
    for (const cause of ['expired', 'password-changed']) {
      const key = noticeKeyForSessionParam(cause)
      const text = NOTICE_DEFAULTS[key]
      assert.equal(typeof text, 'string', `${key} has no default text`)
      assert.ok(text.length > 0, `${key} has empty default text`)
      assert.notEqual(text, key, `${key} still renders its own key`)
    }
  })
})
