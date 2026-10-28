import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { filterAuditEntries } from './audit-search.js'

describe('filterAuditEntries', () => {
  const entries = [
    { entity_type: 'sources', action: 'CREATE', actor_display_name: 'Alice Admin' },
    { entity_type: 'schemas', action: 'UPDATE', actor_display_name: 'Bob Builder' },
    { entity_type: 'objects', action: 'DELETE', actor_display_name: 'Alice Admin' },
  ]
  const label = (type) => ({ sources: 'Sources', schemas: 'Schemas', objects: 'Objects' })[type] || type

  it('returns all entries when q is empty', () => {
    assert.equal(filterAuditEntries(entries, '', label).length, 3)
  })

  it('filters by actor display name', () => {
    assert.deepEqual(filterAuditEntries(entries, 'bob', label), [entries[1]])
  })

  it('filters by action', () => {
    assert.deepEqual(filterAuditEntries(entries, 'delete', label), [entries[2]])
  })

  it('filters by the translated entity label', () => {
    assert.deepEqual(filterAuditEntries(entries, 'sources', label), [entries[0]])
  })

  it('is case-insensitive', () => {
    assert.equal(filterAuditEntries(entries, 'ALICE', label).length, 2)
  })

  it('returns an empty array when nothing matches', () => {
    assert.deepEqual(filterAuditEntries(entries, 'zzz', label), [])
  })
})
