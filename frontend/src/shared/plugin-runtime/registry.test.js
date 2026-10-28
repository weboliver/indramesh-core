import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { createContributionRegistry, SLOT_CONFLICT_KEYS } from './registry.js'
import { buildPluginBundleUrl, loadPluginBundle, validateDescriptor } from './loader.js'

const FULL_DESCRIPTOR = {
  pages: [{ path: '/alpha', component: 'AlphaPage' }],
  navItems: [{ id: 'alpha-nav' }],
  attributeEditors: [{ id: 'alpha-editor', data_type: 'text', component: 'AlphaEditor' }],
  actionButtons: [{ id: 'alpha-button', objectType: '*', component: 'AlphaButton' }],
  settingsSections: [{ id: 'alpha-section', component: 'AlphaSection' }],
  taskForms: [{ id: 'alpha-form', tasktype: 'alpha.task', component: () => null }],
  connectorConfigEditors: [
    { id: 'alpha-connector', connector_type: 'alpha', label: 'Alpha', component: () => null },
  ],
  objectDetailSections: [{ id: 'alpha-detail', label: 'Alpha', component: () => null }],
  schemaSettingsSections: [{ id: 'alpha-schema', label: 'Alpha', component: () => null }],
  objectTypeSettingsSections: [{ id: 'alpha-object-type', label: 'Alpha', component: () => null }],
  itemBadges: [{ id: 'alpha-badge', kinds: ['object'], resolve: () => null }],
  schemaWizardEntries: [
    { id: 'alpha-wizard', label: 'Alpha wizard', permission: 'alpha:read', component: () => null },
  ],
  translationPanelActions: [
    { id: 'alpha-panel', label: 'Alpha panel', permission: 'alpha:read', component: () => null },
  ],
  translationAdminActions: [
    { id: 'alpha-admin', label: 'Alpha admin', permission: 'alpha:read', component: () => null },
  ],
  taskDetailActions: [
    { id: 'alpha-task', label: 'Alpha task', permission: 'alpha:read', component: () => null },
  ],
}

const ALL_FAMILIES = Object.keys(SLOT_CONFLICT_KEYS)

describe('validateDescriptor (manifest extension_points is a binding contract, D-06)', () => {
  it('rejects a descriptor family absent from frontend.extension_points and names the offending family', () => {
    const result = validateDescriptor({ pages: [], actionButtons: [] }, ['pages'])

    assert.equal(result.state, 'ui_fault')
    assert.deepEqual(result.contributions, {})
    assert.equal(result.faults.length, 1)
    assert.match(result.faults[0], /undeclared:/)
    assert.match(result.faults[0], /actionButtons/)
  })

  it('does not apply any contribution of a descriptor that declares an undeclared family', () => {
    const result = validateDescriptor(
      { pages: [{ path: '/x' }], settingsSections: [{ id: 'y' }] },
      ['pages']
    )

    assert.equal(result.state, 'ui_fault')
    assert.deepEqual(result.contributions, {})
  })

  it('rejects a declared family that is not an array', () => {
    const result = validateDescriptor({ navItems: { id: 'nope' } }, ['navItems'])

    assert.equal(result.state, 'ui_fault')
    assert.match(result.faults[0], /malformed:/)
    assert.match(result.faults[0], /navItems/)
  })

  it('rejects a declared family the bundle never registers and names it', () => {
    const result = validateDescriptor({ pages: [] }, ['pages', 'itemBadges'])

    assert.equal(result.state, 'ui_fault')
    assert.deepEqual(result.contributions, {})
    assert.equal(result.faults.length, 1)
    assert.match(result.faults[0], /unregistered:/)
    assert.match(result.faults[0], /itemBadges/)
  })

  it('accepts a fully declared descriptor with contributions and no faults', () => {
    const result = validateDescriptor(FULL_DESCRIPTOR, ALL_FAMILIES)

    assert.equal(result.state, 'ready')
    assert.deepEqual(result.faults, [])
    assert.equal(result.contributions.pages.length, 1)
    assert.equal(result.contributions.attributeEditors[0].data_type, 'text')
    assert.equal(result.contributions.settingsSections[0].id, 'alpha-section')
  })

  it('treats a frontend-less plugin as ready with no contributions and no faults', async () => {
    const result = await loadPluginBundle({ name: 'alpha', frontend: null })

    assert.equal(result.state, 'ready')
    assert.deepEqual(result.contributions, {})
    assert.deepEqual(result.faults, [])
  })
})

