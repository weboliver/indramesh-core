import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  ACTION_FAMILIES,
  CONNECTOR_ROLES,
  ITEM_BADGE_KINDS,
  createOwnerGenerations,
  frameContribution,
  frameSchemaWizardEntry,
  isPermitted,
  mergeTranslationDraft,
  normalizeBadgeMap,
  normalizeDraftProposals,
  normalizeNavBadge,
  parsePermission,
  pluginNamespace,
  selectVisibleEntries,
  sortByPluginName,
  sortContributions,
  splitItemBadges,
  translationPairKey,
  validateContributionEntry,
  validateTranslations,
} from './contribution-utils.js'

const fn = () => null

describe('contribution constants', () => {
  it('exposes the item badge kinds and connector roles the descriptor contracts allow', () => {
    assert.deepEqual(ITEM_BADGE_KINDS, ['object', 'comment', 'file', 'attribute'])
    assert.deepEqual(CONNECTOR_ROLES, ['source', 'target'])
  })
})

describe('parsePermission', () => {
  it('returns resource and action for one colon with two non-empty parts', () => {
    assert.deepEqual(parsePermission('tasks:read'), { resource: 'tasks', action: 'read' })
  })

  it('returns null for anything that is not exactly resource:action', () => {
    for (const value of ['tasks', 'tasks:read:extra', ':read', 'tasks:', '', null, 7]) {
      assert.equal(parsePermission(value), null)
    }
  })
})

describe('isPermitted', () => {
  it('allows an entry without a declared permission', () => {
    assert.equal(isPermitted({ id: 'x' }, () => false), true)
  })

  it('fails closed for a malformed permission', () => {
    assert.equal(isPermitted({ id: 'x', permission: 'broken' }, () => true), false)
  })

  it('delegates a valid permission to the can callback', () => {
    const seen = []
    const can = (resource, action) => {
      seen.push(`${resource}:${action}`)
      return true
    }
    assert.equal(isPermitted({ id: 'x', permission: 'tasks:read' }, can), true)
    assert.deepEqual(seen, ['tasks:read'])
  })
})

describe('sortByPluginName', () => {
  it('orders by plugin name base-insensitively and keeps same-plugin descriptor order', () => {
    const entries = [
      { pluginName: 'beta', id: 'b2' },
      { pluginName: 'Alpha', id: 'a1' },
      { pluginName: 'beta', id: 'b1' },
      { pluginName: 'alpha', id: 'a2' },
    ]

    assert.deepEqual(
      sortByPluginName(entries).map((entry) => entry.id),
      ['a1', 'a2', 'b2', 'b1']
    )
  })
})

describe('pluginNamespace', () => {
  it('derives the host-assigned namespace from the plugin name', () => {
    assert.equal(pluginNamespace('example'), 'plugin-example')
  })
})

describe('sortContributions (D-06 owner then stable id, locale-independent)', () => {
  it('orders by plugin name then stable id and ignores insertion order', () => {
    const entries = [
      { pluginName: 'beta', id: 'b2' },
      { pluginName: 'Alpha', id: 'a2' },
      { pluginName: 'beta', id: 'b1' },
      { pluginName: 'Alpha', id: 'a1' },
    ]

    assert.deepEqual(
      sortContributions(entries).map((entry) => `${entry.pluginName}:${entry.id}`),
      ['Alpha:a1', 'Alpha:a2', 'beta:b1', 'beta:b2']
    )
  })

  it('compares by codepoint, not the runtime default locale', () => {
    const entries = [
      { pluginName: 'alpha', id: 'x' },
      { pluginName: 'Alpha', id: 'x' },
    ]

    assert.deepEqual(
      sortContributions(entries).map((entry) => entry.pluginName),
      ['Alpha', 'alpha']
    )
  })
})

describe('frameContribution (D-06 host-stamped ownership and resolved label)', () => {
  it('resolves a labelKey through the host resolver and stamps the registry owner', () => {
    const entry = {
      pluginName: 'example',
      id: 'e1',
      label: 'Fallback',
      labelKey: 'wizard.entry.label',
    }
    const seen = []
    const framed = frameContribution(entry, (owner, key, fallback) => {
      seen.push([owner, key, fallback])
      return 'Resolved label'
    })

    assert.deepEqual(seen, [['example', 'wizard.entry.label', 'Fallback']])
    assert.equal(framed.displayLabel, 'Resolved label')
    assert.equal(framed.owner, 'example')
  })

  it('falls back to the plain label when no labelKey is present and never uses a plugin-supplied origin', () => {
    const framed = frameContribution(
      { pluginName: 'example', id: 'e1', label: 'Plain', owner: 'spoofed' },
      () => 'nope'
    )

    assert.equal(framed.displayLabel, 'Plain')
    assert.equal(framed.owner, 'example')
  })
})

