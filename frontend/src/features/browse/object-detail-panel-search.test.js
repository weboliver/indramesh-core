import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  filterAttributeAndRefRows,
  filterInboundGroups,
} from './object-detail-panel-search.js'
import { buildObjectTypeBreadcrumbItems, buildTypeChain } from './object-type-breadcrumb-items.js'

describe('filterAttributeAndRefRows (ObjectDetailPanel Attributes & References, manifest surface 08 — local loaded-list filter)', () => {
  const values = [
    { id: 'v1', object_type_attribute_id: 'a1', attribute_name: 'Hostname' },
    { id: 'v2', object_type_attribute_id: 'a2', attribute_name: 'IP Address' },
  ]
  const refGroups = [
    ['Related Server', [{ id: 'r1', to_object_name: 'web-01' }]],
    ['Owner', [{ id: 'r2', to_object_name: 'alice' }]],
  ]

  it('returns values and refGroups unchanged when q is empty', () => {
    const out = filterAttributeAndRefRows({ values, refGroups }, '')
    assert.equal(out.values.length, 2)
    assert.equal(out.refGroups.length, 2)
  })

  it('filters values by attribute_name', () => {
    const out = filterAttributeAndRefRows({ values, refGroups }, 'host')
    assert.deepEqual(out.values, [values[0]])
  })

  it('filters refGroups by the reference attribute name', () => {
    const out = filterAttributeAndRefRows({ values, refGroups }, 'owner')
    assert.deepEqual(out.refGroups, [refGroups[1]])
    // The same query filters the attribute-value rows too — no value row is
    // named 'owner', so only the matching reference group remains visible.
    assert.equal(out.values.length, 0)
  })

  it('is case-insensitive and trims whitespace', () => {
    const out = filterAttributeAndRefRows({ values, refGroups }, '  IP  ')
    assert.deepEqual(out.values, [values[1]])
  })

  it('returns empty collections when nothing matches', () => {
    const out = filterAttributeAndRefRows({ values, refGroups }, 'zzz-no-match-252')
    assert.equal(out.values.length, 0)
    assert.equal(out.refGroups.length, 0)
  })
})

describe('filterInboundGroups (ObjectDetailPanel Referenced By, manifest surface 09 — local loaded-list filter)', () => {
  const groups = [
    {
      typeId: 't1',
      typeName: 'Server',
      refs: [
        { id: 'r1', from_object_name: 'web-01', from_attribute_name: 'Runs On' },
        { id: 'r2', from_object_name: 'db-01', from_attribute_name: 'Runs On' },
      ],
    },
    {
      typeId: 't2',
      typeName: 'Network Device',
      refs: [{ id: 'r3', from_object_name: 'switch-01', from_attribute_name: 'Located In' }],
    },
  ]

  it('returns all groups unchanged when q is empty', () => {
    assert.equal(filterInboundGroups(groups, '').length, 2)
  })

  it('filters by object-type name', () => {
    const out = filterInboundGroups(groups, 'network')
    assert.equal(out.length, 1)
    assert.equal(out[0].typeName, 'Network Device')
  })

  it('filters by the source object name', () => {
    const out = filterInboundGroups(groups, 'db-01')
    assert.equal(out.length, 1)
    assert.equal(out[0].refs.length, 1)
    assert.equal(out[0].refs[0].from_object_name, 'db-01')
  })

  it('filters by the via attribute name', () => {
    const out = filterInboundGroups(groups, 'located')
    assert.equal(out.length, 1)
    assert.equal(out[0].refs[0].from_attribute_name, 'Located In')
  })

  it('drops a group when none of its refs match', () => {
    const out = filterInboundGroups(groups, 'web')
    assert.equal(out.length, 1)
    assert.equal(out[0].typeName, 'Server')
    assert.equal(out[0].refs.length, 1)
  })

  it('returns an empty array when nothing matches', () => {
    assert.deepEqual(filterInboundGroups(groups, 'zzz-no-match-252'), [])
  })
})

describe('buildTypeChain (ObjectTypeBreadcrumb adapter — parent-chain resolution)', () => {
  it('returns an empty chain when the object type is not loaded', () => {
    assert.deepEqual(buildTypeChain(null, []), [])
  })

  it('resolves a root type to a single-item chain', () => {
    const root = { id: 't1', name: 'Root', parent_id: null }
    assert.deepEqual(buildTypeChain(root, []), [root])
  })

  it('walks parent_id links through the loaded sibling list', () => {
    const child = { id: 't3', name: 'Child', parent_id: 't2' }
    const parent = { id: 't2', name: 'Parent', parent_id: 't1' }
    const grand = { id: 't1', name: 'Grand', parent_id: null }
    const chain = buildTypeChain(child, [grand, parent])
    assert.deepEqual(chain, [grand, parent, child])
  })

  it('stops at an unresolvable parent instead of looping', () => {
    const child = { id: 't2', name: 'Child', parent_id: 'missing' }
    assert.deepEqual(buildTypeChain(child, []), [child])
  })
})

describe('buildObjectTypeBreadcrumbItems (D-03 explicit items from loaded display data)', () => {
  const source = { id: 's1', name: 'Jira Prod' }
  const schema = { id: 'sc1', name: 'ITSM' }
  const typeChain = [
    { id: 't1', name: 'Root' },
    { id: 't2', name: 'Server' },
  ]

  it('builds Source → Schema → Type items with deterministic hrefs', () => {
    const items = buildObjectTypeBreadcrumbItems({ source, schema, typeChain })
    assert.equal(items.length, 4)
    assert.deepEqual(items[0], { label: 'Jira Prod', href: '/configuration/sources' })
    assert.deepEqual(items[1], { label: 'ITSM', href: '/configuration/schemas/sc1' })
    assert.deepEqual(items[2], { label: 'Root', href: '/browse/object-types/t1' })
  })

  it('renders the final type as the current item (no href) when no trailing label is given', () => {
    const items = buildObjectTypeBreadcrumbItems({ source, schema, typeChain })
    assert.deepEqual(items[3], { label: 'Server' })
  })

  it('appends the loaded object label as the current item when trailing is given', () => {
    const items = buildObjectTypeBreadcrumbItems({ source, schema, typeChain, trailing: 'web-01' })
    assert.equal(items.length, 5)
    assert.deepEqual(items[3], { label: 'Server', href: '/browse/object-types/t2' })
    assert.deepEqual(items[4], { label: 'web-01' })
  })

  it('never substitutes a raw identifier for a missing display name', () => {
    const items = buildObjectTypeBreadcrumbItems({ source: null, schema: null, typeChain, trailing: 'web-01' })
    const labels = items.map((i) => i.label)
    assert.ok(labels.every((l) => !l.startsWith('s1') && !l.startsWith('sc1') && !l.startsWith('t1') && !l.startsWith('t2')))
  })

  it('returns only the trailing label when no hierarchy data is loaded', () => {
    const items = buildObjectTypeBreadcrumbItems({ source: null, schema: null, typeChain: [], trailing: 'web-01' })
    assert.deepEqual(items, [{ label: 'web-01' }])
  })
})