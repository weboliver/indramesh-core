import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  shouldShowWorkerWarning,
  shouldShowDatabaseError,
  isKillSwitchInteractive,
  llmWarningKind,
  nextKillSwitchTarget,
  buildConfigFields,
  buildConfigPayload,
  coerceConfigFieldValue,
} from './plugin-admin-helpers.js'

describe('shouldShowWorkerWarning (283 D-03)', () => {
  it('is true when the manifest requires a worker update and the worker has no snapshot yet', () => {
    assert.equal(
      shouldShowWorkerWarning({
        state: 'active',
        version: '0.1.0',
        requires_worker_update: true,
        worker_loaded_at: null,
        worker_loaded_version: null,
      }),
      true
    )
  })

  it('is true when the worker snapshot carries a stale version', () => {
    assert.equal(
      shouldShowWorkerWarning({
        state: 'active',
        version: '0.2.0',
        requires_worker_update: true,
        worker_loaded_at: '2026-01-01T00:00:00Z',
        worker_loaded_version: '0.1.0',
      }),
      true
    )
  })

  it('is false when the manifest does not require a worker update', () => {
    assert.equal(
      shouldShowWorkerWarning({
        state: 'active',
        version: '0.2.0',
        requires_worker_update: false,
        worker_loaded_at: null,
        worker_loaded_version: null,
      }),
      false
    )
  })

  it('is false when the worker snapshot version matches the manifest version', () => {
    assert.equal(
      shouldShowWorkerWarning({
        state: 'active',
        version: '0.2.0',
        requires_worker_update: true,
        worker_loaded_at: '2026-01-01T00:00:00Z',
        worker_loaded_version: '0.2.0',
      }),
      false
    )
  })

  it('is false for a disabled plugin the worker never loaded', () => {
    assert.equal(
      shouldShowWorkerWarning({
        state: 'disabled',
        version: '0.2.0',
        requires_worker_update: true,
        worker_loaded_at: null,
        worker_loaded_version: null,
      }),
      false
    )
  })

  it('is false for a missing plugin', () => {
    assert.equal(shouldShowWorkerWarning(null), false)
  })
})

describe('shouldShowDatabaseError (287 D-13/D-14)', () => {
  it('is true when the plugin carries a non-empty sanitized reason', () => {
    assert.equal(
      shouldShowDatabaseError({
        name: 'example',
        database_error: 'Plugin "example" migration failed at revision 0002_add_notes.',
      }),
      true
    )
  })

  it('is false for a missing plugin', () => {
    assert.equal(shouldShowDatabaseError(undefined), false)
    assert.equal(shouldShowDatabaseError(null), false)
  })

  it('is false when the field is absent', () => {
    assert.equal(shouldShowDatabaseError({ name: 'example' }), false)
  })

  it('is false when the field is null or an empty string', () => {
    assert.equal(shouldShowDatabaseError({ database_error: null }), false)
    assert.equal(shouldShowDatabaseError({ database_error: '' }), false)
  })

  it('is false when the field is a non-string value', () => {
    assert.equal(shouldShowDatabaseError({ database_error: 0 }), false)
    assert.equal(shouldShowDatabaseError({ database_error: false }), false)
    assert.equal(shouldShowDatabaseError({ database_error: {} }), false)
    assert.equal(shouldShowDatabaseError({ database_error: [] }), false)
  })
})

describe('kill-switch gating', () => {
  it('is interactive only for active and disabled', () => {
    assert.equal(isKillSwitchInteractive('active'), true)
    assert.equal(isKillSwitchInteractive('disabled'), true)
    assert.equal(isKillSwitchInteractive('loaded'), false)
    assert.equal(isKillSwitchInteractive('error'), false)
  })

  it('maps the current state to the next target, and nothing for a non-transitionable state', () => {
    assert.equal(nextKillSwitchTarget('active'), 'disabled')
    assert.equal(nextKillSwitchTarget('disabled'), 'active')
    assert.equal(nextKillSwitchTarget('loaded'), null)
    assert.equal(nextKillSwitchTarget('error'), null)
  })
})

