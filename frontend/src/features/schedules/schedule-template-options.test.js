import test from 'node:test'
import assert from 'node:assert/strict'

import {
  templatesForType,
  templateScope,
  templateOptionText,
  typeOfTemplate,
} from './schedule-template-options.js'

// A fake `t` returning the default string, as the host does before any
// translation is installed; `formatDate` is injected so the option text is
// asserted without the system timezone.
const fakeT = (key, defaultText) => defaultText
const formatDate = (value) => `DATE(${value})`

test('templatesForType keeps PARTIAL_SUCCESS and drops RUNNING/DEAD_LETTER and other tasktypes', () => {
  const tasks = [
    { id: '1', tasktype: 'REINDEX', current_status: 'PENDING' },
    { id: '2', tasktype: 'REINDEX', current_status: 'SUCCESS' },
    { id: '3', tasktype: 'REINDEX', current_status: 'PARTIAL_SUCCESS' },
    { id: '4', tasktype: 'REINDEX', current_status: 'FAILED' },
    { id: '5', tasktype: 'REINDEX', current_status: 'RUNNING' },
    { id: '6', tasktype: 'REINDEX', current_status: 'DEAD_LETTER' },
    { id: '7', tasktype: 'EXPORT', current_status: 'PENDING' },
  ]
  assert.deepEqual(
    templatesForType(tasks, 'REINDEX').map((task) => task.id),
    ['1', '2', '3', '4']
  )
})

test('templatesForType never offers IMPORT/EXPORT as task templates', () => {
  const tasks = [
    { id: 'i-1', tasktype: 'IMPORT', current_status: 'PENDING' },
    { id: 'e-1', tasktype: 'EXPORT', current_status: 'PENDING' },
  ]
  assert.deepEqual(templatesForType(tasks, 'IMPORT'), [])
  assert.deepEqual(templatesForType(tasks, 'EXPORT'), [])
})

test('templateScope picks the most specific present scope', () => {
  const lookups = {
    objectTypeNames: new Map([['ot-1', 'Object Type A']]),
    schemaNames: new Map([['sc-1', 'Schema A']]),
    targetNames: new Map([['tg-1', 'Target A']]),
    sourceNames: new Map([['sr-1', 'Source A']]),
  }
  assert.deepEqual(
    templateScope({ object_type_id: 'ot-1', schema_id: 'sc-1', target_id: 'tg-1', source_id: 'sr-1' }, lookups),
    { kind: 'named', name: 'Object Type A' }
  )
  assert.deepEqual(
    templateScope({ schema_id: 'sc-1', target_id: 'tg-1', source_id: 'sr-1' }, lookups),
    { kind: 'named', name: 'Schema A' }
  )
  assert.deepEqual(
    templateScope({ target_id: 'tg-1', source_id: 'sr-1' }, lookups),
    { kind: 'named', name: 'Target A' }
  )
  assert.deepEqual(
    templateScope({ source_id: 'sr-1' }, lookups),
    { kind: 'named', name: 'Source A' }
  )
})

test('templateScope falls back to the schema when the ObjectType name is missing, and handles all-schemas/none', () => {
  const lookups = { schemaNames: new Map([['sc-1', 'Schema A']]) }
  assert.deepEqual(
    templateScope({ object_type_id: 'missing', schema_id: 'sc-1' }, lookups),
    { kind: 'named', name: 'Schema A' }
  )
  assert.deepEqual(
    templateScope({ tasktype: 'REINDEX', schema_id: null }, lookups),
    { kind: 'all_schemas' }
  )
  assert.deepEqual(templateScope({}, lookups), { kind: 'none' })
})

test('templateOptionText is name first with type and scope', () => {
  const entry = { name: 'REINDEX', label: 'Reindex' }
  const lookups = { schemaNames: new Map([['sc-1', 'Schema A']]) }
  assert.equal(
    templateOptionText(fakeT, { task: { id: '1', name: 'Nightly', schema_id: 'sc-1' }, entry, lookups, formatDate }),
    'Nightly (Reindex, Schema A)'
  )
  assert.equal(
    templateOptionText(fakeT, { task: { id: '1', name: 'Nightly' }, entry, lookups, formatDate }),
    'Nightly (Reindex)'
  )
  assert.equal(
    templateOptionText(
      fakeT,
      { task: { id: '1', name: 'Nightly', tasktype: 'REINDEX', schema_id: null }, entry, lookups, formatDate }
    ),
    'Nightly (Reindex, all schemas)'
  )
})

test('templateOptionText names an unnamed template with its type and date, then applies the scope suffix', () => {
  const entry = { name: 'REINDEX', label: 'Reindex' }
  const lookups = { schemaNames: new Map([['sc-1', 'Schema A']]) }
  assert.equal(
    templateOptionText(
      fakeT,
      { task: { id: '1', name: null, created_at: '2026-01-02T03:04:05Z' }, entry, lookups, formatDate }
    ),
    'Reindex from DATE(2026-01-02T03:04:05Z) (Reindex)'
  )
  assert.equal(
    templateOptionText(
      fakeT,
      {
        task: { id: '1', name: '', created_at: '2026-01-02T03:04:05Z', schema_id: 'sc-1' },
        entry,
        lookups,
        formatDate,
      }
    ),
    'Reindex from DATE(2026-01-02T03:04:05Z) (Reindex, Schema A)'
  )
})

test('typeOfTemplate resolves export and task templates, or null when the task is not found', () => {
  const tasks = [{ id: 't-1', tasktype: 'REINDEX' }]
  assert.equal(typeOfTemplate({ templateType: 'export', templateId: 'e-1' }, tasks), 'EXPORT')
  assert.equal(typeOfTemplate({ templateType: 'task', templateId: 't-1' }, tasks), 'REINDEX')
  assert.equal(typeOfTemplate({ templateType: 'task', templateId: 'missing' }, tasks), null)
  assert.equal(typeOfTemplate({ templateType: null, templateId: null }, tasks), null)
})