describe('frameSchemaWizardEntry (owner-namespaced description, validated icon)', () => {
  it('resolves descriptionKey in the owner namespace with the descriptor fallback and keeps a known icon', () => {
    const seen = []
    const entry = {
      pluginName: 'ai-schema-wizard',
      id: 'generate-schema',
      label: 'Generate with AI',
      labelKey: 'entry.label',
      description: 'Fallback description',
      descriptionKey: 'entry.description',
      icon: 'sparkles',
    }
    const framed = frameSchemaWizardEntry(
      entry,
      (owner, key, fallback) => {
        seen.push([owner, key, fallback])
        return `resolved:${key}`
      },
      ['sparkles', 'server']
    )

    assert.deepEqual(seen, [
      ['ai-schema-wizard', 'entry.label', 'Generate with AI'],
      ['ai-schema-wizard', 'entry.description', 'Fallback description'],
    ])
    assert.equal(framed.displayLabel, 'resolved:entry.label')
    assert.equal(framed.displayDescription, 'resolved:entry.description')
    assert.equal(framed.icon, 'sparkles')
    assert.equal(framed.owner, 'ai-schema-wizard')
  })

  it('drops an unknown icon without rejecting the entry and yields a null description when absent', () => {
    const framed = frameSchemaWizardEntry(
      { pluginName: 'example', id: 'e', label: 'Plain', icon: 'not-a-real-icon' },
      () => 'ignored',
      ['sparkles']
    )

    assert.equal(framed.icon, null)
    assert.equal(framed.displayDescription, null)
    assert.equal(framed.displayLabel, 'Plain')
  })

  it('treats a descriptor without description/icon as the legacy label-only shape', () => {
    const framed = frameSchemaWizardEntry({ pluginName: 'example', id: 'e', label: 'Label' }, () => 'x', [])
    assert.equal(framed.displayDescription, null)
    assert.equal(framed.icon, null)
    assert.equal(framed.displayLabel, 'Label')
  })
})

describe('normalizeNavBadge', () => {
  it('returns null for absent, zero, negative, fractional and non-number values', () => {
    for (const value of [undefined, null, 0, -1, 1.5, '7', Number.NaN]) {
      assert.equal(normalizeNavBadge(value), null)
    }
  })

  it('renders exact counts up to 99 and caps higher values at 99+', () => {
    assert.equal(normalizeNavBadge(1), '1')
    assert.equal(normalizeNavBadge(99), '99')
    assert.equal(normalizeNavBadge(100), '99+')
    assert.equal(normalizeNavBadge(12345), '99+')
  })
})

