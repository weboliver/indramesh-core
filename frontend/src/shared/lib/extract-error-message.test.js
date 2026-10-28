import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { extractErrorMessage } from './utils.js'

describe('extractErrorMessage', () => {
  it('returns a string detail as-is', () => {
    const err = { response: { data: { detail: 'Saved query not found' } } }
    assert.equal(extractErrorMessage(err), 'Saved query not found')
  })

  it('joins an array of FastAPI validation errors', () => {
    const err = {
      response: {
        data: {
          detail: [{ msg: 'field required' }, { msg: 'not a valid uuid' }, {}],
        },
      },
    }
    assert.equal(extractErrorMessage(err), 'field required; not a valid uuid')
  })

  it('returns the message of a structured object detail', () => {
    // The saved-query 422 payload: the reason lives in `message`, next to the
    // machine-readable limit fields.
    const err = {
      message: 'Request failed with status code 422',
      response: {
        data: {
          detail: {
            path: 'query_text',
            message: 'Query uses 21 aliases; the allowed maximum is 20',
            code: 'QUERY_TOO_COMPLEX',
            limit: 'aliases',
            actual: 21,
            allowed: 20,
          },
        },
      },
    }
    assert.equal(
      extractErrorMessage(err),
      'Query uses 21 aliases; the allowed maximum is 20'
    )
  })

  it('falls back to err.message when the object detail carries no message', () => {
    const err = {
      message: 'Request failed with status code 422',
      response: { data: { detail: { path: 'query_text' } } },
    }
    assert.equal(extractErrorMessage(err), 'Request failed with status code 422')
  })

  it('falls back when the response carries no detail at all', () => {
    const err = { message: 'Network Error' }
    assert.equal(extractErrorMessage(err), 'Network Error')
    assert.equal(
      extractErrorMessage({}, 'An unexpected error occurred.'),
      'An unexpected error occurred.'
    )
  })
})