describe('buildConfigFields (D-16)', () => {
  const schema = {
    type: 'object',
    required: ['greeting', 'limit'],
    properties: {
      greeting: { type: 'string', default: 'hello' },
      api_token: { type: 'string', format: 'password' },
      limit: { type: 'integer', default: 5 },
      retries: { type: 'number' },
      verbose: { type: 'boolean', default: false },
      mode: {
        oneOf: [
          { const: 'fast', title: 'Fast' },
          { const: 'thorough', title: 'Thorough' },
        ],
      },
      nested: { type: 'object', properties: {} },
      list: { type: 'array', items: { type: 'string' } },
      ref: { $ref: '#/$defs/thing' },
      mixed: { anyOf: [{ type: 'string' }, { type: 'number' }] },
    },
  }

  it('keeps declaration order and maps every supported shape', () => {
    const fields = buildConfigFields(schema)

    assert.deepEqual(
      fields.map((field) => field.key),
      ['greeting', 'api_token', 'limit', 'retries', 'verbose', 'mode', 'nested', 'list', 'ref', 'mixed']
    )
    assert.deepEqual(
      fields.slice(0, 6).map((field) => field.kind),
      ['string', 'password', 'integer', 'number', 'boolean', 'enum']
    )
    assert.deepEqual(
      fields.slice(6).map((field) => field.unsupportedType),
      ['object', 'array', '$ref', 'anyOf']
    )
  })

  it('carries defaults, required flags and enum options', () => {
    const fields = buildConfigFields(schema)
    const byKey = Object.fromEntries(fields.map((field) => [field.key, field]))

    assert.equal(byKey.greeting.defaultValue, 'hello')
    assert.equal(byKey.limit.defaultValue, 5)
    assert.equal(byKey.verbose.defaultValue, false)
    assert.equal(byKey.greeting.required, true)
    assert.equal(byKey.limit.required, true)
    assert.equal(byKey.api_token.required, false)
    assert.deepEqual(byKey.mode.options, [
      { value: 'fast', label: 'Fast' },
      { value: 'thorough', label: 'Thorough' },
    ])
    assert.equal(byKey.mode.required, false)
  })

  it('maps a bare enum property to an enum field', () => {
    const fields = buildConfigFields({
      properties: { level: { enum: ['low', 'high'] } },
    })

    assert.equal(fields[0].kind, 'enum')
    assert.deepEqual(fields[0].options, [
      { value: 'low', label: 'low' },
      { value: 'high', label: 'high' },
    ])
  })

  it('returns an empty list for a missing or empty schema', () => {
    assert.deepEqual(buildConfigFields(undefined), [])
    assert.deepEqual(buildConfigFields({ type: 'object' }), [])
  })
})

describe('coerceConfigFieldValue (WR-04: DOM strings regain their declared type)', () => {
  it('resolves a numeric enum option back to its number', () => {
    const field = { options: [{ value: 1, label: '1' }, { value: 2, label: '2' }] }

    assert.equal(coerceConfigFieldValue(field, '2'), 2)
    assert.equal(coerceConfigFieldValue(field, '1'), 1)
  })

  it('resolves boolean const branches back to booleans', () => {
    const field = { options: [{ value: true, label: 'On' }, { value: false, label: 'Off' }] }

    assert.equal(coerceConfigFieldValue(field, 'true'), true)
    assert.equal(coerceConfigFieldValue(field, 'false'), false)
  })

  it('keeps string enum values unchanged', () => {
    const field = { options: [{ value: 'fast', label: 'Fast' }] }

    assert.equal(coerceConfigFieldValue(field, 'fast'), 'fast')
  })

  it('passes an unmatched value (including the empty choice) through untouched', () => {
    const field = { options: [{ value: 1, label: '1' }] }

    assert.equal(coerceConfigFieldValue(field, ''), '')
    assert.equal(coerceConfigFieldValue(field, 'other'), 'other')
  })

  it('passes non-enum fields through untouched', () => {
    assert.equal(coerceConfigFieldValue(undefined, 'x'), 'x')
    assert.equal(coerceConfigFieldValue({ kind: 'string' }, 'x'), 'x')
  })

  it('produces a schema-typed payload for a numeric enum and a boolean const branch', () => {
    const fields = buildConfigFields({
      properties: {
        level: { enum: [1, 2] },
        flag: { oneOf: [{ const: true }, { const: false }] },
      },
    })
    const draft = {
      level: coerceConfigFieldValue(fields[0], '2'),
      flag: coerceConfigFieldValue(fields[1], 'true'),
    }

    assert.deepEqual(buildConfigPayload(fields, draft, {}), { config: { level: 2, flag: true } })
  })
})