describe('validateContributionEntry', () => {
  it('accepts a well-formed taskForms entry and rejects each broken shape', () => {
    assert.equal(
      validateContributionEntry('taskForms', { id: 'f', tasktype: 'x.y', component: fn }),
      null
    )
    assert.ok(validateContributionEntry('taskForms', { id: 'f', component: fn }))
    assert.ok(validateContributionEntry('taskForms', { id: 'f', tasktype: '', component: fn }))
    assert.ok(validateContributionEntry('taskForms', { id: 'f', tasktype: 'x.y', component: 'nope' }))
  })

  it('accepts a well-formed connectorConfigEditors entry and validates roles', () => {
    const base = { id: 'c', connector_type: 'example', label: 'Example', component: fn }
    assert.equal(validateContributionEntry('connectorConfigEditors', base), null)
    assert.equal(
      validateContributionEntry('connectorConfigEditors', { ...base, roles: CONNECTOR_ROLES }),
      null
    )
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, connector_type: '' }))
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, label: '' }))
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, component: 'nope' }))
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, roles: [] }))
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, roles: ['row'] }))
  })

  it('accepts an optional afterCoreSave function and rejects a non-function', () => {
    const base = { id: 'c', connector_type: 'jira', label: 'Jira', component: fn }
    assert.equal(validateContributionEntry('connectorConfigEditors', base), null)
    assert.equal(
      validateContributionEntry('connectorConfigEditors', { ...base, afterCoreSave: fn }),
      null
    )
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, afterCoreSave: true }))
    assert.ok(validateContributionEntry('connectorConfigEditors', { ...base, afterCoreSave: 'nope' }))
  })

  it('requires id, label and component for every section family and validates labelKey/permission', () => {
    for (const family of [
      'objectDetailSections',
      'schemaSettingsSections',
      'objectTypeSettingsSections',
    ]) {
      const base = { id: 's', label: 'Section', component: fn }
      assert.equal(validateContributionEntry(family, base), null)
      assert.equal(validateContributionEntry(family, { ...base, labelKey: 'section.title' }), null)
      assert.equal(validateContributionEntry(family, { ...base, permission: 'tasks:read' }), null)
      assert.ok(validateContributionEntry(family, { label: 'Section', component: fn }))
      assert.ok(validateContributionEntry(family, { ...base, label: '' }))
      assert.ok(validateContributionEntry(family, { ...base, component: 'nope' }))
      assert.ok(validateContributionEntry(family, { ...base, labelKey: '' }))
      assert.ok(validateContributionEntry(family, { ...base, permission: 'broken' }))
    }
  })

  it('requires id, a resolve function and a kinds subset for itemBadges', () => {
    assert.equal(
      validateContributionEntry('itemBadges', { id: 'b', kinds: ['object', 'comment'], resolve: fn }),
      null
    )
    assert.ok(validateContributionEntry('itemBadges', { kinds: ['object'], resolve: fn }))
    assert.ok(validateContributionEntry('itemBadges', { id: 'b', kinds: [], resolve: fn }))
    assert.ok(
      validateContributionEntry('itemBadges', { id: 'b', kinds: ['object', 'row'], resolve: fn })
    )
    assert.ok(validateContributionEntry('itemBadges', { id: 'b', kinds: ['object'], resolve: 'nope' }))
  })

  it('accepts the v19 navItems shape and validates only the new optional fields', () => {
    assert.equal(validateContributionEntry('navItems', { id: 'n', label: 'Nav', path: '/x' }), null)
    assert.equal(validateContributionEntry('navItems', { id: 'n', badge: fn, badgeLabel: fn }), null)
    assert.equal(validateContributionEntry('navItems', { id: 'n', permission: 'tasks:read' }), null)
    assert.ok(validateContributionEntry('navItems', { id: 'n', badge: 'nope' }))
    assert.ok(validateContributionEntry('navItems', { id: 'n', badgeLabel: 'nope' }))
    assert.ok(validateContributionEntry('navItems', { id: 'n', permission: 'broken' }))
    assert.ok(validateContributionEntry('navItems', { id: 'n', labelKey: '' }))
  })

  it('exposes exactly the four shared action families (Carry-02)', () => {
    assert.deepEqual(ACTION_FAMILIES, [
      'schemaWizardEntries',
      'translationPanelActions',
      'translationAdminActions',
      'taskDetailActions',
    ])
  })

  it('requires id, label, component and a valid operation permission for every shared action family', () => {
    for (const family of ACTION_FAMILIES) {
      const base = { id: 'x', label: 'X', permission: 'example-tools:read', component: fn }
      assert.equal(validateContributionEntry(family, base), null)
      assert.equal(validateContributionEntry(family, { ...base, labelKey: 'a.b' }), null)
      assert.ok(validateContributionEntry(family, { label: 'X', permission: 'example-tools:read', component: fn }))
      assert.ok(validateContributionEntry(family, { ...base, label: '' }))
      assert.ok(validateContributionEntry(family, { ...base, component: 'nope' }))
      assert.ok(validateContributionEntry(family, { ...base, labelKey: '' }))
      // Missing or malformed permission is invalid — never an implicit grant.
      assert.ok(validateContributionEntry(family, { id: 'x', label: 'X', component: fn }))
      assert.ok(validateContributionEntry(family, { ...base, permission: 'broken' }))
    }
  })

  it('accepts the optional schemaWizardEntries availability/description/icon fields only in that family', () => {
    const base = { id: 'w', label: 'Wizard', permission: 'ai-schema-wizard:generate', component: fn }
    assert.equal(validateContributionEntry('schemaWizardEntries', base), null)
    assert.equal(validateContributionEntry('schemaWizardEntries', { ...base, isAvailable: fn }), null)
    assert.equal(validateContributionEntry('schemaWizardEntries', { ...base, description: 'Desc' }), null)
    assert.equal(validateContributionEntry('schemaWizardEntries', { ...base, descriptionKey: 'entry.desc' }), null)
    assert.equal(validateContributionEntry('schemaWizardEntries', { ...base, icon: 'sparkles' }), null)

    assert.ok(validateContributionEntry('schemaWizardEntries', { ...base, isAvailable: true }))
    assert.ok(validateContributionEntry('schemaWizardEntries', { ...base, description: 7 }))
    assert.ok(validateContributionEntry('schemaWizardEntries', { ...base, descriptionKey: '' }))
    assert.ok(validateContributionEntry('schemaWizardEntries', { ...base, icon: 7 }))

    // The extension is scoped: another action family keeps its existing contract
    // and never validates these fields.
    const other = { ...base, isAvailable: true, description: 7, icon: 7 }
    assert.equal(validateContributionEntry('translationPanelActions', other), null)
  })

  it('validates the optional isPresent predicate on the two translation action families and schemaSettingsSections', () => {
    const families = ['translationPanelActions', 'translationAdminActions']
    for (const family of families) {
      const base = { id: 'x', label: 'X', permission: 'ai-translation:data', component: fn }
      assert.equal(validateContributionEntry(family, base), null)
      assert.equal(validateContributionEntry(family, { ...base, isPresent: fn }), null)
      assert.ok(validateContributionEntry(family, { ...base, isPresent: true }))
      assert.ok(validateContributionEntry(family, { ...base, isPresent: 'nope' }))
    }

    const section = { id: 's', label: 'Section', component: fn }
    assert.equal(validateContributionEntry('schemaSettingsSections', section), null)
    assert.equal(validateContributionEntry('schemaSettingsSections', { ...section, isPresent: fn }), null)
    assert.ok(validateContributionEntry('schemaSettingsSections', { ...section, isPresent: 7 }))

    // The extension is scoped: another family never validates isPresent.
    assert.equal(validateContributionEntry('objectDetailSections', { ...section, isPresent: true }), null)
    assert.equal(validateContributionEntry('taskDetailActions', { id: 'x', label: 'X', permission: 'a:b', component: fn, isPresent: true }), null)
  })
})

