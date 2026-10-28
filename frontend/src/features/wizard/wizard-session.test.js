import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  HANDOFF_ENVELOPE_KEYS,
  MAX_ENVELOPE_BYTES,
  MAX_NAME_LENGTH,
  MAX_SNAPSHOT_BYTES,
  MAX_WARNINGS,
  MAX_WARNING_LENGTH,
  PLUGIN_DRAFT_KEYS,
  createWizardSession,
  isPlainJsonObject,
  normalizeHandoff,
  resolveReviewOrigin,
  reviewedDraftToTemplateData,
  reviewWriteRights,
} from './wizard-session.js'

const DRAFT = {
  description: 'Track servers',
  statuses: ['active'],
  object_types: [
    { name: 'Server', description: null, icon_key: 'server', attributes: [{ name: 'Host', type_string: 'string' }] },
  ],
}

describe('wizard-session constants', () => {
  it('pins the exact draft allowlist and envelope keys the host accepts', () => {
    assert.deepEqual([...PLUGIN_DRAFT_KEYS], ['object_types', 'statuses', 'description'])
    assert.deepEqual([...HANDOFF_ENVELOPE_KEYS], ['draft', 'warnings', 'suggestedName'])
  })
})

describe('normalizeHandoff (all-or-none bounded inert envelope)', () => {
  it('accepts draft + warnings + optional suggestedName and never aliases the input', () => {
    const envelope = { draft: DRAFT, warnings: ['careful'], suggestedName: 'My Schema' }
    const result = normalizeHandoff(envelope)

    assert.equal(result.ok, true)
    assert.deepEqual(result.data, envelope)
    // Deep copy: mutating the accepted data must not touch the caller's object.
    result.data.draft.object_types[0].name = 'Mutated'
    assert.equal(envelope.draft.object_types[0].name, 'Server')
  })

  it('accepts the legacy label-only Example envelope without suggestedName', () => {
    const result = normalizeHandoff({ draft: DRAFT, warnings: ['Example reference draft'] })
    assert.equal(result.ok, true)
    assert.equal(result.data.suggestedName, null)
  })

  it('rejects unknown envelope keys, a non-object draft and a non-array object_types', () => {
    assert.equal(normalizeHandoff({ draft: DRAFT, source_id: 'x' }).reason, 'unknown_envelope_field:source_id')
    assert.equal(normalizeHandoff({ draft: DRAFT, actor: 'x' }).reason, 'unknown_envelope_field:actor')
    assert.equal(normalizeHandoff({ draft: 'nope' }).reason, 'invalid_draft')
    assert.equal(normalizeHandoff({ draft: { statuses: [] } }).reason, 'invalid_draft')
    assert.equal(normalizeHandoff(null).reason, 'envelope_not_object')
    assert.equal(normalizeHandoff([]).reason, 'envelope_not_object')
  })

  it('rejects a draft key outside the strict allowlist', () => {
    const result = normalizeHandoff({ draft: { ...DRAFT, schema_name: 'sneaky' } })
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'unknown_draft_field:schema_name')
  })

  it('bounds warnings (count and length) before any state change', () => {
    assert.equal(normalizeHandoff({ draft: DRAFT, warnings: 'x' }).reason, 'invalid_warnings')
    const many = Array.from({ length: MAX_WARNINGS + 1 }, () => 'w')
    assert.equal(normalizeHandoff({ draft: DRAFT, warnings: many }).reason, 'too_many_warnings')
    const long = ['x'.repeat(MAX_WARNING_LENGTH + 1)]
    assert.equal(normalizeHandoff({ draft: DRAFT, warnings: long }).reason, 'warning_too_long')
    assert.equal(normalizeHandoff({ draft: DRAFT, warnings: [7] }).reason, 'invalid_warning')
  })

  it('bounds the optional name and the whole serialized envelope', () => {
    assert.equal(normalizeHandoff({ draft: DRAFT, suggestedName: 7 }).reason, 'invalid_name')
    assert.equal(
      normalizeHandoff({ draft: DRAFT, suggestedName: 'x'.repeat(MAX_NAME_LENGTH + 1) }).reason,
      'name_too_long'
    )

    const bigDraft = {
      object_types: [{ name: 'T', description: 'y'.repeat(MAX_ENVELOPE_BYTES) }],
    }
    assert.equal(normalizeHandoff({ draft: bigDraft }).reason, 'envelope_too_large')
  })
})

describe('isPlainJsonObject', () => {
  it('accepts only non-null non-array objects', () => {
    assert.equal(isPlainJsonObject({}), true)
    assert.equal(isPlainJsonObject([]), false)
    assert.equal(isPlainJsonObject(null), false)
    assert.equal(isPlainJsonObject('x'), false)
    assert.equal(isPlainJsonObject(7), false)
  })
})

