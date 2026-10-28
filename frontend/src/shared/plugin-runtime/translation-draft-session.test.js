import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { createTranslationDraftSession } from './translation-draft-session.js'
import { translationPairKey } from './contribution-utils.js'

function makeSession(overrides = {}) {
  return createTranslationDraftSession({
    sessionId: 's1',
    scopeRevision: 3,
    ...overrides,
  })
}

function commitHandlers(log, result) {
  return {
    commitDraft: async (input) => {
      log.push(input)
      return result
    },
    discardDraft: async (input) => {
      log.push({ discard: input })
      return true
    },
  }
}

describe('createTranslationDraftSession session identity', () => {
  it('isCurrent is true only for the exact host session and revision', () => {
    const session = makeSession()
    assert.equal(session.isCurrent({ sessionId: 's1', scopeRevision: 3 }), true)
    assert.equal(session.isCurrent({ sessionId: 's2', scopeRevision: 3 }), false)
    assert.equal(session.isCurrent({ sessionId: 's1', scopeRevision: 4 }), false)
    assert.equal(session.isCurrent(null), false)
  })
})

describe('acceptDraft (T-307-15 current-state merge)', () => {
  it('applies only empty, untouched, allowed pairs and records per-pair draft metadata', () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))

    const result = session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [
        { field: 'name', language: 'de', value: 'Name DE', reference: 'ref-1' },
        { field: 'name', language: 'fr', value: 'Nom FR', reference: 'ref-2' },
      ],
      latest: { name: { de: 'Vorhanden', fr: '' } },
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de'), translationPairKey('name', 'fr')]),
    })

    assert.deepEqual(result.applied, [{ field: 'name', language: 'fr', value: 'Nom FR' }])
    assert.deepEqual(result.skipped, [{ field: 'name', language: 'de', reason: 'existing' }])
    assert.equal(session.hasDraft('name', 'fr'), true)
    assert.equal(session.hasDraft('name', 'de'), false)
    assert.deepEqual(session.getDraft('name', 'fr'), {
      field: 'name',
      language: 'fr',
      value: 'Nom FR',
      owner: 'ai-translation',
      reference: 'ref-2',
      protected: true,
      sourceLabel: null,
    })
  })

  it('treats a typed-then-cleared pair as touched and refuses it', () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))

    const result = session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Neu', reference: 'r' }],
      latest: { name: { de: '' } },
      touched: new Set([translationPairKey('name', 'de')]),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    assert.deepEqual(result.applied, [])
    assert.deepEqual(result.skipped, [{ field: 'name', language: 'de', reason: 'touched' }])
    assert.equal(session.hasDraft('name', 'de'), false)
  })

  it('ignores a late delivery from another session or revision', () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))

    const stale = session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 'old',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'X', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })
    const oldRevision = session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 2,
      proposals: [{ field: 'name', language: 'de', value: 'X', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    assert.deepEqual(stale.applied, [])
    assert.deepEqual(oldRevision.applied, [])
    assert.equal(session.hasDraft('name', 'de'), false)
  })

  it('refuses delivery from an owner without registered handlers', () => {
    const session = makeSession()
    const result = session.acceptDraft({
      owner: 'ghost',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'X', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    assert.deepEqual(result.applied, [])
    assert.equal(session.hasDraft('name', 'de'), false)
  })
})

describe('per-pair draft identity survives edits', () => {
  it('keeps the marker and owner when the host updates the draft value', () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Alt', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    session.setDraftValue('name', 'de', 'Bearbeitet')

    assert.equal(session.hasDraft('name', 'de'), true)
    assert.equal(session.getDraft('name', 'de').value, 'Bearbeitet')
    assert.equal(session.getDraft('name', 'de').owner, 'ai-translation')
  })
})