describe('selectVisibleEntries (D-16 fail-closed presence)', () => {
  const entry = (id, extra = {}) => ({ pluginName: 'ai-translation', id, ...extra })

  it('keeps an entry without a predicate and only a resolved-true predicate', () => {
    const entries = [entry('plain'), entry('gated', { isPresent: fn }), entry('hidden', { isPresent: fn })]
    const availability = {
      'ai-translation\u0000gated': true,
      'ai-translation\u0000hidden': false,
    }

    assert.deepEqual(
      selectVisibleEntries(entries, availability).map((item) => item.id),
      ['plain', 'gated']
    )
  })

  it('fails closed for an unresolved predicate and never returns an entry with no availability', () => {
    const entries = [entry('gated', { isPresent: fn })]
    assert.deepEqual(selectVisibleEntries(entries, {}), [])
    assert.deepEqual(selectVisibleEntries(null, {}), [])
  })
})

// CLEAN-03 absence (D-06): with a first-party plugin disabled, its gated
// contributions must be hidden — never a dead nav item, page frame or section.
// The host resolves availability only for owners in the runtime list, so an
// inactive owner has no resolution at all and fails closed.
describe('first-party absence fails closed (CLEAN-03/D-06)', () => {
  const FIRST_PARTY_OWNERS = [
    'graphql-studio',
    'log-explorer',
    'ai-schema-wizard',
    'ai-translation',
  ]
  const gatedEntry = (owner, id) => ({ pluginName: owner, id, isPresent: fn })

  it('hides every gated entry of all four first-party plugins when none resolves present', () => {
    const entries = FIRST_PARTY_OWNERS.map((owner) => gatedEntry(owner, 'section'))

    assert.deepEqual(selectVisibleEntries(entries, {}), [])
  })

  it('keeps only the resolving owner and never a sibling when one plugin is present', () => {
    const entries = FIRST_PARTY_OWNERS.map((owner) => gatedEntry(owner, 'section'))
    const availability = { 'log-explorer\u0000section': true }

    assert.deepEqual(
      selectVisibleEntries(entries, availability).map((item) => item.pluginName),
      ['log-explorer']
    )
  })

  it('fails closed for a first-party entry with a malformed permission (no dead button)', () => {
    const can = () => true

    assert.equal(
      isPermitted({ pluginName: 'graphql-studio', permission: 'graphql-studio' }, can),
      false
    )
    assert.equal(
      isPermitted({ pluginName: 'ai-schema-wizard', permission: 'ai-schema-wizard:read' }, can),
      true
    )
  })
})