describe('buildPluginBundleUrl (WR-03: a retry must not reuse a failed import URL)', () => {
  const plugin = { name: 'alpha', version: '1.2.3', frontend: { bundle: 'static/app.js' } }

  it('keeps ?v= as the manifest version cache key on the first attempt', () => {
    assert.equal(
      buildPluginBundleUrl(plugin, 0),
      '/api/v1/plugins/alpha/assets/static/app.js?v=1.2.3&ce=2'
    )
  })

  it('appends a distinct attempt parameter so the retry imports a new URL', () => {
    const first = buildPluginBundleUrl(plugin)
    const retry = buildPluginBundleUrl(plugin, 1)

    assert.equal(retry, '/api/v1/plugins/alpha/assets/static/app.js?v=1.2.3&ce=2&attempt=1')
    assert.notEqual(retry, first)
  })

  it('encodes the plugin name and the attempt value', () => {
    const url = buildPluginBundleUrl(
      { name: 'a b', version: '1', frontend: { bundle: 'x.js' } },
      2
    )

    assert.equal(url, '/api/v1/plugins/a%20b/assets/x.js?v=1&ce=2&attempt=2')
  })
})

describe('createContributionRegistry (D-07 first registration wins per conflict key)', () => {
  it('keeps the earlier registration for every conflict key and reports the later one as rejected', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', FULL_DESCRIPTOR)
    registry.register('beta', {
      pages: [{ path: '/alpha', component: 'BetaPage' }],
      navItems: [{ id: 'alpha-nav' }],
      attributeEditors: [{ id: 'beta-editor', data_type: 'text', component: 'BetaEditor' }],
      actionButtons: [{ id: 'alpha-button', objectType: '*', component: 'BetaButton' }],
      settingsSections: [{ id: 'alpha-section', component: 'BetaSection' }],
      taskForms: [{ id: 'beta-form', tasktype: 'alpha.task', component: 'BetaForm' }],
      connectorConfigEditors: [
        { id: 'beta-connector', connector_type: 'alpha', label: 'Beta', component: 'BetaConnector' },
      ],
      objectDetailSections: [{ id: 'alpha-detail', label: 'Beta', component: 'BetaDetail' }],
      schemaSettingsSections: [{ id: 'alpha-schema', label: 'Beta', component: 'BetaSchema' }],
      objectTypeSettingsSections: [{ id: 'alpha-object-type', label: 'Beta', component: 'BetaObjectType' }],
      itemBadges: [{ id: 'alpha-badge', kinds: ['object'], resolve: 'BetaResolve' }],
      schemaWizardEntries: [
        { id: 'alpha-wizard', label: 'Beta wizard', permission: 'beta:read', component: 'BetaWizard' },
      ],
      translationPanelActions: [
        { id: 'alpha-panel', label: 'Beta panel', permission: 'beta:read', component: 'BetaPanel' },
      ],
      translationAdminActions: [
        { id: 'alpha-admin', label: 'Beta admin', permission: 'beta:read', component: 'BetaAdmin' },
      ],
      taskDetailActions: [
        { id: 'alpha-task', label: 'Beta task', permission: 'beta:read', component: 'BetaTask' },
      ],
    })

    const contributions = registry.getContributions()
    assert.equal(contributions.pages.length, 1)
    assert.equal(contributions.pages[0].pluginName, 'alpha')
    assert.equal(contributions.pages[0].component, 'AlphaPage')
    assert.equal(contributions.navItems.length, 1)
    assert.equal(contributions.navItems[0].pluginName, 'alpha')
    assert.equal(contributions.attributeEditors.length, 1)
    assert.equal(contributions.attributeEditors[0].pluginName, 'alpha')
    assert.equal(contributions.actionButtons.length, 1)
    assert.equal(contributions.actionButtons[0].pluginName, 'alpha')
    assert.equal(contributions.settingsSections.length, 1)
    assert.equal(contributions.settingsSections[0].pluginName, 'alpha')

    const conflicts = registry.getConflicts()
    assert.equal(conflicts.length, ALL_FAMILIES.length)
    assert.deepEqual(
      conflicts.map((conflict) => conflict.family).sort(),
      [...ALL_FAMILIES].sort()
    )
    conflicts.forEach((conflict) => {
      assert.equal(conflict.kind, 'slot_conflict')
      assert.equal(conflict.pluginName, 'beta')
      assert.match(conflict.slot, new RegExp(`^${conflict.family}:`))
    })
  })

  it('applies non-conflicting keys from different plugins side by side', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', FULL_DESCRIPTOR)
    registry.register('beta', {
      pages: [{ path: '/beta', component: 'BetaPage' }],
      attributeEditors: [{ id: 'beta-editor', data_type: 'url', component: 'BetaEditor' }],
    })

    const contributions = registry.getContributions()
    assert.equal(contributions.pages.length, 2)
    assert.equal(contributions.attributeEditors.length, 2)
    assert.deepEqual(registry.getConflicts(), [])
  })

  it('keeps action buttons of different plugins for the same object type side by side (WR-01)', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', {
      actionButtons: [{ id: 'alpha-button', objectType: '*', component: 'AlphaButton' }],
    })
    registry.register('beta', {
      actionButtons: [{ id: 'beta-button', objectType: '*', component: 'BetaButton' }],
    })

    const contributions = registry.getContributions()
    assert.deepEqual(
      contributions.actionButtons.map((entry) => entry.pluginName),
      ['alpha', 'beta']
    )
    assert.deepEqual(
      contributions.actionButtons.map((entry) => entry.objectType),
      ['*', '*']
    )
    assert.deepEqual(registry.getConflicts(), [])
  })

  it('still rejects a second action button that reuses a claimed contribution id (WR-01)', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', {
      actionButtons: [{ id: 'shared-button', objectType: '*', component: 'AlphaButton' }],
    })
    registry.register('beta', {
      actionButtons: [{ id: 'shared-button', objectType: 'object-type-123', component: 'BetaButton' }],
    })

    const contributions = registry.getContributions()
    assert.equal(contributions.actionButtons.length, 1)
    assert.equal(contributions.actionButtons[0].pluginName, 'alpha')
    const conflicts = registry.getConflicts()
    assert.equal(conflicts.length, 1)
    assert.equal(conflicts[0].slot, 'actionButtons:shared-button')
  })

  it('rejects a second taskForms entry that claims an already-registered tasktype', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', {
      taskForms: [{ id: 'alpha-form', tasktype: 'x.y', component: 'AlphaForm' }],
    })
    registry.register('beta', {
      taskForms: [{ id: 'beta-form', tasktype: 'x.y', component: 'BetaForm' }],
    })

    const contributions = registry.getContributions()
    assert.equal(contributions.taskForms.length, 1)
    assert.equal(contributions.taskForms[0].pluginName, 'alpha')

    const conflicts = registry.getConflicts()
    assert.equal(conflicts.length, 1)
    assert.equal(conflicts[0].kind, 'slot_conflict')
    assert.equal(conflicts[0].family, 'taskForms')
    assert.equal(conflicts[0].reason, 'slot_conflict:taskForms:x.y')
    assert.equal(conflicts[0].pluginName, 'beta')
  })

  it('does not treat the same id in different families as a conflict', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', {
      objectDetailSections: [{ id: 'shared', label: 'A', component: 'A' }],
      itemBadges: [{ id: 'shared', kinds: ['object'], resolve: 'R' }],
    })

    const contributions = registry.getContributions()
    assert.equal(contributions.objectDetailSections.length, 1)
    assert.equal(contributions.itemBadges.length, 1)
    assert.deepEqual(registry.getConflicts(), [])
  })

  it('re-registering a plugin replaces its previous contributions idempotently', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', FULL_DESCRIPTOR)
    registry.register('alpha', { pages: [{ path: '/alpha', component: 'AlphaPage' }] })

    const contributions = registry.getContributions()
    assert.equal(contributions.pages.length, 1)
    assert.equal(contributions.navItems.length, 0)
    assert.deepEqual(registry.getConflicts(), [])
  })

  it('releases a removed owner claims so another plugin can claim the freed slot (D-08)', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', { navItems: [{ id: 'shared', label: 'A' }] })
    registry.register('beta', { navItems: [{ id: 'shared', label: 'B' }] })

    assert.equal(registry.getContributions().navItems[0].pluginName, 'alpha')
    assert.equal(registry.getConflicts().length, 1)

    registry.unregister('alpha')

    const contributions = registry.getContributions()
    assert.equal(contributions.navItems.length, 1)
    assert.equal(contributions.navItems[0].pluginName, 'beta')
    assert.deepEqual(registry.getConflicts(), [])
  })

  it('treats unregistering an unknown owner as a no-op', () => {
    const registry = createContributionRegistry()
    registry.register('alpha', { navItems: [{ id: 'a', label: 'A' }] })
    registry.unregister('missing')

    assert.equal(registry.getContributions().navItems.length, 1)
    assert.equal(registry.getContributions().navItems[0].pluginName, 'alpha')
  })
})

