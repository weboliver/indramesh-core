// Phase 291 (FEEXT-05, D-08/D-09/D-10): pure helpers that drive the
// catalog-based Create Task / Create Schedule UI and the Tasks type filter.
//
// Plain ES module with explicit `.js` relative imports so `node:test` can load
// it without a bundler (see task-catalog.test.js). No React, no hooks.

import { interpolateDefault } from '../../shared/lib/i18n-defaults.js'
import { buildConfigFields, coerceConfigFieldValue } from '../plugins/plugin-admin-helpers.js'

// D-08: one flat alphabetical list by the backend `label`, ties broken core
// before plugin, then plugin name, then catalog name — never grouped by origin.
// The input array is never mutated.
export function sortCatalogEntries(entries, lang = 'en') {
  return [...(entries || [])].sort((a, b) => {
    const byLabel = String(a?.label || '').localeCompare(String(b?.label || ''), lang, { sensitivity: 'base' })
    if (byLabel !== 0) return byLabel

    const aCore = a?.source === 'core'
    const bCore = b?.source === 'core'
    if (aCore !== bCore) return aCore ? -1 : 1

    const byPlugin = String(a?.plugin_name || '').localeCompare(String(b?.plugin_name || ''), lang, { sensitivity: 'base' })
    if (byPlugin !== 0) return byPlugin

    return String(a?.name || '').localeCompare(String(b?.name || ''), lang)
  })
}

// D-08 option text. Each variant is a complete catalog key with its own literal
// `t(key, default)` call — never string concatenation (i18next lint
// `checkConcatenation: error`). Core entries show `label` verbatim; plugin
// entries carry the plugin origin; a `dispatchable=false` entry gets the
// `_unavailable` variant in create mode. Schedule mode is only ever called with
// `schedulable=true` entries (the select filters first, per corrected D-13), so
// it reuses the ordinary core/plugin labels; filter mode never adds a suffix
// beyond the plugin origin.
export function typeOptionText(t, entry, mode = 'create') {
  const label = entry?.label
  const plugin = entry?.plugin_name
  const isPlugin = entry?.source === 'plugin'

  if (mode === 'create' && entry?.dispatchable === false) {
    if (isPlugin) {
      return interpolateDefault(
        t('tasks.form.type_option_plugin_unavailable', '{{label}} (Plugin: {{plugin}}, unavailable)'),
        { label, plugin }
      )
    }
    return interpolateDefault(
      t('tasks.form.type_option_unavailable', '{{label}} (unavailable)'),
      { label }
    )
  }

  if (isPlugin) {
    return interpolateDefault(
      t('tasks.form.type_option_plugin', '{{label}} (Plugin: {{plugin}})'),
      { label, plugin }
    )
  }

  return label
}

// The scope columns TaskCreate accepts, in the order the generic form renders
// them (S4: Source, Schema, Target, then ObjectType, then ServiceAccount).
export const SCOPE_FIELD_ORDER = ['source_id', 'schema_id', 'target_id', 'object_type_id', 'service_account_id']

// The form area a catalog entry resolves to. A plugin entry without a
// contributed `taskForms` component (wired by plan 291-06) gets the generic
// parameter form; the surviving core host types keep their own forms/pages
// (REINDEX host form, IMPORT/EXPORT created on their own pages); any other core
// name has no host form and resolves to the neutral unsupported kind. There is
// no per-plugin name matching, no second registry and no legacy sync alias.
export function taskFormKind(entry) {
  if (!entry) return 'unsupported_core'
  if (entry.source === 'plugin') return 'generic'
  switch (entry.name) {
    case 'REINDEX':
      return 'reindex'
    case 'IMPORT':
      return 'import_page'
    case 'EXPORT':
      return 'export_page'
    default:
      return 'unsupported_core'
  }
}

// IMPORT/EXPORT run from their own pages (definitions, uploaded file) and can't
// be created in the Create Task modal, so the modal does not offer them.
//
// SEAM-05 (Carry-03): a `catalog_visible === false` entry is hidden from THIS
// generic Create Task dialog only. The strict `=== false` keeps an older
// catalogue (missing field) and a `null` value visible, so the default is
// backward compatible; and it is display filtering, never an authorization
// check — the task list, list type filter, direct start/retry and scheduling
// never consult this predicate and stay complete for a hidden type.
export function isCreatableInTaskModal(entry) {
  if (entry?.catalog_visible === false) return false
  const kind = taskFormKind(entry)
  return kind !== 'import_page' && kind !== 'export_page'
}

// Split the entry's `scope_requirements` into the supported columns in fixed
// order and any unknown names (rendered as an unsupported note, fail-closed).
export function orderedScopeRequirements(entry) {
  const requirements = Array.isArray(entry?.scope_requirements) ? entry.scope_requirements : []
  const fields = SCOPE_FIELD_ORDER.filter((field) => requirements.includes(field))
  const unsupportedScope = requirements.filter((name) => !SCOPE_FIELD_ORDER.includes(name))
  return { fields, unsupportedScope }
}

