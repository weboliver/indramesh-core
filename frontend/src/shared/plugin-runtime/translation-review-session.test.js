import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { createTranslationReviewSession } from './translation-review-session.js'

function rowsOf(count) {
  return Array.from({ length: count }, (_, index) => ({
    uiStringId: `id-${index}`,
    namespace: index % 2 === 0 ? 'nav' : 'admin',
    key: `key.${String(index).padStart(3, '0')}`,
    codeDefault: `Default ${index}`,
  }))
}

describe('createTranslationReviewSession scope fencing', () => {
  it('accepts a current-scope publish and rejects a stale revision', () => {
    const session = createTranslationReviewSession({ scopeRevision: 7 })

    assert.equal(
      session.publishReview({ owner: 'ai-translation', scopeRevision: 6, rows: rowsOf(1), total: 1 }),
      false
    )
    assert.equal(session.isActive(), false)

    assert.equal(
      session.publishReview({ owner: 'ai-translation', scopeRevision: 7, rows: rowsOf(1), total: 1 }),
      true
    )
    assert.equal(session.isActive(), true)
  })
})

describe('publishReview projection', () => {
  it('orders rows by namespace then key and caps the block at 100', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({ owner: 'ai-translation', scopeRevision: 1, rows: rowsOf(150), total: 150 })

    const review = session.getReview()
    assert.equal(review.rows.length, 100)
    assert.equal(review.total, 150)

    const firstNamespaces = review.rows.slice(0, 4).map((row) => row.namespace)
    assert.deepEqual(firstNamespaces, ['admin', 'admin', 'admin', 'admin'])
    const keys = review.rows.map((row) => row.key)
    const ordered = [...review.rows].sort((a, b) => {
      const ns = a.namespace < b.namespace ? -1 : a.namespace > b.namespace ? 1 : 0
      if (ns !== 0) return ns
      return a.key < b.key ? -1 : a.key > b.key ? 1 : 0
    })
    assert.deepEqual(keys, ordered.map((row) => row.key))
  })

  it('merges drafts onto their row by uiStringId and exposes them', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: rowsOf(2),
      drafts: [
        { uiStringId: 'id-0', value: 'Entwurf 0', sourceLabel: 'Translate with AI', reasonText: null, invalid: false, reference: 'ref-0' },
      ],
      total: 2,
    })

    const review = session.getReview()
    const withDraft = review.rows.find((row) => row.uiStringId === 'id-0')
    const withoutDraft = review.rows.find((row) => row.uiStringId === 'id-1')
    assert.equal(withDraft.draft.value, 'Entwurf 0')
    assert.equal(withDraft.draft.sourceLabel, 'Translate with AI')
    assert.equal(withDraft.draft.reference, 'ref-0')
    assert.equal(withoutDraft.draft, null)
    assert.deepEqual(review.drafts.map((draft) => draft.uiStringId), ['id-0'])
  })

  it('ignores a row without a uiStringId and an empty publish deactivates the view', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: [{ namespace: 'nav', key: 'k', codeDefault: 'D' }, ...rowsOf(1)],
      total: 1,
    })
    assert.equal(session.getReview().rows.length, 1)

    session.publishReview({ owner: 'ai-translation', scopeRevision: 1, rows: [], total: 0 })
    assert.equal(session.isActive(), false)
  })
})

describe('review edits stay local until apply', () => {
  it('setDraftValue updates the row draft and getDrafts reflects it', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: rowsOf(1),
      drafts: [{ uiStringId: 'id-0', value: 'Alt', sourceLabel: null, reference: 'r' }],
      total: 1,
    })

    session.setDraftValue('id-0', 'Bearbeitet')

    assert.equal(session.getReview().rows[0].draft.value, 'Bearbeitet')
    assert.equal(session.getDrafts()[0].value, 'Bearbeitet')
  })

  it('removeRow drops a row and deactivates when the last row is removed', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: rowsOf(2),
      drafts: [{ uiStringId: 'id-0', value: 'A', reference: 'r' }],
      total: 2,
    })

    session.removeRow('id-0')
    assert.deepEqual(session.getReview().rows.map((row) => row.uiStringId), ['id-1'])
    assert.deepEqual(session.getDrafts(), [])

    session.removeRow('id-1')
    assert.equal(session.isActive(), false)
  })

  it('leaveReview clears the block but keeps the session usable', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    session.publishReview({ owner: 'ai-translation', scopeRevision: 1, rows: rowsOf(1), total: 1 })
    session.leaveReview()
    assert.equal(session.isActive(), false)
    assert.equal(session.getReview(), null)
  })
})

describe('table edits and discards go back to the owner', () => {
  function publishWithOwner(session, handlers) {
    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: rowsOf(2),
      drafts: [
        { uiStringId: 'id-0', value: 'A', reference: 'r0' },
        { uiStringId: 'id-1', value: 'B', reference: 'r1' },
      ],
      total: 2,
      ...handlers,
    })
  }

  it('editDraft updates the projection and notifies the owner', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    const calls = []
    publishWithOwner(session, { onDraftChange: (id, value) => calls.push([id, value]) })

    assert.equal(session.editDraft('id-0', 'Neu'), true)

    assert.equal(session.getReview().rows.find((row) => row.uiStringId === 'id-0').draft.value, 'Neu')
    assert.deepEqual(calls, [['id-0', 'Neu']])
  })

  it('discardRow removes the row only after the owner confirms', async () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    let confirm = false
    publishWithOwner(session, { onDiscard: async () => confirm })

    assert.equal(await session.discardRow('id-0'), false)
    assert.equal(session.getReview().rows.length, 2)

    confirm = true
    assert.equal(await session.discardRow('id-0'), true)
    assert.deepEqual(session.getReview().rows.map((row) => row.uiStringId), ['id-1'])
  })

  it('discardRow keeps the row when the owner throws or no handler was published', async () => {
    const throwing = createTranslationReviewSession({ scopeRevision: 1 })
    publishWithOwner(throwing, { onDiscard: async () => { throw new Error('boom') } })
    assert.equal(await throwing.discardRow('id-0'), false)
    assert.equal(throwing.getReview().rows.length, 2)

    const bare = createTranslationReviewSession({ scopeRevision: 1 })
    publishWithOwner(bare, {})
    assert.equal(await bare.discardRow('id-0'), false)
    assert.equal(bare.getReview().rows.length, 2)
  })
})

describe('subscribers', () => {
  it('notifies on publish, edit, remove and leave and stops after unsubscribe', () => {
    const session = createTranslationReviewSession({ scopeRevision: 1 })
    let count = 0
    const unsubscribe = session.subscribe(() => { count += 1 })

    session.publishReview({
      owner: 'ai-translation',
      scopeRevision: 1,
      rows: rowsOf(1),
      drafts: [{ uiStringId: 'id-0', value: 'A', reference: 'r' }],
      total: 1,
    })
    assert.ok(count >= 1)
    session.setDraftValue('id-0', 'B')
    const afterEdit = count
    assert.ok(afterEdit >= 2)

    unsubscribe()
    session.leaveReview()
    assert.equal(count, afterEdit)
  })
})