describe('createWizardSession — retained snapshots', () => {
  it('stores and restores a detached snapshot keyed by owner/contribution within a session', () => {
    const session = createWizardSession()
    session.beginSession()
    session.mountInput('ai-schema-wizard', 'generate-schema')

    assert.equal(session.writeSnapshot('ai-schema-wizard', 'generate-schema', { description: 'hi', category: 'IT' }), true)

    const restored = session.readSnapshot('ai-schema-wizard', 'generate-schema')
    assert.deepEqual(restored, { description: 'hi', category: 'IT' })
    // Detached clone — mutating the restored value never touches the store.
    restored.description = 'mutated'
    assert.equal(session.readSnapshot('ai-schema-wizard', 'generate-schema').description, 'hi')

    // The snapshot survives an unmount/remount inside the same open session.
    session.unmountInput()
    assert.equal(session.hasAcceptedDraft(), false)
    session.mountInput('ai-schema-wizard', 'generate-schema')
    assert.deepEqual(session.readSnapshot('ai-schema-wizard', 'generate-schema'), { description: 'hi', category: 'IT' })
  })

  it('isolates snapshots across owners/ids and rejects writes from a non-current input', () => {
    const session = createWizardSession()
    session.beginSession()
    session.mountInput('owner-a', 'entry-1')

    assert.equal(session.writeSnapshot('owner-a', 'entry-1', { v: 1 }), true)
    assert.equal(session.writeSnapshot('owner-b', 'entry-1', { v: 2 }), false)
    assert.equal(session.writeSnapshot('owner-a', 'entry-2', { v: 2 }), false)
    assert.equal(session.readSnapshot('owner-b', 'entry-1'), null)

    // After unmount no input is current, so a late callback is dropped.
    session.unmountInput()
    assert.equal(session.writeSnapshot('owner-a', 'entry-1', { v: 3 }), false)
    assert.deepEqual(session.readSnapshot('owner-a', 'entry-1'), { v: 1 })
  })

  it('rejects a malformed or oversized snapshot without storing it', () => {
    const session = createWizardSession()
    session.beginSession()
    session.mountInput('o', 'i')

    assert.equal(session.writeSnapshot('o', 'i', 'nope'), false)
    assert.equal(session.writeSnapshot('o', 'i', [1, 2, 3]), false)
    assert.equal(session.writeSnapshot('o', 'i', { big: 'x'.repeat(MAX_SNAPSHOT_BYTES) }), false)
    assert.equal(session.readSnapshot('o', 'i'), null)
  })
})

describe('createWizardSession — guarded attempts', () => {
  it('delivers one accepted handoff with inert origin and returns a unique signal per attempt', () => {
    const commits = []
    const session = createWizardSession({ commitAccepted: (data, origin) => commits.push({ data, origin }) })
    session.beginSession()
    session.mountInput('ai-schema-wizard', 'generate-schema')

    const a = session.beginAttempt()
    const b = session.beginAttempt()
    assert.notEqual(a.id, b.id)
    assert.notEqual(a.signal, b.signal)
    // Starting a second attempt invalidated the first.
    assert.equal(a.isCurrent(), false)

    const result = b.acceptDraft({ draft: DRAFT, warnings: [], suggestedName: 'Suggested' })
    assert.equal(result.ok, true)
    assert.equal(commits.length, 1)
    assert.deepEqual(commits[0].origin, { owner: 'ai-schema-wizard', contributionId: 'generate-schema' })
    assert.equal(commits[0].data.suggestedName, 'Suggested')
    assert.equal(session.hasAcceptedDraft(), true)
    // A duplicate acceptance from the same attempt is stale — no second commit.
    assert.equal(b.acceptDraft({ draft: DRAFT }).reason, 'stale')
    assert.equal(commits.length, 1)
  })

  it('drops a late success or error after invalidation, a newer attempt, or a new session', () => {
    const commits = []
    const session = createWizardSession({ commitAccepted: (data) => commits.push(data) })
    session.beginSession()
    session.mountInput('o', 'i')

    const attempt = session.beginAttempt()
    session.invalidateAttempt()
    assert.equal(attempt.isCurrent(), false)
    assert.equal(attempt.acceptDraft({ draft: DRAFT }).reason, 'stale')
    assert.equal(attempt.reportError({ code: 'timeout' }).reason, 'stale')
    assert.equal(commits.length, 0)

    const newer = session.beginAttempt()
    // A callback captured from an older attempt cannot touch state after a newer one.
    assert.equal(attempt.reportError({ code: 'timeout' }).reason, 'stale')
    assert.equal(newer.isCurrent(), true)

    // A new session (close/reopen) invalidates outstanding attempts.
    session.beginSession()
    assert.equal(newer.isCurrent(), false)
    assert.equal(newer.acceptDraft({ draft: DRAFT }).reason, 'stale')
    assert.equal(commits.length, 0)
  })

  it('a current error consumes the attempt so a later success cannot commit', () => {
    const commits = []
    const session = createWizardSession({ commitAccepted: (data) => commits.push(data) })
    session.beginSession()
    session.mountInput('o', 'i')

    const attempt = session.beginAttempt()
    assert.deepEqual(attempt.reportError({ code: 'unreachable' }), { ok: true, error: { code: 'unreachable' } })
    assert.equal(attempt.isCurrent(), false)
    assert.equal(attempt.acceptDraft({ draft: DRAFT }).reason, 'stale')
    assert.equal(commits.length, 0)
  })

  it('does not commit a malformed or oversized handoff', () => {
    const commits = []
    const session = createWizardSession({ commitAccepted: (data) => commits.push(data) })
    session.beginSession()
    session.mountInput('o', 'i')

    const attempt = session.beginAttempt()
    assert.equal(attempt.acceptDraft({ draft: DRAFT, source_id: 'x' }).ok, false)
    assert.equal(commits.length, 0)
    // The attempt is still current after a rejected envelope, so a corrected one can commit.
    assert.equal(attempt.isCurrent(), true)

    const bigDraft = { object_types: [{ name: 'T', description: 'y'.repeat(MAX_ENVELOPE_BYTES) }] }
    assert.equal(attempt.acceptDraft({ draft: bigDraft }).ok, false)
    assert.equal(commits.length, 0)

    assert.equal(attempt.acceptDraft({ draft: DRAFT }).ok, true)
    assert.equal(commits.length, 1)
  })
})