describe('buildConfigPayload (secret rule — review 285-05/MEDIUM)', () => {
  const fields = [
    { key: 'greeting', kind: 'string', defaultValue: 'hello' },
    { key: 'api_token', kind: 'password' },
    { key: 'limit', kind: 'integer', defaultValue: 5 },
  ]

  it('omits an untouched password even when the GET returned the sentinel', () => {
    const built = buildConfigPayload(
      fields,
      { greeting: 'hi', api_token: '__INDRAMESH_SECRET_UNCHANGED__', limit: 7 },
      { greeting: 'hello', api_token: '__INDRAMESH_SECRET_UNCHANGED__', limit: 5 }
    )

    assert.equal('api_token' in built.config, false)
    assert.deepEqual(built.config, { greeting: 'hi', limit: 7 })
  })

  it('omits a password that is absent from the original GET view', () => {
    const built = buildConfigPayload(
      fields,
      { greeting: 'hello', api_token: '', limit: 5 },
      { greeting: 'hello', limit: 5 }
    )

    assert.equal('api_token' in built.config, false)
  })

  it('includes a password the operator actually edited', () => {
    const built = buildConfigPayload(
      fields,
      { greeting: 'hello', api_token: 'new-secret', limit: 5 },
      { greeting: 'hello', api_token: '__INDRAMESH_SECRET_UNCHANGED__', limit: 5 }
    )

    assert.equal(built.config.api_token, 'new-secret')
  })

  it('never emits an empty-string secret', () => {
    const built = buildConfigPayload(
      fields,
      { api_token: '' },
      { api_token: '__INDRAMESH_SECRET_UNCHANGED__' }
    )

    assert.equal('api_token' in built.config, false)
    assert.equal(built.config.api_token, undefined)
  })

  it('emits every non-secret field, falling back to the schema default', () => {
    const built = buildConfigPayload(fields, { api_token: '' }, {})

    assert.deepEqual(built.config, { greeting: 'hello', limit: 5 })
  })

  it('wraps the payload in the PluginConfigUpdate shape', () => {
    assert.deepEqual(buildConfigPayload(fields, {}, {}), { config: { greeting: 'hello', limit: 5 } })
  })
})

describe('llmWarningKind (Phase 318)', () => {
  it('reports the two non-working LLM states for a plugin requiring an LLM', () => {
    assert.equal(llmWarningKind({ requires_llm: true, llm_status: 'not_configured' }), 'not_configured')
    assert.equal(llmWarningKind({ requires_llm: true, llm_status: 'misconfigured' }), 'misconfigured')
  })

  it('stays silent for a configured LLM, a plugin without the flag, or no plugin', () => {
    assert.equal(llmWarningKind({ requires_llm: true, llm_status: 'configured' }), null)
    assert.equal(llmWarningKind({ requires_llm: false, llm_status: null }), null)
    assert.equal(llmWarningKind({ requires_llm: false, llm_status: 'not_configured' }), null)
    assert.equal(llmWarningKind({ requires_llm: true, llm_status: null }), null)
    assert.equal(llmWarningKind(null), null)
  })
})
