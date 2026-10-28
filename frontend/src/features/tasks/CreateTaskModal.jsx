import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { useSources } from '../../shared/hooks/use-sources'
import { useSchemas } from '../../shared/hooks/use-schemas'
import {
  useTriggerReindex,
  useTriggerReindexAll,
  useCreateTask,
} from '../../shared/hooks/use-other-resources'
import { useTaskTypes } from '../../shared/hooks/use-task-types'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { extractErrorMessage } from '../../shared/lib/utils'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import {
  sortCatalogEntries,
  typeOptionText,
  taskFormKind,
  isCreatableInTaskModal,
  buildParameterFields,
  initialParameterValues,
  orderedScopeRequirements,
  validateTaskForm,
  missingScope,
  buildPluginTaskPayload,
} from './task-catalog'
import GenericParameterForm from './GenericParameterForm'
import { usePluginTaskForm } from '../../shared/plugin-runtime/PluginProvider'
import { PluginSlotBoundary, PluginSlotErrorRow } from '../../shared/plugin-runtime/PluginSlotBoundary'

// Focus the first invalid control (scope pickers render before parameter
// fields, so DOM order = scope errors first, then field order).
function focusFirstInvalidControl(entry, validation, fields) {
  const { fields: scopeFields } = orderedScopeRequirements(entry)
  const firstScope = scopeFields.find((field) => validation.scopeErrors[field])
  const firstField = (fields || []).find((field) => validation.fieldErrors[field.key])
  const id = firstScope
    ? 'generic-scope-' + firstScope
    : firstField
      ? 'generic-param-' + firstField.key
      : ''
  const element = id ? document.getElementById(id) : null
  if (element) element.focus()
}

// The existing scope validation messages, reused by the contributed taskForms
// path (which has no inline field controls to attach errors to, so the message
// goes into the host error banner).
function scopeValidationText(t, field) {
  switch (field) {
    case 'source_id':
      return t('tasks.validation.source', 'Please select a source')
    case 'schema_id':
      return t('tasks.validation.schema', 'Please select a schema')
    case 'target_id':
      return t('tasks.validation.target', 'Please select a Target')
    case 'object_type_id':
      return t('tasks.validation.object_type', 'Please select an ObjectType')
    case 'service_account_id':
      return t('tasks.validation.service_account', 'Please select a service account')
    default:
      return ''
  }
}