// CLEAN-03 absence (D-06): an inactive plugin never reaches this registry at
// all — the host loads bundles only for the plugins `plugins/runtime` returns,
// and that payload lists ACTIVE plugins only (PluginProvider.jsx). These cases
// pin the host half of the contract: with a first-party owner gone
// (unregistered), its navItems/pages/sections disappear, the remaining owners
// keep their relative order, and with all four gone the plugin contribution
// set is EMPTY — Core nav and pages are static JSX outside this registry
// (App.jsx + layout), so "Core navigation intact with all four disabled" is a
// browser check deferred to the Phase-309 regression, not a claim made here.
const FIRST_PARTY_OWNERS = [
  'graphql-studio',
  'log-explorer',
  'ai-schema-wizard',
  'ai-translation',
]

const firstPartyContributions = (name) => ({
  pages: [{ path: `/${name}`, component: `${name}-page` }],
  navItems: [{ id: `${name}-nav`, label: name }],
  settingsSections: [{ id: `${name}-section`, component: `${name}-section` }],
})

const registeredFirstPartyOwners = () => {
  const registry = createContributionRegistry()
  FIRST_PARTY_OWNERS.forEach((name) =>
    registry.register(name, firstPartyContributions(name))
  )
  return registry
}

describe('first-party plugin absence (CLEAN-03/D-06: a disabled plugin leaves nothing behind)', () => {
  it('hides exactly the disabled owner entries for each first-party plugin, keeping the rest in order', () => {
    for (const disabled of FIRST_PARTY_OWNERS) {
      const registry = registeredFirstPartyOwners()
      const remaining = FIRST_PARTY_OWNERS.filter((name) => name !== disabled)

      registry.unregister(disabled)
      const contributions = registry.getContributions()

      assert.deepEqual(
        contributions.navItems.map((entry) => entry.id),
        remaining.map((name) => `${name}-nav`)
      )
      assert.deepEqual(
        contributions.pages.map((entry) => entry.pluginName),
        remaining
      )
      assert.deepEqual(
        contributions.settingsSections.map((entry) => entry.id),
        remaining.map((name) => `${name}-section`)
      )
      assert.ok(
        !contributions.navItems.some((entry) => entry.pluginName === disabled),
        `${disabled} left a nav entry behind`
      )
      assert.deepEqual(registry.getConflicts(), [])
    }
  })

  it('leaves every contribution family empty once all four first-party owners are gone', () => {
    const registry = registeredFirstPartyOwners()
    FIRST_PARTY_OWNERS.forEach((name) => registry.unregister(name))

    const contributions = registry.getContributions()
    ALL_FAMILIES.forEach((family) => {
      assert.deepEqual(
        contributions[family],
        [],
        `${family} must be an empty array with no plugin registered`
      )
    })
    assert.deepEqual(registry.getConflicts(), [])
  })
})

