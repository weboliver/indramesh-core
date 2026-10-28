import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { filterCatalogRows } from './translation-catalog-search.js'

describe('filterCatalogRows', () => {
  const rows = [
    { id: 'a', namespace: 'common', key: 'save', default_text: 'Save', effective_value: 'Speichern' },
    { id: 'b', namespace: 'nav', key: 'configuration', default_text: 'Configuration', effective_value: 'Configuration' },
    { id: 'c', namespace: 'admin', key: 'groups.title', default_text: 'Groups', effective_value: 'Gruppen' },
  ]

  it('returns all rows when q is empty', () => {
    assert.equal(filterCatalogRows(rows, '').length, 3)
  })

  it('matches on the key', () => {
    assert.deepEqual(filterCatalogRows(rows, 'configuration'), [rows[1]])
  })

  it('matches on the namespace', () => {
    assert.deepEqual(filterCatalogRows(rows, 'admin'), [rows[2]])
  })

  it('matches on the default text', () => {
    assert.deepEqual(filterCatalogRows(rows, 'groups'), [rows[2]])
  })

  it('matches on the effective (stored) value', () => {
    assert.deepEqual(filterCatalogRows(rows, 'speichern'), [rows[0]])
  })

  it('is case-insensitive and trims', () => {
    assert.equal(filterCatalogRows(rows, '  SAVE  ').length, 1)
  })
})