export default function CreateTaskModal({ isOpen, onClose }) {
  const { t, i18n } = useTranslation()
  // D-09: no type is preselected when the modal opens.
  const [taskType, setTaskType] = useState('')
  // REINDEX host form scope (the only surviving host scope; plugin entries
  // collect their own scope through the generic/contributed form).
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [selectedSchemaId, setSelectedSchemaId] = useState('')
  const [isPermanent, setIsPermanent] = useState(false)
  const [taskName, setTaskName] = useState('')
  const [successMessage, setSuccessMessage] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  // Generic plugin form state (D-10): the scope ids, the parameter values and
  // the inline validation errors of the currently selected plugin type.
  const [pluginScope, setPluginScope] = useState({})
  const [pluginValues, setPluginValues] = useState({})
  const [pluginErrors, setPluginErrors] = useState({ scopeErrors: {}, fieldErrors: {} })
  // FEEXT-05: a plugin's own taskForms contribution. A render error disables
  // Trigger (T-291-06); the contribution's `valid: false` report does too.
  const [taskFormFaulted, setTaskFormFaulted] = useState(false)
  const [pluginFormValid, setPluginFormValid] = useState(true)

  // FEEXT-05 (D-08): the task type list comes from the server catalog and is
  // refetched every time the modal opens, so a newly activated plugin type
  // appears without a page reload.
  const {
    data: catalog = [],
    isLoading: catalogLoading,
    isError: catalogError,
    refetch: refetchCatalog,
  } = useTaskTypes({ enabled: isOpen })
  const sortedCatalog = sortCatalogEntries(
    catalog.filter(isCreatableInTaskModal),
    i18n.resolvedLanguage || 'en'
  )

  // The selected catalog entry and the form area it resolves to. A plugin
  // entry without a contributed `taskForms` component (wired by plan 291-06)
  // gets the generic parameter_schema form (D-10).
  const selectedEntry = catalog.find((entry) => entry.name === taskType) || null
  // A2 ownership: `usePluginTaskForm` only returns a contribution for the
  // selected catalog entry's own plugin, so a plugin form can never replace a
  // core or foreign task form. When present it takes precedence over the
  // generic `parameter_schema` form (D-10).
  const pluginForm = usePluginTaskForm(selectedEntry)
  const formKind = pluginForm ? 'plugin_form' : taskFormKind(selectedEntry)
  const PluginForm = pluginForm ? pluginForm.component : null
  const genericFields = useMemo(
    () => (formKind === 'generic' && selectedEntry ? buildParameterFields(selectedEntry.parameter_schema) : []),
    [formKind, selectedEntry]
  )
  const genericBlocked = useMemo(() => {
    if (formKind !== 'generic' || !selectedEntry) return false
    return validateTaskForm({
      entry: selectedEntry,
      scope: pluginScope,
      values: pluginValues,
      fields: genericFields,
    }).blocked
  }, [formKind, selectedEntry, pluginScope, pluginValues, genericFields])
  const entryUnavailable = !!selectedEntry && selectedEntry.dispatchable === false

  const { data: sources = [] } = useSources(0, 1000)
  const { data: schemas = [] } = useSchemas(selectedSourceId)
  const isReindexType = formKind === 'reindex'

  const triggerReindex = useTriggerReindex()
  const triggerReindexAll = useTriggerReindexAll()
  // FEEXT-05: plugin task types submit POST /tasks/ with plugin_parameters.
  // The Trigger button stays disabled while the mutation is pending, so a
  // double click sends exactly one request (idempotency).
  const createTask = useCreateTask()

  const isLoading =
    triggerReindex.isPending ||
    triggerReindexAll.isPending ||
    createTask.isPending

    useEffect(() => {
    if (isOpen) {
      setTaskType('')
      setSelectedSourceId('')
      setSelectedSchemaId('')
      setIsPermanent(false)
      setTaskName('')
      setSuccessMessage(false)
      setErrorMessage('')
      setPluginScope({})
      setPluginValues({})
      setPluginErrors({ scopeErrors: {}, fieldErrors: {} })
      setTaskFormFaulted(false)
      setPluginFormValid(true)
      refetchCatalog()
    }
  }, [isOpen, refetchCatalog])

  // FEEXT-05: a contributed task form is controlled. It owns `scope` and
  // `plugin_parameters` and may report validity; the host keeps the type
  // select, permanent/name fields, banners, footer and the POST.
  const handlePluginFormChange = (next) => {
    setPluginScope(next?.scope ?? {})
    setPluginValues(next?.plugin_parameters ?? {})
    setPluginFormValid(next?.valid !== false)
  }

  // Contributed taskForms submit path (Pitfall 12): every `scope_requirements`
  // column must be present before any request; the plugin's plugin_parameters
  // object passes through unchanged (`fields: null`) with `current_step: null`.
  const submitPluginTask = async () => {
    const missing = missingScope(selectedEntry, pluginScope)
    if (missing.length > 0) {
      setErrorMessage(scopeValidationText(t, missing[0]))
      return
    }
    try {
      await createTask.mutateAsync(
        buildPluginTaskPayload({
          entry: selectedEntry,
          scope: pluginScope,
          values: pluginValues,
          fields: null,
          isPermanent,
          name: taskName,
        })
      )
      setSuccessMessage(true)
      setTimeout(() => {
        setSuccessMessage(false)
        onClose()
      }, 2000)
    } catch (err) {
      setErrorMessage(extractErrorMessage(err, t('tasks.error.trigger', 'Failed to trigger task')))
    }
  }

  // D-10 generic plugin path: client validation is UX only (the server
  // admission decides at submit time — FEEXT-05 concurrency), focus moves to
  // the first invalid control, then POST /tasks/ with plugin_parameters and
  // current_step null. A 409/422 admission message is shown verbatim in the
  // existing error banner and the modal stays open.
  const submitGenericTask = async () => {
    try {
      const validation = validateTaskForm({
        entry: selectedEntry,
        scope: pluginScope,
        values: pluginValues,
        fields: genericFields,
      })
      setPluginErrors({ scopeErrors: validation.scopeErrors, fieldErrors: validation.fieldErrors })
      if (validation.blocked) return
      if (
        Object.keys(validation.scopeErrors).length > 0 ||
        Object.keys(validation.fieldErrors).length > 0
      ) {
        focusFirstInvalidControl(selectedEntry, validation, genericFields)
        return
      }
      await createTask.mutateAsync(
        buildPluginTaskPayload({
          entry: selectedEntry,
          scope: pluginScope,
          values: pluginValues,
          fields: genericFields,
          isPermanent,
          name: taskName,
        })
      )
      setSuccessMessage(true)
      setTimeout(() => {
        setSuccessMessage(false)
        onClose()
      }, 2000)
    } catch (err) {
      setErrorMessage(extractErrorMessage(err, t('tasks.error.trigger', 'Failed to trigger task')))
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setErrorMessage('')
    setSuccessMessage(false)

    if (formKind === 'plugin_form' && selectedEntry) {
      await submitPluginTask()
      return
    }

    if (formKind === 'generic' && selectedEntry) {
      await submitGenericTask()
      return
    }

    if (!isReindexType) return

    try {
      // Reindex is one catalog type: a chosen schema scopes the reindex to
      // that schema; the "All schemas" option (value "") triggers the global
      // reindex instead of the old separate REINDEX_ALL pseudo-type.
      if (selectedSchemaId) {
        await triggerReindex.mutateAsync({
          schema_id: selectedSchemaId,
          is_permanent: isPermanent,
          name: isPermanent ? taskName.trim() || undefined : undefined,
        })
      } else {
        await triggerReindexAll.mutateAsync({
          is_permanent: isPermanent,
          name: isPermanent ? taskName.trim() || undefined : undefined,
        })
      }

      setSuccessMessage(true)
      setTimeout(() => {
        setSuccessMessage(false)
        onClose()
      }, 2000)
    } catch (err) {
       setErrorMessage(extractErrorMessage(err, t('tasks.error.trigger', 'Failed to trigger task')))
    }
  }

  if (!isOpen) return null

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
      onClick={onClose}
      data-testid="create-task-modal"
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-2xl max-h-[90vh] flex flex-col gap-4 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
         <h2 className="text-lg font-semibold">{t('tasks.modal.trigger_title', 'Trigger New Task')}</h2>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            onClick={onClose}
            className="h-7 w-7 text-lg leading-none"
             aria-label={t('common.close', 'Close')}
          >
            ×
          </Button>
        </div>

        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col gap-4">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1" data-testid="create-task-body">
          <div className="space-y-1.5">
           <label className="text-sm font-medium text-foreground">{t('tasks.form.task_type', 'Task Type')}</label>
            <select
              value={taskType}
              onChange={(e) => {
                const nextType = e.target.value
                const nextEntry = catalog.find((entry) => entry.name === nextType) || null
                setTaskType(nextType)
                setSelectedSourceId('')
                setSelectedSchemaId('')
                setErrorMessage('')
                // Reset the type-specific generic-form state; a plugin type
                // pre-fills its parameter values from the schema defaults.
                setPluginScope({})
                setPluginErrors({ scopeErrors: {}, fieldErrors: {} })
                // Switching types resets the contributed form's boundary and
                // validity (the boundary is also keyed by tasktype).
                setTaskFormFaulted(false)
                setPluginFormValid(true)
                setPluginValues(
                  nextEntry && nextEntry.source === 'plugin'
                    ? initialParameterValues(buildParameterFields(nextEntry.parameter_schema))
                    : {}
                )
              }}
              disabled={catalogLoading || sortedCatalog.length === 0}
              className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground disabled:opacity-50"
              data-testid="task-type-select"
            >
              <option value="">
                {catalogLoading
                  ? t('tasks.catalog.loading', 'Loading task types…')
                  : sortedCatalog.length === 0 && !catalogError
                    ? t('tasks.catalog.empty', 'No task types available.')
                    : t('tasks.form.select_task_type', 'Select a task type...')}
              </option>
              {sortedCatalog.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {typeOptionText(t, entry, 'create')}
                </option>
              ))}
            </select>
            {catalogError && (
              <div className="mt-1.5 flex items-center gap-3">
                <p className="text-xs text-destructive" data-testid="task-catalog-error">
                  {t('tasks.catalog.load_failed', 'Task types could not be loaded. Try again.')}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  data-testid="task-catalog-retry"
                  onClick={() => refetchCatalog()}
                >
                  {t('common.try_again', 'Try again')}
                </Button>
              </div>
            )}
          </div>

          {entryUnavailable && (
            <div
              className="flex items-start gap-2 text-sm text-muted-foreground"
              aria-live="polite"
              data-testid="create-task-unavailable-note"
            >
              <LucideIcon name="info" size={14} className="mt-0.5 shrink-0" />
              <span>
                {interpolateDefault(
                  t('tasks.form.unavailable_note', "This task type can't start right now: {{reason}}"),
                  { reason: selectedEntry?.unavailable_reason || '' }
                )}
              </span>
            </div>
          )}


          {formKind === 'unsupported_core' && selectedEntry && (
            <p className="text-sm text-muted-foreground" data-testid="create-task-unsupported-core">
              {t(
                'tasks.form.unsupported_required',
                "This task type needs a setting that can't be entered here. Start it through the API."
              )}
            </p>
          )}

          {isReindexType && (
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                 <label className="text-sm font-medium text-foreground">{t('tasks.form.source', 'Source')}</label>
                <select
                  value={selectedSourceId}
                  onChange={(e) => {
                    setSelectedSourceId(e.target.value)
                    setSelectedSchemaId('')
                  }}
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
                >
                   <option value="">{t('tasks.form.select_source', 'Select a source...')}</option>
                  {sources.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
                {sources.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                     {t('tasks.empty.sources', 'No sources available. Create one in Configuration first.')}
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                 <label className="text-sm font-medium text-foreground">{t('tasks.form.schema', 'Schema')}</label>
                <select
                  value={selectedSchemaId}
                  onChange={(e) => setSelectedSchemaId(e.target.value)}
                  disabled={!selectedSourceId}
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground disabled:opacity-50"
                  data-testid="reindex-schema-select"
                >
                  <option value="">
                     {t('tasks.form.all_schemas', 'All schemas')}
                  </option>
                  {schemas.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
                {!selectedSchemaId && (
                  <p className="text-sm text-muted-foreground">
                     {t('tasks.form.reindex_all_description', 'This will rebuild the OpenSearch index for all schemas. Stale entries will be removed.')}
                  </p>
                )}
              </div>
            </div>
          )}

          {formKind === 'plugin_form' && PluginForm && selectedEntry && (
            // FEEXT-05: the plugin's own form renders in the form flow with no
            // frame, card or heading; the boundary is keyed by tasktype so
            // switching types resets a caught fault (Pitfall 6).
            <PluginSlotBoundary
              key={selectedEntry.name}
              slot={'taskForms:' + selectedEntry.name}
              pluginName={pluginForm.pluginName}
              fallback={<PluginSlotErrorRow slot={'taskForms:' + selectedEntry.name} />}
              onError={() => setTaskFormFaulted(true)}
            >
              <PluginForm
                taskType={selectedEntry}
                value={{ scope: pluginScope, plugin_parameters: pluginValues }}
                onChange={handlePluginFormChange}
                disabled={isLoading}
              />
            </PluginSlotBoundary>
          )}

          {formKind === 'generic' && selectedEntry && (
            <GenericParameterForm
              entry={selectedEntry}
              scope={pluginScope}
              onScopeChange={(field, value) => {
                setPluginScope((prev) => {
                  const next = { ...prev, [field]: value }
                  // Changing a picker other pickers depend on clears them.
                  if (field === 'source_id') {
                    next.schema_id = ''
                    next.object_type_id = ''
                    next.service_account_id = ''
                  } else if (field === 'schema_id') {
                    next.object_type_id = ''
                  } else if (field === 'target_id') {
                    next.service_account_id = ''
                  }
                  return next
                })
                setPluginErrors((prev) => ({
                  ...prev,
                  scopeErrors: { ...prev.scopeErrors, [field]: undefined },
                }))
              }}
              values={pluginValues}
              onValuesChange={(key, value) => {
                setPluginValues((prev) => ({ ...prev, [key]: value }))
                setPluginErrors((prev) => ({
                  ...prev,
                  fieldErrors: { ...prev.fieldErrors, [key]: undefined },
                }))
              }}
              errors={pluginErrors}
              disabled={isLoading}
            />
          )}

          {taskType !== '' && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={isPermanent}
                onChange={(e) => setIsPermanent(e.target.checked)}
                className="rounded border-input bg-background dark:bg-gray-800"
                data-testid="is-permanent-task-checkbox" />
               {t('tasks.form.permanent', 'Permanent (prevent deletion when referenced by schedule)')}
            </label>
          )}

          {isPermanent && taskType !== '' && (
            <div className="space-y-1.5">
               <label htmlFor="task-name" className="text-sm font-medium text-foreground">{t('tasks.form.name', 'Name')}</label>
              <input
                id="task-name" type="text" value={taskName}
                onChange={(e) => setTaskName(e.target.value)}
                 placeholder={t('tasks.form.name_placeholder', 'e.g. Nightly import')}
                className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
                data-testid="permanent-task-name-input"
              />
            </div>
          )}

          {successMessage && (
            <div className="flex items-center gap-2 rounded-md bg-green-50 dark:bg-green-950 p-3 text-sm text-green-700 dark:text-green-300" data-testid="create-task-success">
              <span className="text-green-600 dark:text-green-400 font-bold">&#10003;</span>
               {t('tasks.success.triggered', 'Task triggered!')}
            </div>
          )}

          {errorMessage && (
            <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive" data-testid="create-task-error">
              {errorMessage}
            </div>
          )}

          </div>

          <div className="flex shrink-0 justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onClose}
              disabled={isLoading}
            >
               {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={
                taskType === '' ||
                isLoading ||
                entryUnavailable ||
                genericBlocked ||
                (formKind === 'plugin_form' && (taskFormFaulted || !pluginFormValid)) ||
                (formKind === 'unsupported_core' && !!selectedEntry)
              }
              loading={isLoading}
              data-testid="create-task-submit"
            >
               {t('tasks.action.trigger', 'Trigger Task')}
            </Button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  )
}
