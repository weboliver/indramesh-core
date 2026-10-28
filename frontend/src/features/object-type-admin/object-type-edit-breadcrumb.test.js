import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { buildEditObjectTypeBreadcrumbItems } from './object-type-edit-breadcrumb.js'

describe('buildEditObjectTypeBreadcrumbItems (EditObjectType navigation chain, Phase 252 UX-07)', () => {
  it('builds overview → loaded ObjectType → Edit items with deterministic hrefs', () => {
    const items = buildEditObjectTypeBreadcrumbItems({
      overviewLabel: 'Object Types',
      name: 'Server',
      id: 'ot-1',
      editLabel: 'Edit',
    })
    assert.equal(items.length, 3)
    assert.deepEqual(items[0], { label: 'Object Types', href: '/browse/object-types' })
    assert.deepEqual(items[1], { label: 'Server', href: '/browse/object-types/ot-1' })
  })

  it('renders the final Edit item as the current page (no href)', () => {
    const items = buildEditObjectTypeBreadcrumbItems({
      overviewLabel: 'Object Types',
      name: 'Server',
      id: 'ot-1',
      editLabel: 'Edit',
    })
    assert.deepEqual(items[2], { label: 'Edit' })
    assert.ok(!('href' in items[2]))
  })

  it('uses the loaded ObjectType name, never the raw id, as the hierarchy label', () => {
    const items = buildEditObjectTypeBreadcrumbItems({
      overviewLabel: 'Object Types',
      name: 'Server',
      id: 'ot-1',
      editLabel: 'Edit',
    })
    assert.equal(items[1].label, 'Server')
    assert.ok(!items[1].label.includes('ot-1'))
  })

  it('degrades to overview → Edit when the ObjectType name is not loaded', () => {
    const items = buildEditObjectTypeBreadcrumbItems({
      overviewLabel: 'Object Types',
      name: '',
      id: 'ot-1',
      editLabel: 'Edit',
    })
    assert.equal(items.length, 2)
    assert.deepEqual(items[1], { label: 'Edit' })
  })
})