describe('validateTranslations', () => {
  it('keeps valid language bundles', () => {
    const { bundles, warnings } = validateTranslations({
      en: { hello: 'Hello' },
      de: { hallo: 'Hallo' },
    })

    assert.deepEqual(bundles, { en: { hello: 'Hello' }, de: { hallo: 'Hallo' } })
    assert.deepEqual(warnings, [])
  })

  it('drops a non-object language, a non-string value and a colon key with one warning each', () => {
    const { bundles, warnings } = validateTranslations({
      en: { greeting: 'Hi', 'bad:key': 'x', notString: 5 },
      de: 'not-an-object',
      fr: { ok: 'Oui' },
    })

    assert.deepEqual(bundles, { en: { greeting: 'Hi' }, fr: { ok: 'Oui' } })
    assert.equal(warnings.length, 3)
  })

  it('yields no bundle and no warning when translations are absent', () => {
    assert.deepEqual(validateTranslations(undefined), { bundles: {}, warnings: [] })
    assert.deepEqual(validateTranslations(null), { bundles: {}, warnings: [] })
  })
})

describe('normalizeBadgeMap', () => {
  it('reads a Map and drops unrequested ids and label-less badges', () => {
    const result = normalizeBadgeMap(
      new Map([
        ['id1', [{ label: 'A' }, { title: 'no label' }]],
        ['id2', [{ label: 'B' }]],
      ]),
      ['id1']
    )

    assert.equal(result.ok, true)
    assert.deepEqual([...result.byId.keys()], ['id1'])
    assert.deepEqual(result.byId.get('id1').map((badge) => badge.label), ['A'])
  })

  it('reads a plain object result the same way', () => {
    const result = normalizeBadgeMap({ id1: [{ label: 'A' }] }, ['id1', 'id2'])

    assert.equal(result.ok, true)
    assert.deepEqual([...result.byId.keys()], ['id1'])
  })

  it('reports a malformed return shape as not ok', () => {
    assert.equal(normalizeBadgeMap([{ label: 'A' }], ['id1']).ok, false)
    assert.equal(normalizeBadgeMap('nope', ['id1']).ok, false)
  })

  it('copies only string badge metadata', () => {
    const result = normalizeBadgeMap(
      new Map([['id1', [{ label: 'A', icon: 7, title: 'T', labelKey: 'k', titleKey: 9 }]]]),
      ['id1']
    )

    assert.deepEqual(result.byId.get('id1')[0], { label: 'A', title: 'T', labelKey: 'k' })
  })
})

describe('splitItemBadges', () => {
  it('shows at most two badges and moves the rest into hidden', () => {
    assert.deepEqual(splitItemBadges(['a', 'b', 'c', 'd']), {
      visible: ['a', 'b'],
      hidden: ['c', 'd'],
    })
  })

  it('has no hidden badges for two or fewer', () => {
    assert.deepEqual(splitItemBadges(['a', 'b']), { visible: ['a', 'b'], hidden: [] })
    assert.deepEqual(splitItemBadges([]), { visible: [], hidden: [] })
  })
})

describe('createOwnerGenerations (D-08/D-14 generation guard)', () => {
  it('drops a completion whose owner generation advanced after disable', () => {
    const generations = createOwnerGenerations()
    const token = generations.begin('alpha')
    assert.equal(generations.isCurrent('alpha', token), true)

    generations.invalidate('alpha')
    assert.equal(generations.isCurrent('alpha', token), false)
  })

  it('keeps independent generations per owner', () => {
    const generations = createOwnerGenerations()
    const alpha = generations.begin('alpha')
    const beta = generations.begin('beta')

    generations.invalidate('alpha')

    assert.equal(generations.isCurrent('alpha', alpha), false)
    assert.equal(generations.isCurrent('beta', beta), true)
  })
})

