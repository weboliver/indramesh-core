import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  SCOPE_FIELD_ORDER,
  buildParameterFields,
  buildPluginTaskPayload,
  initialParameterValues,
  missingScope,
  orderedScopeRequirements,
  sortCatalogEntries,
  taskFormKind,
  isCreatableInTaskModal,
  typeOptionText,
  validateTaskForm,
} from './task-catalog.js'

// A fake `t` that returns the key's default text keeps every assertion
// independent of the i18next runtime and of the (not-yet-cataloged) host keys.
const fakeT = (key, defaultValue) => defaultValue

describe('sortCatalogEntries', () => {
  it('sorts by label base-insensitively, core before plugin, then plugin name', () => {
    const entries = [
      { name: 'sync-pull', label: 'Sync Pull', source: 'core' },
      { name: 'export-core', label: 'Export', source: 'core' },
      { name: 'export-b', label: 'Export', source: 'plugin', plugin_name: 'b' },
      { name: 'export-a', label: 'Export', source: 'plugin', plugin_name: 'a' },
      { name: 'alpha', label: 'alpha', source: 'core' },
    ]

    const sorted = sortCatalogEntries(entries, 'en')

    assert.deepEqual(
      sorted.map((entry) => entry.name),
      ['alpha', 'export-core', 'export-a', 'export-b', 'sync-pull']
    )
    // The input array is not mutated.
    assert.deepEqual(
      entries.map((entry) => entry.name),
      ['sync-pull', 'export-core', 'export-b', 'export-a', 'alpha']
    )
  })
})

describe('typeOptionText', () => {
  const core = (over = {}) => ({ name: 'REINDEX', label: 'Reindex', source: 'core', dispatchable: true, ...over })
  const plugin = (over = {}) => ({
    name: 'p.x',
    label: 'Label',
    source: 'plugin',
    plugin_name: 'p',
    dispatchable: true,
    ...over,
  })

  it('shows core labels verbatim and plugin labels with the plugin origin', () => {
    assert.equal(typeOptionText(fakeT, core(), 'create'), 'Reindex')
    assert.equal(typeOptionText(fakeT, plugin(), 'create'), 'Label (Plugin: p)')
  })

  it('uses the complete unavailable key for a non-dispatchable entry', () => {
    assert.equal(typeOptionText(fakeT, core({ dispatchable: false }), 'create'), 'Reindex (unavailable)')
    assert.equal(typeOptionText(fakeT, plugin({ dispatchable: false }), 'create'), 'Label (Plugin: p, unavailable)')
  })

  it('uses ordinary labels in schedule and filter mode (non-schedulable entries never reach schedule mode)', () => {
    // Corrected D-13: the schedule select filters to schedulable=true before
    // formatting, and the approved UI contract adds no `_not_schedulable`
    // option variants — so schedule mode reuses the ordinary labels.
    assert.equal(typeOptionText(fakeT, core({ schedulable: true }), 'schedule'), 'Reindex')
    assert.equal(typeOptionText(fakeT, plugin({ schedulable: true }), 'schedule'), 'Label (Plugin: p)')
    assert.equal(typeOptionText(fakeT, core({ schedulable: true }), 'filter'), 'Reindex')
    assert.equal(typeOptionText(fakeT, plugin({ schedulable: true }), 'filter'), 'Label (Plugin: p)')
  })
})

describe('taskFormKind', () => {
  it('maps catalog entries to their host form kind', () => {
    assert.equal(taskFormKind({ name: 'x', source: 'plugin' }), 'generic')
    assert.equal(taskFormKind({ name: 'REINDEX', source: 'core' }), 'reindex')
    assert.equal(taskFormKind({ name: 'IMPORT', source: 'core' }), 'import_page')
    assert.equal(taskFormKind({ name: 'EXPORT', source: 'core' }), 'export_page')
    assert.equal(taskFormKind({ name: 'MYSTERY', source: 'core' }), 'unsupported_core')
  })

  it('resolves the retired sync names to the neutral unsupported kind', () => {
    assert.equal(taskFormKind({ name: 'SYNC_PULL', source: 'core' }), 'unsupported_core')
    assert.equal(taskFormKind({ name: 'SYNC_PUSH', source: 'core' }), 'unsupported_core')
    assert.equal(taskFormKind({ name: 'SYNC_EXPORT', source: 'core' }), 'unsupported_core')
  })
})