describe('reviewWriteRights (S6 defensive write gate)', () => {
  it('grants each write action only on an explicit true right', () => {
    assert.deepEqual(reviewWriteRights(true, true), { showCreateSchema: true, showSaveTemplate: true })
    assert.deepEqual(reviewWriteRights(true, false), { showCreateSchema: true, showSaveTemplate: false })
    assert.deepEqual(reviewWriteRights(false, true), { showCreateSchema: false, showSaveTemplate: true })
    assert.deepEqual(reviewWriteRights(undefined, null), { showCreateSchema: false, showSaveTemplate: false })
  })
})

describe('resolveReviewOrigin (owner/id reacquisition)', () => {
  const entries = [
    { pluginName: 'example', id: 'entry-1' },
    { pluginName: 'ai-schema-wizard', id: 'generate-schema' },
  ]

  it('returns the offered entry matching the origin owner and id', () => {
    assert.equal(
      resolveReviewOrigin({ pluginName: 'ai-schema-wizard', id: 'generate-schema' }, entries),
      entries[1]
    )
  })

  it('returns null when the origin is no longer offered or inputs are missing', () => {
    assert.equal(resolveReviewOrigin({ pluginName: 'example', id: 'gone' }, entries), null)
    assert.equal(resolveReviewOrigin(null, entries), null)
    assert.equal(resolveReviewOrigin({ pluginName: 'example', id: 'entry-1' }, null), null)
  })
})

describe('reviewedDraftToTemplateData (Core-owned library format)', () => {
  it('serializes the Core-final name/description, selected tree and statuses', () => {
    const result = reviewedDraftToTemplateData({
      schemaName: '  Server Fleet  ',
      schemaDescription: 'Track servers',
      objectTypes: [{ name: 'Server', attributes: [{ name: 'Host', type_string: 'string' }] }],
      statuses: [{ name: 'Active' }],
    })

    assert.deepEqual(result, {
      schema_name: 'Server Fleet',
      schema_description: 'Track servers',
      object_types: [{ name: 'Server', attributes: [{ name: 'Host', type_string: 'string' }] }],
      statuses: [{ name: 'Active' }],
    })
  })

  it('falls back to Untitled Schema only when no Core name is supplied', () => {
    assert.equal(reviewedDraftToTemplateData({ objectTypes: [] }).schema_name, 'Untitled Schema')
    assert.equal(
      reviewedDraftToTemplateData({ schemaName: '   ', objectTypes: [] }).schema_name,
      'Untitled Schema'
    )
  })

  it('keeps only the four library fields — no authority/session/plugin knowledge', () => {
    const result = reviewedDraftToTemplateData({
      schemaName: 'Fleet',
      schemaDescription: null,
      objectTypes: [],
      statuses: [],
      suggestedName: 'plugin suggestion',
      source_id: 'core-source',
      provenance: { owner: 'ai-schema-wizard' },
    })

    assert.deepEqual(Object.keys(result), ['schema_name', 'schema_description', 'object_types', 'statuses'])
    assert.equal(result.schema_description, null)
  })

  it('defaults missing collections to empty arrays', () => {
    const result = reviewedDraftToTemplateData({ schemaName: 'Fleet' })
    assert.deepEqual(result.object_types, [])
    assert.deepEqual(result.statuses, [])
  })
})

describe('createWizardSession — lifecycle', () => {
  it('clears snapshots and accepted data on beginSession/endSession', () => {
    const session = createWizardSession()
    session.beginSession()
    session.mountInput('o', 'i')
    session.writeSnapshot('o', 'i', { v: 1 })
    session.beginAttempt().acceptDraft({ draft: DRAFT })
    assert.equal(session.hasAcceptedDraft(), true)

    session.endSession()
    assert.equal(session.hasAcceptedDraft(), false)
    assert.equal(session.getAcceptedDraft(), null)
    assert.equal(session.readSnapshot('o', 'i'), null)

    session.beginSession()
    assert.equal(session.readSnapshot('o', 'i'), null)
  })
})