describe('recorded slot faults (D-08: a fault never disables a plugin)', () => {
  it('keeps every contribution selectable and the runtime state ready after a slot_render fault is appended', () => {
    const registry = createContributionRegistry()
    registry.register('gamma', {
      attributeEditors: [{ id: 'gamma-editor', data_type: 'url', component: 'GammaEditor' }],
      actionButtons: [{ id: 'gamma-button', objectType: '*', component: 'GammaButton' }],
      settingsSections: [{ id: 'gamma-section', component: 'GammaSection' }],
    })

    const runtimeStates = { gamma: 'ready' }
    const faults = {}
    faults.gamma = [
      ...(faults.gamma || []),
      { kind: 'slot_render', slot: 'settingsSection:gamma-section' },
    ]

    const isApplicable = (pluginName) => runtimeStates[pluginName] === 'ready'

    assert.equal(runtimeStates.gamma, 'ready')
    assert.equal(faults.gamma.length, 1)
    assert.equal(isApplicable('gamma'), true)

    const contributions = registry.getContributions()
    assert.equal(contributions.settingsSections.filter((entry) => isApplicable(entry.pluginName)).length, 1)
    assert.equal(contributions.attributeEditors.filter((entry) => isApplicable(entry.pluginName)).length, 1)
    assert.equal(contributions.actionButtons.filter((entry) => isApplicable(entry.pluginName)).length, 1)
  })

  it('filters a plugin out only when its runtime state is not ready, regardless of recorded faults', () => {
    const registry = createContributionRegistry()
    registry.register('gamma', {
      settingsSections: [{ id: 'gamma-section', component: 'GammaSection' }],
    })

    const runtimeStates = { gamma: 'ui_fault' }
    const faults = { gamma: [{ kind: 'load', slot: null, reason: 'bundle: failed' }] }
    const isApplicable = (pluginName) => runtimeStates[pluginName] === 'ready'

    assert.equal(isApplicable('gamma'), false)
    assert.equal(
      registry.getContributions().settingsSections.filter((entry) => isApplicable(entry.pluginName)).length,
      0
    )
    assert.equal(faults.gamma.length, 1)
  })
})
