// Phase 291 (FEEXT-05, D-11/D-13/D-14): pure helpers that turn the task-type
// catalog and the permanent template tasks into the Create Schedule template
// select. Plain ES module with explicit `.js` relative imports so `node:test`
// can load it without a bundler. No React, no hooks.

import { interpolateDefault } from '../../shared/lib/i18n-defaults.js'

// Research Pitfall 13: the example runner ends in PARTIAL_SUCCESS, so a
// permanent template that finished that way must stay selectable. RUNNING and
// DEAD_LETTER stay excluded (as today).
const SCHEDULABLE_TEMPLATE_STATUSES = new Set([
  'PENDING',
  'SUCCESS',
  'PARTIAL_SUCCESS',
  'FAILED',
])

// 261022-np1: IMPORT/EXPORT are never offered as `template_type='task'` task
// templates. Such a clone carries no import/export definition, so the backend
// refuses it for every caller (superusers included); EXPORT is schedulable only
// through its own ExportDefinition path. The import of `templatesForType` in the
// modal is therefore exhaustive without a second filter at the call site.
const NON_TASK_TEMPLATE_TYPES = new Set(['IMPORT', 'EXPORT'])

// D-13: the template list is derived only from the selected catalog type's
// `name` — no type name is special-cased. The caller already fetched only
// permanent, schedule-less tasks (`is_permanent`, `schedule_id IS NULL`).
export function templatesForType(tasks, typeName) {
  if (NON_TASK_TEMPLATE_TYPES.has(typeName)) return []
  return (tasks || []).filter(
    (task) =>
      task && task.tasktype === typeName && SCHEDULABLE_TEMPLATE_STATUSES.has(task.current_status)
  )
}

// D-14 scope precedence: the most specific present scope names the template.
// ObjectType name wins, then Schema, then Target, then Source; a missing
// ObjectType name falls back to the Schema name. A Reindex task without a
// schema is "all schemas"; no scope at all is `{ kind: 'none' }`. The name
// lookups are `Map`s so a not-yet-loaded entity simply falls through.
export function templateScope(task, lookups = {}) {
  const { objectTypeNames, schemaNames, targetNames, sourceNames } = lookups

  if (task?.object_type_id) {
    const name = objectTypeNames?.get(task.object_type_id)
    if (name) return { kind: 'named', name }
  }
  if (task?.schema_id) {
    const name = schemaNames?.get(task.schema_id)
    if (name) return { kind: 'named', name }
  }
  if (task?.target_id) {
    const name = targetNames?.get(task.target_id)
    if (name) return { kind: 'named', name }
  }
  if (task?.source_id) {
    const name = sourceNames?.get(task.source_id)
    if (name) return { kind: 'named', name }
  }
  if (task?.tasktype === 'REINDEX' && !task?.schema_id) return { kind: 'all_schemas' }
  return { kind: 'none' }
}

// D-14 option text, name first: "<name> (<type>, <scope>)". An unnamed
// template reads "<type> from <date>" (its id stays in the option's `title`),
// and that text is then the "name" the scope suffix applies to. Every string is
// interpolated as text via `interpolateDefault` (never i18next vars) so a
// template name containing "&" is never double-escaped (Pitfall 10).
export function templateOptionText(t, { task, entry, lookups = {}, formatDate } = {}) {
  const rawName = typeof task?.name === 'string' ? task.name.trim() : ''
  const name = rawName
    ? rawName
    : interpolateDefault(
        t('settings.schedule.template.unnamed', '{{type}} from {{date}}'),
        { type: entry?.label ?? '', date: formatDate ? formatDate(task?.created_at) : '' }
      )

  const scope = templateScope(task, lookups)
  if (scope.kind === 'none') {
    return interpolateDefault(
      t('settings.schedule.template.option_no_scope', '{{name}} ({{type}})'),
      { name, type: entry?.label ?? '' }
    )
  }

  const scopeText =
    scope.kind === 'all_schemas'
      ? t('settings.schedule.template.scope_all_schemas', 'all schemas')
      : scope.name

  return interpolateDefault(
    t('settings.schedule.template.option', '{{name}} ({{type}}, {{scope}})'),
    { name, type: entry?.label ?? '', scope: scopeText }
  )
}

// The catalog type a persisted schedule template belongs to: 'EXPORT' for an
// export template, the task's own tasktype for a task template, or null when
// the task cannot be found (a disabled plugin or a permission the current user
// no longer holds).
export function typeOfTemplate(selection, tasks) {
  if (!selection?.templateType || !selection?.templateId) return null
  if (selection.templateType === 'export') return 'EXPORT'
  if (selection.templateType !== 'task') return null
  const task = (tasks || []).find((candidate) => candidate && candidate.id === selection.templateId)
  return task?.tasktype ?? null
}