describe('protected commit routes a marked pair to its registered handler', () => {
  it('calls commitDraft with the opaque reference and the edited value and clears on written', async () => {
    const log = []
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers(log, { status: 'written' }))
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Draft', reference: 'ref-9' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    const outcome = await session.commit('name', 'de', 'Edited value')

    assert.deepEqual(log, [{ reference: 'ref-9', value: 'Edited value', field: 'name', language: 'de' }])
    assert.equal(outcome.status, 'written')
    assert.equal(session.hasDraft('name', 'de'), false)
  })

  it('clears the draft and returns the stored value when the write is skipped', async () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'skipped', value: 'Inzwischen' }))
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Draft', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    const outcome = await session.commit('name', 'de', 'Draft')

    assert.equal(outcome.status, 'skipped')
    assert.equal(outcome.value, 'Inzwischen')
    assert.equal(session.hasDraft('name', 'de'), false)
  })

  it('keeps the draft when the write is invalid', async () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'invalid', reason: 'placeholder' }))
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Draft', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    const outcome = await session.commit('name', 'de', 'Draft')

    assert.equal(outcome.status, 'invalid')
    assert.equal(session.hasDraft('name', 'de'), true)
  })

  it('reports manual for an unmarked pair so the host keeps its Core upsert', async () => {
    const session = makeSession()
    const outcome = await session.commit('name', 'de', 'Manual')
    assert.equal(outcome.status, 'manual')
  })
})

describe('discard and owner unload', () => {
  it('discard calls the owner handler and clears on confirmed success', async () => {
    const log = []
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers(log, { status: 'written' }))
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Draft', reference: 'ref-1' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    const ok = await session.discard('name', 'de')

    assert.equal(ok, true)
    assert.deepEqual(log, [{ discard: { reference: 'ref-1', field: 'name', language: 'de' } }])
    assert.equal(session.hasDraft('name', 'de'), false)
  })

  it('keeps the draft when discard fails', async () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', {
      commitDraft: async () => ({ status: 'written' }),
      discardDraft: async () => false,
    })
    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'Draft', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })

    const ok = await session.discard('name', 'de')

    assert.equal(ok, false)
    assert.equal(session.hasDraft('name', 'de'), true)
  })

  it('unregisterOwner removes that owner drafts and handlers and never falls back to manual upsert', async () => {
    const session = makeSession()
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))
    session.registerDraftHandlers('example', commitHandlers([], { status: 'written' }))
    for (const owner of ['ai-translation', 'example']) {
      session.acceptDraft({
        owner,
        sessionId: 's1',
        scopeRevision: 3,
        proposals: [{ field: `${owner}-field`, language: 'de', value: 'Draft', reference: 'r' }],
        latest: {},
        touched: new Set(),
        allowed: new Set([translationPairKey(`${owner}-field`, 'de')]),
      })
    }

    session.unregisterOwner('ai-translation')

    assert.equal(session.hasDraft('ai-translation-field', 'de'), false)
    assert.equal(session.hasDraft('example-field', 'de'), true)
    const outcome = await session.commit('ai-translation-field', 'de', 'x')
    assert.equal(outcome.status, 'manual')
  })
})

describe('subscribers', () => {
  it('notifies a subscriber on accept, clear and unload and stops after unsubscribe', () => {
    const session = makeSession()
    let count = 0
    const unsubscribe = session.subscribe(() => { count += 1 })
    session.registerDraftHandlers('ai-translation', commitHandlers([], { status: 'written' }))

    session.acceptDraft({
      owner: 'ai-translation',
      sessionId: 's1',
      scopeRevision: 3,
      proposals: [{ field: 'name', language: 'de', value: 'X', reference: 'r' }],
      latest: {},
      touched: new Set(),
      allowed: new Set([translationPairKey('name', 'de')]),
    })
    const afterAccept = count
    assert.ok(afterAccept >= 1)

    session.clearDraft('name', 'de')
    assert.ok(count > afterAccept)

    unsubscribe()
    const afterUnsub = count
    session.unregisterOwner('ai-translation')
    assert.equal(count, afterUnsub)
  })
})