describe('mergeTranslationDraft (D-07 pure draft handoff)', () => {
  it('fills only empty, untouched pairs against the latest Core state', () => {
    const result = mergeTranslationDraft(
      [
        { field: 'name', language: 'de', value: 'Name DE' },
        { field: 'name', language: 'fr', value: 'Name FR' },
        { field: 'description', language: 'de', value: 'Beschreibung' },
      ],
      {
        latest: {
          name: { de: 'Vorhanden' },
          description: { de: '   ' },
        },
      }
    )

    assert.deepEqual(result.applied, [
      { field: 'name', language: 'fr', value: 'Name FR' },
      { field: 'description', language: 'de', value: 'Beschreibung' },
    ])
    assert.deepEqual(result.skipped, [{ field: 'name', language: 'de', reason: 'existing' }])
    assert.deepEqual(result.rejected, [])
  })

  it('skips a touched pair, including one typed then cleared', () => {
    const touched = new Set([translationPairKey('name', 'de')])
    const result = mergeTranslationDraft(
      [{ field: 'name', language: 'de', value: 'Neu' }],
      { latest: { name: { de: '' } }, touched }
    )

    assert.deepEqual(result.applied, [])
    assert.deepEqual(result.skipped, [{ field: 'name', language: 'de', reason: 'touched' }])
  })

  it('rejects unknown field/language pairs against the allowed set', () => {
    const allowed = new Set([translationPairKey('name', 'de')])
    const result = mergeTranslationDraft(
      [
        { field: 'name', language: 'de', value: 'Name' },
        { field: 'name', language: 'fr', value: 'Nom' },
        { field: 'unknown', language: 'de', value: 'X' },
      ],
      { latest: {}, allowed }
    )

    assert.deepEqual(result.applied, [{ field: 'name', language: 'de', value: 'Name' }])
    assert.deepEqual(
      result.rejected.map((entry) => `${entry.field}:${entry.language}:${entry.reason}`),
      ['name:fr:not_allowed', 'unknown:de:not_allowed']
    )
  })

  it('rejects a duplicate pair that disagrees and a malformed proposal', () => {
    const result = mergeTranslationDraft(
      [
        { field: 'name', language: 'de', value: 'A' },
        { field: 'name', language: 'de', value: 'B' },
        { field: 'description', language: 'de', value: 7 },
      ],
      { latest: {} }
    )

    assert.deepEqual(result.applied, [])
    assert.deepEqual(result.rejected, [
      { field: 'name', language: 'de', reason: 'duplicate_conflict' },
      { field: 'description', language: 'de', reason: 'invalid_proposal' },
    ])
  })

  it('returns empty results for a non-array input', () => {
    assert.deepEqual(mergeTranslationDraft(null), { applied: [], skipped: [], rejected: [] })
  })
})

describe('normalizeDraftProposals (T-307-15 plain-JSON contributor delivery)', () => {
  it('keeps field/language/value and an opaque string or number reference', () => {
    const { accepted, rejected } = normalizeDraftProposals([
      { field: 'name', language: 'de', value: 'Name', reference: 'ref-1' },
      { field: 'name', language: 'fr', value: 'Nom', reference: 42 },
      { field: 'name', language: 'es', value: 'Nombre' },
    ])

    assert.deepEqual(accepted, [
      { field: 'name', language: 'de', value: 'Name', reference: 'ref-1' },
      { field: 'name', language: 'fr', value: 'Nom', reference: 42 },
      { field: 'name', language: 'es', value: 'Nombre' },
    ])
    assert.deepEqual(rejected, [])
  })

  it('rejects malformed pairs and a non-opaque reference (function, object, empty string)', () => {
    const { accepted, rejected } = normalizeDraftProposals([
      { field: '', language: 'de', value: 'X' },
      { field: 'name', language: 'de', value: 7 },
      { field: 'name', language: 'de', value: 'X', reference: () => null },
      { field: 'name', language: 'de', value: 'X', reference: { url: 'http://x' } },
      { field: 'name', language: 'de', value: 'X', reference: '' },
      'not-an-object',
    ])

    assert.deepEqual(accepted, [])
    assert.equal(rejected.length, 6)
    assert.deepEqual(rejected.map((entry) => entry.reason), [
      'invalid_proposal',
      'invalid_proposal',
      'invalid_reference',
      'invalid_reference',
      'invalid_reference',
      'invalid_proposal',
    ])
  })

  it('returns empty results for a non-array input', () => {
    assert.deepEqual(normalizeDraftProposals(null), { accepted: [], rejected: [] })
  })
})