import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { filterSchemaAttributes, matchSchemaAttribute } from './browse-schema-filter.js'

describe('filterSchemaAttributes (ObjectTypeObjects schema tab, manifest surface 07 — local loaded-list filter)', () => {
  const attributes = [
    { id: 'a1', name: 'Hostname', description: 'FQDN of the server', sort: 10 },
    { id: 'a2', name: 'IP Address', description: 'IPv4 or IPv6 address', sort: 20 },
    { id: 'a3', name: 'Status', description: 'Operational status', sort: 30 },
  ]
  const labelFor = (attr) => ({ Hostname: 'Text', 'IP Address': 'IP Address', Status: 'Status' })[attr.name] || ''

  it('returns all attributes when q is empty', () => {
    assert.equal(filterSchemaAttributes(attributes, '', labelFor).length, 3)
  })

  it('matches by attribute name case-insensitively', () => {
    assert.deepEqual(filterSchemaAttributes(attributes, 'hostname', labelFor), [attributes[0]])
    assert.deepEqual(filterSchemaAttributes(attributes, 'IP', labelFor), [attributes[1]])
  })

  it('matches by the data-type display label', () => {
    assert.deepEqual(filterSchemaAttributes(attributes, 'text', labelFor), [attributes[0]])
  })

  it('matches by description', () => {
    assert.deepEqual(filterSchemaAttributes(attributes, 'operational', labelFor), [attributes[2]])
  })

  it('returns an empty array when nothing matches', () => {
    assert.deepEqual(filterSchemaAttributes(attributes, 'zzz-no-match-252', labelFor), [])
  })

  it('trims surrounding whitespace before matching', () => {
    assert.equal(filterSchemaAttributes(attributes, '  ip  ', labelFor).length, 1)
  })
})

describe('matchSchemaAttribute (index-preserving predicate for drag-reorder correctness)', () => {
  const attr = { id: 'a1', name: 'Hostname', description: 'FQDN of the server' }

  it('returns true for an empty query (no filtering)', () => {
    assert.equal(matchSchemaAttribute(attr, '', () => 'Text'), true)
  })

  it('matches the attribute name', () => {
    assert.equal(matchSchemaAttribute(attr, 'host', () => 'Text'), true)
  })

  it('matches the display label', () => {
    assert.equal(matchSchemaAttribute(attr, 'TEXT', () => 'Text'), true)
  })

  it('returns false when nothing matches', () => {
    assert.equal(matchSchemaAttribute(attr, 'zzz', () => 'Text'), false)
  })
})