describe('orderedScopeRequirements', () => {
  it('orders supported scope fields and returns unknown names separately', () => {
    assert.deepEqual(SCOPE_FIELD_ORDER, [
      'source_id',
      'schema_id',
      'target_id',
      'object_type_id',
      'service_account_id',
    ])

    const result = orderedScopeRequirements({
      scope_requirements: ['target_id', 'schema_id', 'custom_scope', 'source_id'],
    })
    assert.deepEqual(result.fields, ['source_id', 'schema_id', 'target_id'])
    assert.deepEqual(result.unsupportedScope, ['custom_scope'])
  })
})

describe('buildParameterFields', () => {
  it('copies title/description and numeric bounds, and maps unsupported shapes', () => {
    const fields = buildParameterFields({
      properties: {
        limit: { type: 'integer', minimum: 1, maximum: 50, default: 5, title: 'Limit', description: 'How many' },
        ratio: { type: 'number' },
        ratio_step: { type: 'number', multipleOf: 0.5 },
        secret: { type: 'string', format: 'password' },
        nested: { type: 'object' },
        list: { type: 'array' },
        ref: { $ref: '#/$defs/x' },
        choice: { oneOf: [{ title: 'A' }, { title: 'B' }] },
      },
      required: ['limit'],
    })
    const byKey = Object.fromEntries(fields.map((field) => [field.key, field]))

    assert.equal(byKey.limit.kind, 'integer')
    assert.equal(byKey.limit.min, 1)
    assert.equal(byKey.limit.max, 50)
    assert.equal(byKey.limit.step, 1)
    assert.equal(byKey.limit.title, 'Limit')
    assert.equal(byKey.limit.description, 'How many')
    assert.equal(byKey.limit.required, true)
    assert.equal(byKey.limit.defaultValue, 5)

    assert.equal(byKey.ratio.step, 'any')
    assert.equal(byKey.ratio_step.step, 0.5)

    assert.equal(byKey.secret.kind, 'unsupported')
    assert.equal(byKey.secret.unsupportedType, 'password')
    assert.equal(byKey.nested.kind, 'unsupported')
    assert.equal(byKey.nested.unsupportedType, 'object')
    assert.equal(byKey.list.unsupportedType, 'array')
    assert.equal(byKey.ref.unsupportedType, '$ref')
    assert.equal(byKey.choice.unsupportedType, 'oneOf')
  })
})

describe('initialParameterValues', () => {
  it('prefers defaults and fills booleans, enums and text predictably', () => {
    const fields = [
      { key: 'with_default', kind: 'string', defaultValue: 'x' },
      { key: 'flag', kind: 'boolean' },
      { key: 'flag_default', kind: 'boolean', defaultValue: true },
      { key: 'req_enum', kind: 'enum', required: true, options: [{ value: 'fast' }, { value: 'slow' }] },
      { key: 'opt_enum', kind: 'enum', required: false, options: [{ value: 'a' }] },
      { key: 'text', kind: 'string' },
      { key: 'count', kind: 'integer' },
    ]

    assert.deepEqual(initialParameterValues(fields), {
      with_default: 'x',
      flag: false,
      flag_default: true,
      req_enum: 'fast',
      opt_enum: '',
      text: '',
      count: '',
    })
  })
})

describe('missingScope', () => {
  it('returns ordered supported scope fields that are empty', () => {
    assert.deepEqual(missingScope({ scope_requirements: ['target_id', 'source_id', 'schema_id'] }, {}), [
      'source_id',
      'schema_id',
      'target_id',
    ])
    assert.deepEqual(missingScope({ scope_requirements: ['source_id', 'schema_id'] }, { source_id: 's' }), [
      'schema_id',
    ])
  })
})

describe('validateTaskForm', () => {
  const entry = { scope_requirements: ['schema_id'] }
  const fields = [
    { key: 'label', kind: 'string', required: true },
    { key: 'count', kind: 'integer', required: false },
    { key: 'nested', kind: 'unsupported', unsupportedType: 'object', required: false },
  ]

  it('reports missing scope and required fields', () => {
    const result = validateTaskForm({ entry, scope: {}, values: { label: '', count: '' }, fields })
    assert.deepEqual(result.scopeErrors, { schema_id: 'required' })
    assert.deepEqual(result.fieldErrors, { label: 'required' })
    assert.equal(result.blocked, false)
  })

  it('reports a non-numeric integer', () => {
    const result = validateTaskForm({
      entry,
      scope: { schema_id: 's' },
      values: { label: 'x', count: 'abc' },
      fields,
    })
    assert.deepEqual(result.fieldErrors, { count: 'number' })
  })

  it('blocks on an unsupported required field or an unsupported scope requirement', () => {
    const blockedField = validateTaskForm({
      entry,
      scope: { schema_id: 's' },
      values: {},
      fields: [{ key: 'nested', kind: 'unsupported', unsupportedType: 'object', required: true }],
    })
    assert.equal(blockedField.blocked, true)

    const blockedScope = validateTaskForm({
      entry: { scope_requirements: ['custom_scope'] },
      scope: {},
      values: {},
      fields: [],
    })
    assert.equal(blockedScope.blocked, true)
  })

  it('is clean when everything is valid', () => {
    const result = validateTaskForm({
      entry,
      scope: { schema_id: 's' },
      values: { label: 'x', count: 5 },
      fields,
    })
    assert.deepEqual(result.scopeErrors, {})
    assert.deepEqual(result.fieldErrors, {})
    assert.equal(result.blocked, false)
  })
})

