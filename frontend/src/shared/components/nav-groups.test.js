import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { mergePluginNavGroups, slugifyGroupName } from './nav-groups.js'

const BASE_GROUPS = [
  { id: 'tools', canonicalName: 'Tools', label: 'Tools' },
  { id: 'config', canonicalName: 'Configuration', label: 'Konfiguration' },
  { id: 'tasks', canonicalName: 'Integration', label: 'Integration' },
  { id: 'admin', canonicalName: 'Administration', label: 'Verwaltung' },
]

function item(overrides) {
  return { id: 'item-1', pluginName: 'demo', path: '/plugins/demo/page', ...overrides }
}

describe('slugifyGroupName', () => {
  it('lowercases and replaces non-alphanumeric characters with dashes', () => {
    assert.equal(slugifyGroupName('Example'), 'example')
    assert.equal(slugifyGroupName('  My Group  '), 'my-group')
    assert.equal(slugifyGroupName('Demo_Plugin'), 'demo-plugin')
  })

  it('returns an empty string for nullish input', () => {
    assert.equal(slugifyGroupName(undefined), '')
    assert.equal(slugifyGroupName(null), '')
  })
})

describe('mergePluginNavGroups', () => {
  it('matches a plugin group against canonicalName, never the translated label', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'admin-item', group: 'Administration' }),
    ])

    assert.equal(groups.length, BASE_GROUPS.length)
    assert.deepEqual(groups.map((group) => group.id), ['tools', 'config', 'tasks', 'admin'])
    assert.deepEqual(itemsByGroupId.admin, [item({ id: 'admin-item', group: 'Administration' })])
  })

  it('matches case-insensitively and trims whitespace', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'tools-item', group: '  tools  ' }),
    ])

    assert.equal(groups.length, BASE_GROUPS.length)
    assert.deepEqual(itemsByGroupId.tools.map((entry) => entry.id), ['tools-item'])
  })

  it('creates a post-static root group with the slug testid for an unmatched group', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'example-item', group: 'Example' }),
    ])

    assert.equal(groups.length, BASE_GROUPS.length + 1)
    const appended = groups[groups.length - 1]
    assert.equal(appended.id, 'plugin-group-example')
    assert.equal(appended.label, 'Example')
    assert.equal(appended.testId, 'plugin-nav-group-example')
    assert.deepEqual(itemsByGroupId['plugin-group-example'].map((entry) => entry.id), ['example-item'])
  })

  it('falls back to the plugin name when no group is declared', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'plain-item', pluginName: 'Demo Plugin', group: undefined }),
    ])

    const appended = groups[groups.length - 1]
    assert.equal(appended.id, 'plugin-group-demo-plugin')
    assert.equal(appended.label, 'Demo Plugin')
    assert.equal(appended.testId, 'plugin-nav-group-demo-plugin')
    assert.deepEqual(itemsByGroupId['plugin-group-demo-plugin'].map((entry) => entry.id), ['plain-item'])
  })

  it('sorts plugin-only groups alphabetically after all static groups', () => {
    const { groups } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'zeta-item', pluginName: 'alpha-plugin', group: 'Zeta' }),
      item({ id: 'alpha-item', pluginName: 'zeta-plugin', group: 'alpha' }),
    ])

    assert.deepEqual(
      groups.map((group) => group.id),
      ['tools', 'config', 'tasks', 'admin', 'plugin-group-alpha', 'plugin-group-zeta']
    )
  })

  it('merges two plugin groups with the same match key into one dropdown', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'first-item', pluginName: 'plugin-a', group: 'Beta' }),
      item({ id: 'second-item', pluginName: 'plugin-b', group: 'beta' }),
    ])

    const pluginGroups = groups.filter((group) => group.id.startsWith('plugin-group-'))
    assert.equal(pluginGroups.length, 1)
    assert.deepEqual(
      itemsByGroupId['plugin-group-beta'].map((entry) => entry.id),
      ['first-item', 'second-item']
    )
  })

  it('keeps distinct labels that slugify identically in separate, uniquely identified groups (WR-02)', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'first-item', pluginName: 'Demo Plugin', group: undefined }),
      item({ id: 'second-item', pluginName: 'demo-plugin', group: undefined }),
    ])

    const pluginGroups = groups.filter((group) => group.id.startsWith('plugin-group-demo-plugin'))
    assert.deepEqual(
      pluginGroups.map((group) => group.id),
      ['plugin-group-demo-plugin', 'plugin-group-demo-plugin-2']
    )
    assert.deepEqual(
      pluginGroups.map((group) => group.label),
      ['Demo Plugin', 'demo-plugin']
    )
    assert.deepEqual(
      pluginGroups.map((group) => group.testId),
      ['plugin-nav-group-demo-plugin', 'plugin-nav-group-demo-plugin-2']
    )
    assert.deepEqual(itemsByGroupId['plugin-group-demo-plugin'].map((entry) => entry.id), ['first-item'])
    assert.deepEqual(itemsByGroupId['plugin-group-demo-plugin-2'].map((entry) => entry.id), ['second-item'])
  })

  it('preserves manifest order of items inside a matched static group', () => {
    const { itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [
      item({ id: 'first-item', pluginName: 'plugin-a', group: 'Tools' }),
      item({ id: 'second-item', pluginName: 'plugin-b', group: 'Tools' }),
    ])

    assert.deepEqual(itemsByGroupId.tools.map((entry) => entry.id), ['first-item', 'second-item'])
  })

  it('leaves the static group list untouched when no plugin contributes', () => {
    const { groups, itemsByGroupId } = mergePluginNavGroups(BASE_GROUPS, [])

    assert.deepEqual(groups, BASE_GROUPS)
    assert.deepEqual(itemsByGroupId, {})
  })
})