// One generic-form field per `parameter_schema` property, in schema order.
// Wraps buildConfigFields (the JSON-schema walker PluginConfigForm already uses)
// and adds the title/description/min/max/step the generic form renders. A
// `format: "password"` string is unsupported: plugin task parameters never
// carry secrets (the backend refuses a secret parameter at declaration time).
export function buildParameterFields(parameterSchema) {
  const properties = parameterSchema?.properties
  return buildConfigFields(parameterSchema).map((field) => {
    const spec = properties?.[field.key]
    const extra = {}
    if (spec && typeof spec === 'object' && !Array.isArray(spec)) {
      if (typeof spec.title === 'string') extra.title = spec.title
      if (typeof spec.description === 'string') extra.description = spec.description
      if (typeof spec.minimum === 'number') extra.min = spec.minimum
      if (typeof spec.maximum === 'number') extra.max = spec.maximum
      if (spec.type === 'integer') {
        extra.step = 1
      } else if (spec.type === 'number') {
        extra.step = typeof spec.multipleOf === 'number' ? spec.multipleOf : 'any'
      }
    }
    if (field.kind === 'password') {
      return { ...field, ...extra, kind: 'unsupported', unsupportedType: 'password' }
    }
    return { ...field, ...extra }
  })
}

// Initial control values: an explicit default wins; a boolean defaults to
// false; a required enum defaults to its first option; everything else starts
// empty.
export function initialParameterValues(fields) {
  const values = {}
  for (const field of fields || []) {
    if (field.defaultValue !== undefined) {
      values[field.key] = field.defaultValue
      continue
    }
    if (field.kind === 'boolean') {
      values[field.key] = false
      continue
    }
    if (field.kind === 'enum') {
      const options = Array.isArray(field.options) ? field.options : []
      values[field.key] = field.required && options.length > 0 ? options[0].value : ''
      continue
    }
    values[field.key] = ''
  }
  return values
}

// The ordered supported scope fields that are still empty. Shared by the
// generic form and plan 291-06's contributed taskForms path (Pitfall 12: every
// scope_requirements column must be collected at Create Task time so a
// permanent plugin template can later be scheduled).
export function missingScope(entry, scope) {
  const { fields } = orderedScopeRequirements(entry)
  return fields.filter((field) => !scope?.[field])
}

// Client-side validation: UX only — the server validates plugin_parameters
// with jsonschema. Missing required scope and required fields block submit with
// inline errors; an unsupported required field or an unsupported scope name
// blocks submit entirely (fail closed).
export function validateTaskForm({ entry, scope = {}, values = {}, fields = [] }) {
  const scopeErrors = {}
  for (const field of missingScope(entry, scope)) scopeErrors[field] = 'required'

  const fieldErrors = {}
  let blocked = orderedScopeRequirements(entry).unsupportedScope.length > 0

  for (const field of fields || []) {
    if (field.kind === 'unsupported') {
      if (field.required) blocked = true
      continue
    }

    const value = values[field.key]
    const empty = value === undefined || value === null || value === ''

    if (field.required && field.kind !== 'boolean' && empty) {
      fieldErrors[field.key] = 'required'
      continue
    }

    if ((field.kind === 'integer' || field.kind === 'number') && !empty && Number.isNaN(Number(value))) {
      fieldErrors[field.key] = 'number'
    }
  }

  return { scopeErrors, fieldErrors, blocked }
}

// The POST /tasks/ body for a plugin type. Only non-empty scope ids are sent;
// plugin_parameters carries the coerced values of the supported fields (numbers
// parsed, empty optional values omitted so they keep the server default,
// unsupported fields omitted). `current_step: null` creates the plugin task
// with no business step (D-10). `fields === null` is the plan 291-06 path for a
// plugin's own taskForms component: its plugin_parameters object passes through
// unchanged.
export function buildPluginTaskPayload({ entry, scope = {}, values = {}, fields = null, isPermanent = false, name = '' } = {}) {
  const payload = {
    tasktype: entry?.name,
    current_step: null,
    is_permanent: !!isPermanent,
  }

  for (const field of SCOPE_FIELD_ORDER) {
    const value = scope?.[field]
    if (value !== undefined && value !== null && value !== '') payload[field] = value
  }

  if (isPermanent) {
    const trimmed = typeof name === 'string' ? name.trim() : ''
    if (trimmed) payload.name = trimmed
  }

  if (fields === null) {
    payload.plugin_parameters = values
    return payload
  }

  const parameters = {}
  for (const field of fields || []) {
    if (field.kind === 'unsupported') continue
    const raw = values[field.key]
    if (raw === undefined || raw === null || raw === '') continue

    if (field.kind === 'integer' || field.kind === 'number') {
      parameters[field.key] = Number(raw)
    } else if (field.kind === 'boolean') {
      parameters[field.key] = !!raw
    } else if (field.kind === 'enum') {
      parameters[field.key] = coerceConfigFieldValue(field, raw)
    } else {
      parameters[field.key] = raw
    }
  }
  payload.plugin_parameters = parameters
  return payload
}
