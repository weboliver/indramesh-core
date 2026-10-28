import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { filterPermissionGroups, permissionOrigin } from './permission-matrix-search.js'

describe('filterPermissionGroups', () => {
  const groups = [
    {
      resource: 'groups',
      permissions: [
        { id: 'g1', action: 'create', name: 'Create groups', description: null },
        { id: 'g2', action: 'read', name: 'Read groups', description: 'View groups' },
      ],
    },
    {
      resource: 'roles',
      permissions: [
        { id: 'r1', action: 'manage', name: 'Manage roles', description: 'Assign roles' },
      ],
    },
  ]

  it('returns all groups when the query is empty', () => {
    assert.equal(filterPermissionGroups(groups, '').length, 2)
  })

  it('filters groups whose resource matches', () => {
    const out = filterPermissionGroups(groups, 'group')
    assert.equal(out.length, 1)
    assert.equal(out[0].resource, 'groups')
  })

  it('filters to the permissions whose action matches within a group', () => {
    const out = filterPermissionGroups(groups, 'manage')
    assert.equal(out.length, 1)
    assert.equal(out[0].resource, 'roles')
    assert.equal(out[0].permissions.length, 1)
  })

  it('drops a group when its matching permissions become empty', () => {
    const out = filterPermissionGroups(groups, 'create')
    assert.equal(out.length, 1)
    assert.equal(out[0].resource, 'groups')
    assert.equal(out[0].permissions[0].action, 'create')
  })

  it('trims and lowercases the query', () => {
    assert.equal(filterPermissionGroups(groups, '  GROUP  ').length, 1)
  })
})

describe('permissionOrigin (Phase 303, E1)', () => {
  it('reports Core for a permission with no plugin owner', () => {
    assert.deepEqual(permissionOrigin({ owner_plugin: null }), {
      isCore: true,
      label: 'Core',
    })
  })

  it('prefers the owner label and falls back to the stable plugin name', () => {
    assert.deepEqual(
      permissionOrigin({ owner_plugin: 'example', owner_label: 'Example Tools' }),
      { isCore: false, label: 'Example Tools' }
    )
    assert.deepEqual(
      permissionOrigin({ owner_plugin: 'example', owner_label: null }),
      { isCore: false, label: 'example' }
    )
  })
})

describe('filterPermissionGroups origin-aware search', () => {
  const pluginGroups = [
    {
      resource: 'example-tools',
      permissions: [
        {
          id: 'e1',
          action: 'read',
          name: 'example-tools:read',
          description: null,
          owner_plugin: 'example',
          owner_label: null,
        },
      ],
    },
    {
      resource: 'roles',
      permissions: [
        {
          id: 'r1',
          action: 'read',
          name: 'roles:read',
          description: null,
          owner_plugin: null,
          owner_state: 'core',
        },
      ],
    },
  ]

  it('matches the stable plugin name as the displayed origin', () => {
    const out = filterPermissionGroups(pluginGroups, 'example')
    assert.equal(out.length, 1)
    assert.equal(out[0].resource, 'example-tools')
  })

  it('matches the Core origin for core rows', () => {
    const out = filterPermissionGroups(pluginGroups, 'core')
    assert.equal(out.length, 1)
    assert.equal(out[0].resource, 'roles')
  })

  it('still matches resource, action and description', () => {
    assert.equal(filterPermissionGroups(pluginGroups, 'example-tools').length, 1)
    assert.equal(filterPermissionGroups(pluginGroups, 'read').length, 2)
  })
})