describe('buildPluginTaskPayload', () => {
  it('builds a plugin task payload with coerced parameters and current_step null', () => {
    const fields = [
      { key: 'dry_run', kind: 'boolean' },
      { key: 'limit', kind: 'integer' },
      { key: 'label', kind: 'string', required: true },
      { key: 'mode', kind: 'enum', options: [{ value: 'fast' }, { value: 'slow' }] },
      { key: 'nested', kind: 'unsupported', unsupportedType: 'object' },
      { key: 'optional_text', kind: 'string' },
    ]

    const payload = buildPluginTaskPayload({
      entry: { name: 'synthetic.generic-demo' },
      scope: { schema_id: 'sch-1', source_id: '', target_id: null },
      values: { dry_run: true, limit: '5', label: 'L', mode: 'fast', nested: { a: 1 }, optional_text: '' },
      fields,
      isPermanent: false,
      name: '',
    })

    assert.deepEqual(payload, {
      tasktype: 'synthetic.generic-demo',
      schema_id: 'sch-1',
      current_step: null,
      is_permanent: false,
      plugin_parameters: { dry_run: true, limit: 5, label: 'L', mode: 'fast' },
    })
  })

  it('includes name only for a permanent task with a non-blank trimmed name', () => {
    const permanent = buildPluginTaskPayload({
      entry: { name: 'x' },
      scope: {},
      values: {},
      fields: [],
      isPermanent: true,
      name: '  Nightly  ',
    })
    assert.equal(permanent.name, 'Nightly')
    assert.equal(permanent.is_permanent, true)

    const manual = buildPluginTaskPayload({
      entry: { name: 'x' },
      scope: {},
      values: {},
      fields: [],
      isPermanent: false,
      name: 'Nightly',
    })
    assert.equal('name' in manual, false)
  })

  it('passes plugin_parameters through unchanged when fields is null', () => {
    const payload = buildPluginTaskPayload({
      entry: { name: 'p.x' },
      scope: { schema_id: 's' },
      values: { anything: [1, 2], other: 'v' },
      fields: null,
      isPermanent: true,
      name: 'N',
    })
    assert.deepEqual(payload.plugin_parameters, { anything: [1, 2], other: 'v' })
    assert.equal(payload.schema_id, 's')
    assert.equal(payload.name, 'N')
  })
})
it('the Create Task modal offers neither IMPORT nor EXPORT (created on their own pages)', () => {
  assert.equal(isCreatableInTaskModal({ name: 'IMPORT', source: 'core' }), false)
  assert.equal(isCreatableInTaskModal({ name: 'EXPORT', source: 'core' }), false)
  assert.equal(isCreatableInTaskModal({ name: 'REINDEX', source: 'core' }), true)
  assert.equal(isCreatableInTaskModal({ name: 'jira.sync-pull', source: 'plugin' }), true)
})

describe('isCreatableInTaskModal catalog_visible filtering (SEAM-05)', () => {
  it('excludes a catalog_visible=false entry from the generic Create Task dialog', () => {
    assert.equal(
      isCreatableInTaskModal({ name: 'p.hidden', source: 'plugin', catalog_visible: false }),
      false
    )
  })

  it('includes a catalog_visible=true entry', () => {
    assert.equal(
      isCreatableInTaskModal({ name: 'p.visible', source: 'plugin', catalog_visible: true }),
      true
    )
  })

  it('includes an entry without the field (older catalogue stays visible)', () => {
    assert.equal(
      isCreatableInTaskModal({ name: 'p.legacy', source: 'plugin' }),
      true
    )
  })

  it('only treats a strict false as hidden (null is not false)', () => {
    assert.equal(
      isCreatableInTaskModal({ name: 'p.null', source: 'plugin', catalog_visible: null }),
      true
    )
  })
})
