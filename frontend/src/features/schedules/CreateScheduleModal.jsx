import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQueries } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { useCreateSchedule, useUpdateSchedule, useRunSchedule, useSystemSettings } from '../../shared/hooks/use-other-resources'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { useTasks, useExportDefinitions } from '../../shared/hooks/use-other-resources'
import { useTaskTypes } from '../../shared/hooks/use-task-types'
import { useSchemas } from '../../shared/hooks/use-schemas'
import { useSources } from '../../shared/hooks/use-sources'
import { useTargets } from '../../shared/hooks/use-targets'
import { useSchedulePreview } from '../../shared/hooks/use-schedule-preview'
import useDebouncedValue from '../../shared/hooks/use-debounced-value'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { PHASE231_SCHEDULE_PRESETS } from '../../shared/lib/phase231-dynamic-contract'
import { sortCatalogEntries, typeOptionText } from '../tasks/task-catalog'
import { templatesForType, templateOptionText, typeOfTemplate } from './schedule-template-options'

const PRESET_TYPES = PHASE231_SCHEDULE_PRESETS

// createCrudHooks list hooks resolve to the response array; the tasks/export
// hooks wrap theirs in `{ items }`. Accept both so a shape change cannot
// silently drop every scope name.
function listItems(data) {
  return Array.isArray(data) ? data : data?.items || []
}

const DAYS_OF_WEEK = [
  { value: 0, key: 'settings.schedule.weekday.sunday', defaultLabel: 'Sunday' },
  { value: 1, key: 'settings.schedule.weekday.monday', defaultLabel: 'Monday' },
  { value: 2, key: 'settings.schedule.weekday.tuesday', defaultLabel: 'Tuesday' },
  { value: 3, key: 'settings.schedule.weekday.wednesday', defaultLabel: 'Wednesday' },
  { value: 4, key: 'settings.schedule.weekday.thursday', defaultLabel: 'Thursday' },
  { value: 5, key: 'settings.schedule.weekday.friday', defaultLabel: 'Friday' },
  { value: 6, key: 'settings.schedule.weekday.saturday', defaultLabel: 'Saturday' },
]

export default function CreateScheduleModal({ isOpen, onClose, editingSchedule = null }) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [templateType, setTemplateType] = useState(null)  // 'task', 'export'
  const [templateId, setTemplateId] = useState(null)
  const [scheduleType, setScheduleType] = useState(null)  // a catalog task-type `name` or null
  const [staleTemplateType, setStaleTemplateType] = useState(null)  // persisted type no longer in the catalog
  const [enabled, setEnabled] = useState(true)
  const [errors, setErrors] = useState({})
  const [runNowMessage, setRunNowMessage] = useState(false)

  const [tab, setTab] = useState('presets')
  const [selectedPreset, setSelectedPreset] = useState(null)
  const [presetParams, setPresetParams] = useState({})
  const [cronText, setCronText] = useState('')

  const { data: settings = {} } = useSystemSettings()
  const defaultTimezone = settings.default_timezone || 'UTC'
  const formatDate = useFormatDate()

  const createSchedule = useCreateSchedule()
  const updateSchedule = useUpdateSchedule()
  const runSchedule = useRunSchedule()
  const activeMutation = editingSchedule ? updateSchedule : createSchedule
  const schedulePreviewResult = useSchedulePreview()
  const { mutate: previewMutate, reset: previewReset } = schedulePreviewResult

  // FEEXT-05 (D-13): the task type list comes from the server catalog filtered
  // to `schedulable === true` — this is the only filter, no type name is
  // special-cased. Refetched every time the modal opens, so a newly activated
  // plugin type appears without a page reload.
  const {
    data: catalog = [],
    isLoading: catalogLoading,
    refetch: refetchCatalog,
  } = useTaskTypes({ enabled: isOpen })

  const catalogEntries = useMemo(() => (Array.isArray(catalog) ? catalog : []), [catalog])
  const schedulableTypes = useMemo(
    () =>
      sortCatalogEntries(
        catalogEntries.filter((entry) => entry.schedulable === true),
        i18n.resolvedLanguage || 'en'
      ),
    [catalogEntries, i18n.resolvedLanguage]
  )
  const selectedEntry = schedulableTypes.find((entry) => entry.name === scheduleType) || null

  // Fetch the caller's OWN permanent task templates (schedule_id IS NULL,
  // created_by_me) and all ExportDefinitions. 261022-np1: a schedule may only
  // use its creator's template, so offering a foreign one would only produce a
  // 422 on submit.
  const { data: tasksData = {}, isLoading: tasksLoading } = useTasks(0, 1000, null, true, true, null, true)
  const taskTemplates = useMemo(() => tasksData.items || [], [tasksData.items])

  const { data: exportDefsData = {}, isLoading: exportDefsLoading } = useExportDefinitions(0, 1000)
  const exportDefs = useMemo(() => exportDefsData.items || [], [exportDefsData.items])

  const templatesLoading = tasksLoading || exportDefsLoading

  // The templates that belong to the currently selected catalog type: export
  // definitions for EXPORT, every permanent task of that tasktype otherwise.
  const templateList = useMemo(() => {
    if (!scheduleType) return []
    if (scheduleType === 'EXPORT') return exportDefs
    return templatesForType(taskTemplates, scheduleType)
  }, [scheduleType, exportDefs, taskTemplates])

  // D-14 scope lookups: Schema/Source/Target names come from one bounded list
  // each; ObjectType names from a bounded useQueries over only the distinct
  // object_type_ids of the currently listed templates (query key
  // ['object-types', id]). A not-yet-loaded name simply falls through to the
  // next scope level.
  const { data: schemasData = {} } = useSchemas(null, 0, 1000, { enabled: isOpen })
  const { data: sourcesData = {} } = useSources(0, 1000, { enabled: isOpen })
  const { data: targetsData = {} } = useTargets(0, 1000, { enabled: isOpen })

  const objectTypeIds = useMemo(() => {
    const ids = new Set()
    for (const task of templateList) {
      if (task?.object_type_id) ids.add(task.object_type_id)
    }
    return [...ids]
  }, [templateList])

  const objectTypeQueries = useQueries({
    queries: objectTypeIds.map((id) => ({
      queryKey: ['object-types', id],
      queryFn: () => api.get(`/object-types/${id}`).then((r) => r.data),
      enabled: isOpen,
    })),
  })

  const lookups = useMemo(() => {
    const objectTypeNames = new Map()
    objectTypeQueries.forEach((query, index) => {
      if (query.data?.name) objectTypeNames.set(objectTypeIds[index], query.data.name)
    })
    return {
      objectTypeNames,
      schemaNames: new Map(listItems(schemasData).map((schema) => [schema.id, schema.name])),
      sourceNames: new Map(listItems(sourcesData).map((source) => [source.id, source.name])),
      targetNames: new Map(listItems(targetsData).map((target) => [target.id, target.name])),
    }
  }, [objectTypeQueries, objectTypeIds, schemasData, sourcesData, targetsData])

  const [timezone, setTimezone] = useState('UTC')

  const debouncedPresetParams = useDebouncedValue(presetParams, 350)
  const debouncedCronText = useDebouncedValue(cronText, 350)

  const presetParamsComplete = useMemo(() => {
    if (!selectedPreset) return false
    const p = debouncedPresetParams
    switch (selectedPreset) {
      case 'EVERY_N_MINUTES': return !!p.interval_minutes
      case 'EVERY_N_HOURS': return !!p.interval_hours
      case 'DAILY': return p.hour !== null && p.hour !== undefined && p.minute !== null && p.minute !== undefined
      case 'WEEKLY': return p.day_of_week !== null && p.day_of_week !== undefined && p.hour !== null && p.hour !== undefined && p.minute !== null && p.minute !== undefined
      default: return false
    }
  }, [selectedPreset, debouncedPresetParams])

  useEffect(() => {
    if (tab === 'presets' && selectedPreset && presetParamsComplete) {
      previewMutate({ preset_type: selectedPreset, args: debouncedPresetParams, timezone })
    } else if (tab === 'presets') {
      previewReset()
    }
  }, [tab, selectedPreset, debouncedPresetParams, timezone, previewMutate, presetParamsComplete, previewReset])

  useEffect(() => {
    if (tab === 'cron' && debouncedCronText.trim()) {
      previewMutate({ cron_expression: debouncedCronText, timezone })
    }
  }, [tab, debouncedCronText, timezone, previewMutate])

  // Reset form when opening
  useEffect(() => {
    if (isOpen) {
      refetchCatalog()
      if (editingSchedule) {
        setName(editingSchedule.name || '')
        setTemplateType(editingSchedule.template_type || null)
        setTemplateId(editingSchedule.template_id || null)
        setScheduleType(null)  // derived once the catalog/task lists are loaded, see effect below
        setEnabled(editingSchedule.enabled ?? true)
        setErrors({})
        setRunNowMessage(false)
        setTimezone(defaultTimezone)
        if (editingSchedule.preset_type) {
          setTab('presets')
          setSelectedPreset(editingSchedule.preset_type)
          setPresetParams(editingSchedule.args || {})
          setCronText('')
        } else {
          setTab('cron')
          setSelectedPreset(null)
          setPresetParams({})
          setCronText(editingSchedule.cron_expression || '')
        }
        previewReset()
      } else {
        setName('')
        setTemplateType(null)
        setTemplateId(null)
        setScheduleType(null)
        setEnabled(true)
        setErrors({})
        setTab('presets')
        setSelectedPreset(null)
        setPresetParams({})
        setCronText('')
        setTimezone(defaultTimezone)
        previewReset()
      }
    }
  }, [isOpen, editingSchedule, defaultTimezone, previewReset, refetchCatalog])

  // Derive the type-select value once the underlying task/export lists have
  // loaded — runs after the reset effect above, only fills in while
  // scheduleType is unset. A type that is not schedulable stays unset.
  useEffect(() => {
    if (!isOpen || !editingSchedule || scheduleType || staleTemplateType) return
    if (catalogLoading) return
    const derived = typeOfTemplate(
      { templateType: editingSchedule.template_type, templateId: editingSchedule.template_id },
      taskTemplates
    )
    if (!derived) return
    if (schedulableTypes.some((entry) => entry.name === derived)) setScheduleType(derived)
    else setStaleTemplateType(derived)
  }, [isOpen, editingSchedule, scheduleType, staleTemplateType, catalogLoading, schedulableTypes, taskTemplates])

  const handlePresetParamChange = (key, value) => {
    setPresetParams((prev) => ({ ...prev, [key]: value }))
  }

  const handleTabSwitch = (newTab) => {
    setTab(newTab)
    if (newTab === 'cron') {
      setSelectedPreset(null)
      setPresetParams({})
    } else if (newTab !== 'presets') {
      // next_run tab — don't clear
    } else {
      setCronText('')
    }
    previewReset()
  }

  const validate = () => {
    const newErrors = {}
    if (!name.trim()) newErrors.name = t('settings.schedule.validation.name_required', 'Name is required')
    if (
      !templateType ||
      !templateId ||
      !scheduleType ||
      !schedulableTypes.some((entry) => entry.name === scheduleType)
    ) {
      newErrors.template = t('settings.schedule.validation.template', 'Please select a template')
    }
    if (tab === 'presets') {
      if (!selectedPreset) newErrors.preset = t('settings.schedule.validation.preset', 'Please select a preset')
    } else if (tab === 'cron') {
      if (!cronText.trim()) newErrors.cron = t('settings.schedule.validation.cron', 'Cron expression is required')
    }
    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setErrors({})
    if (!validate()) return

    const payload = {
      name: name.trim(),
      template_id: templateId,
      template_type: templateType,
      timezone,
      enabled,
    }

    if (tab === 'presets') {
      payload.preset_type = selectedPreset
      payload.args = presetParams
    } else if (tab === 'cron') {
      payload.cron_expression = cronText.trim()
    }

    try {
      if (editingSchedule) {
        await updateSchedule.mutateAsync({ id: editingSchedule.id, ...payload })
      } else {
        await createSchedule.mutateAsync(payload)
      }
      onClose()
    } catch {
      // Error handled by component state + display below
    }
  }

  // T-291-07-01: the raw persisted type is a text node inside a font-mono span,
  // built by splitting the default copy at the {{type}} token — never injected
  // as markup.
  const staleTypeParts = staleTemplateType
    ? t(
        'settings.schedule.template.type_unavailable',
        'This schedule uses the task type {{type}}, which is no longer available. Choose another template.'
      ).split('{{type}}')
    : null

  if (!isOpen) return null

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="rounded-lg border bg-card p-3 w-full max-w-xl shadow-xl max-h-[95vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold mb-2" data-testid="create-schedule-modal">
          {editingSchedule ? t('settings.schedule.edit', 'Edit Schedule') : t('settings.schedule.create', 'Create Schedule')}
        </h2>

        <form onSubmit={handleSubmit} className="space-y-1.5">
          {/* Name */}
          <div className="space-y-1.5">
            <label htmlFor="schedule-name" className="text-sm font-medium text-foreground">{t('common.name', 'Name')}</label>
            <input
              id="schedule-name" type="text" value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('settings.schedule.name_placeholder', 'Schedule name')}
              className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
              data-testid="schedule-name-input"
            />
            {errors.name && <p className="text-xs text-red-500 mt-1">{errors.name}</p>}
          </div>

          {/* Template Selection — catalog task type, then one of its permanent templates */}
          <div className="space-y-1.5">
            <label htmlFor="schedule-task-type" className="text-sm font-medium text-foreground">{t('tasks.form.task_type', 'Task Type')}</label>
            <select
              id="schedule-task-type"
              value={scheduleType || ''}
              onChange={(e) => {
                setScheduleType(e.target.value || null)
                setStaleTemplateType(null)
                setTemplateType(null)
                setTemplateId(null)
              }}
              disabled={catalogLoading}
              className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground disabled:opacity-50"
              data-testid="schedule-task-type-select"
            >
              <option value="">
                {catalogLoading
                  ? t('common.loading', 'Loading...')
                  : t('settings.schedule.template.select_type', 'Select type...')}
              </option>
              {schedulableTypes.map((entry) => (
                <option key={entry.name} value={entry.name}>{typeOptionText(t, entry, 'schedule')}</option>
              ))}
            </select>

            {/* D-13: a dispatchable=false type stays selectable; the server decides at run time. */}
            {selectedEntry && selectedEntry.dispatchable === false && (
              <div
                className="flex items-start gap-2 text-sm text-muted-foreground"
                aria-live="polite"
                data-testid="schedule-unavailable-note"
              >
                <LucideIcon name="info" size={14} className="mt-0.5 shrink-0" />
                <span>
                  {interpolateDefault(
                    t('tasks.form.unavailable_note', "This task type can't start right now: {{reason}}"),
                    { reason: selectedEntry.unavailable_reason || '' }
                  )}
                </span>
              </div>
            )}

            {/* Edit mode: the persisted type is not schedulable anymore — both
                selects stay empty and the raw type is named in font-mono. */}
            {staleTypeParts && (
              <div
                className="flex items-start gap-2 text-sm text-muted-foreground"
                aria-live="polite"
                data-testid="schedule-type-unavailable-note"
              >
                <LucideIcon name="info" size={14} className="mt-0.5 shrink-0" />
                <span>
                  {staleTypeParts[0]}
                  <span className="font-mono">{staleTemplateType}</span>
                  {staleTypeParts[1]}
                </span>
              </div>
            )}

            {scheduleType && (templatesLoading || templateList.length > 0) && (
              <div className="space-y-1.5">
                <label htmlFor="schedule-template" className="text-sm font-medium text-foreground">{t('settings.schedule.template.label', 'Template')}</label>
                <select
                  id="schedule-template"
                  value={templateId || ''}
                  onChange={(e) => {
                    const id = e.target.value || null
                    if (!id) {
                      setTemplateType(null)
                      setTemplateId(null)
                    } else {
                      setTemplateType(scheduleType === 'EXPORT' ? 'export' : 'task')
                      setTemplateId(id)
                    }
                  }}
                  disabled={templatesLoading}
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground disabled:opacity-50"
                  data-testid="schedule-template-select"
                >
                  <option value="">
                    {templatesLoading
                      ? t('common.loading', 'Loading...')
                      : t('settings.schedule.template.select_item', 'Select item...')}
                  </option>
                  {!templatesLoading && (scheduleType === 'EXPORT'
                    ? exportDefs.map((def) => (
                        <option key={def.id} value={def.id} title={def.id}>{def.name}</option>
                      ))
                    : templateList.map((task) => (
                        <option key={task.id} value={task.id} title={task.id}>
                          {templateOptionText(t, { task, entry: selectedEntry, lookups, formatDate })}
                        </option>
                      )))}
                </select>
              </div>
            )}

            {/* D-12: a type without a permanent template stays selectable and
                explains how to create one first. EXPORT has its own variant. */}
            {scheduleType && !templatesLoading && templateList.length === 0 && (
              <div
                className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"
                aria-live="polite"
                data-testid="schedule-no-template-note"
              >
                <LucideIcon name="info" size={14} className="shrink-0" />
                <span>
                  {scheduleType === 'EXPORT'
                    ? t('settings.schedule.template.none_export', 'There are no export definitions yet. Create one on the Export page.')
                    : t('settings.schedule.template.none_create_first', 'There is no permanent task of this type yet. Create one first: open Tasks, choose Create Task and check Permanent.')}
                </span>
                <Button
                  type="button"
                  variant="link"
                  data-testid={scheduleType === 'EXPORT' ? 'schedule-open-export' : 'schedule-open-tasks'}
                  onClick={() => {
                    const target = scheduleType === 'EXPORT' ? '/exports' : '/tasks'
                    onClose()
                    navigate(target)
                  }}
                >
                  {scheduleType === 'EXPORT'
                    ? t('tasks.action.open_export', 'Open Export')
                    : t('settings.schedule.template.open_tasks', 'Open Tasks')}
                </Button>
              </div>
            )}

            {errors.template && <p className="text-xs text-red-500 mt-1">{errors.template}</p>}
          </div>

          {/* Tab Switch (same as before) */}
          <div className="flex gap-1 border-b">
            <button type="button" onClick={() => handleTabSwitch('presets')}
              className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                tab === 'presets' ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'
              }`}>{t('settings.schedule.tab.presets', 'Presets')}</button>
            <button type="button" onClick={() => handleTabSwitch('cron')}
              className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                tab === 'cron' ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'
              }`}>{t('settings.schedule.tab.cron', 'Cron')}</button>
            {editingSchedule && (
              <button type="button" onClick={() => handleTabSwitch('next_run')}
                className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                  tab === 'next_run' ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'
                }`}>{t('settings.schedule.tab.next_run', 'Next Run')}</button>
            )}
          </div>

          {/* Preset Tab (same as before) */}
          {tab === 'presets' && (
            <div className="space-y-1">
              <div className="flex flex-wrap gap-2">
                {PRESET_TYPES.map((p) => (
                  <Button key={p.value} type="button"
                    variant={selectedPreset === p.value ? 'primary' : 'outline'} size="md"
                    onClick={() => setSelectedPreset(p.value)}>{t(p.key, p.defaultLabel)}</Button>
                ))}
              </div>
              {errors.preset && <p className="text-xs text-red-500">{errors.preset}</p>}

              {selectedPreset === 'EVERY_N_MINUTES' && (
                <select value={presetParams.interval_minutes || ''}
                  onChange={(e) => handlePresetParamChange('interval_minutes', Number(e.target.value))}
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                  <option value="">{t('settings.schedule.select_interval', 'Select interval...')}</option>
                  {Array.from({ length: 59 }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>{selectPluralKey('settings.schedule.interval.minute', n) === 'settings.schedule.interval.minute_one'
                       ? interpolateDefault(t('settings.schedule.interval.minute_one', '{{count}} minute'), { count: n })
                       : interpolateDefault(t('settings.schedule.interval.minute_other', '{{count}} minutes'), { count: n })}</option>
                  ))}
                </select>
              )}

              {selectedPreset === 'EVERY_N_HOURS' && (
                <select value={presetParams.interval_hours || ''}
                  onChange={(e) => handlePresetParamChange('interval_hours', Number(e.target.value))}
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                  <option value="">{t('settings.schedule.select_interval', 'Select interval...')}</option>
                  {Array.from({ length: 23 }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>{selectPluralKey('settings.schedule.interval.hour', n) === 'settings.schedule.interval.hour_one'
                       ? interpolateDefault(t('settings.schedule.interval.hour_one', '{{count}} hour'), { count: n })
                       : interpolateDefault(t('settings.schedule.interval.hour_other', '{{count}} hours'), { count: n })}</option>
                  ))}
                </select>
              )}

              {selectedPreset === 'DAILY' && (
                <div className="flex gap-2">
                  <div className="flex items-center gap-2 flex-1">
                      <label className="text-sm font-medium text-foreground whitespace-nowrap">{t('settings.schedule.at', 'at')}</label>
                    <select value={presetParams.hour ?? ''}
                      onChange={(e) => handlePresetParamChange('hour', Number(e.target.value))}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                        <option value="">{t('settings.schedule.hour', 'Hour...')}</option>
                      {Array.from({ length: 24 }, (_, i) => i).map((h) => (
                        <option key={h} value={h}>{String(h).padStart(2, '0')}</option>
                      ))}
                    </select>
                  </div>
                  <span className="text-sm text-muted-foreground self-center">:</span>
                  <div className="flex items-center gap-2 flex-1">
                    <select value={presetParams.minute ?? ''}
                      onChange={(e) => handlePresetParamChange('minute', Number(e.target.value))}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                        <option value="">{t('settings.schedule.minute', 'Min...')}</option>
                      {Array.from({ length: 60 }, (_, i) => i).map((m) => (
                        <option key={m} value={m}>{String(m).padStart(2, '0')}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}

              {selectedPreset === 'WEEKLY' && (
                <div className="flex gap-2 items-end">
                  <div className="flex items-center gap-2">
                      <label className="text-sm font-medium text-foreground whitespace-nowrap">{t('settings.schedule.on', 'on')}</label>
                    <select value={presetParams.day_of_week ?? ''}
                      onChange={(e) => handlePresetParamChange('day_of_week', Number(e.target.value))}
                      className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                        <option value="">{t('settings.schedule.day', 'Day...')}</option>
                      {DAYS_OF_WEEK.map((d) => (
                          <option key={d.value} value={d.value}>{t(d.key, d.defaultLabel)}</option>
                      ))}
                    </select>
                  </div>
                  <div className="flex items-center gap-2 flex-1">
                     <label className="text-sm font-medium text-foreground whitespace-nowrap">{t('settings.schedule.at', 'at')}</label>
                    <select value={presetParams.hour ?? ''}
                      onChange={(e) => handlePresetParamChange('hour', Number(e.target.value))}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                       <option value="">{t('settings.schedule.hour', 'Hour...')}</option>
                      {Array.from({ length: 24 }, (_, i) => i).map((h) => (
                        <option key={h} value={h}>{String(h).padStart(2, '0')}</option>
                      ))}
                    </select>
                  </div>
                  <span className="text-sm text-muted-foreground self-center">:</span>
                  <div className="flex items-center gap-2 flex-1">
                    <select value={presetParams.minute ?? ''}
                      onChange={(e) => handlePresetParamChange('minute', Number(e.target.value))}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground">
                       <option value="">{t('settings.schedule.minute', 'Min...')}</option>
                      {Array.from({ length: 60 }, (_, i) => i).map((m) => (
                        <option key={m} value={m}>{String(m).padStart(2, '0')}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Cron Tab (same as before) */}
          {tab === 'cron' && (
            <div className="space-y-1.5">
                <label htmlFor="schedule-cron" className="text-sm font-medium text-foreground">{t('settings.schedule.cron_expression', 'Cron Expression')}</label>
              <input id="schedule-cron" type="text" value={cronText}
                onChange={(e) => setCronText(e.target.value)}
                 placeholder={t('settings.schedule.cron_placeholder', '0 */6 * * *')}
                className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground" />
              {errors.cron && <p className="text-xs text-red-500 mt-1">{errors.cron}</p>}
            </div>
          )}

          {/* Next Run Tab (same as before) */}
          {tab === 'next_run' && editingSchedule && (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div><span className="text-muted-foreground">{t('settings.schedule.next_run', 'Next Run:')}</span> {formatDate(editingSchedule.next_run_at)}</div>
                <div><span className="text-muted-foreground">{t('settings.schedule.last_run', 'Last Run:')}</span> {formatDate(editingSchedule.last_run_at)}</div>
                <div><span className="text-muted-foreground">{t('settings.schedule.created', 'Created:')}</span> {formatDate(editingSchedule.created_at)}</div>
                <div><span className="text-muted-foreground">{t('settings.schedule.updated', 'Updated:')}</span> {formatDate(editingSchedule.updated_at)}</div>
              </div>
              <div className="flex items-center gap-3">
                <Button type="button" variant="primary" size="sm"
                  onClick={async () => {
                    try {
                      await runSchedule.mutateAsync(editingSchedule.id)
                      setRunNowMessage(true)
                      setTimeout(() => {
                        setRunNowMessage(false)
                        onClose()
                      }, 1500)
                    } catch { /* handled via runSchedule.error below */ }
                  }}
                  loading={runSchedule.isPending}>{t('settings.schedule.run_now', 'Run Now')}</Button>
                {runNowMessage && (
                  <span className="text-sm text-green-600 dark:text-green-400">{t('tasks.success.triggered', 'Task triggered!')}</span>
                )}
              </div>
              {runSchedule.error && (
                <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                  {runSchedule.error?.response?.data?.detail || runSchedule.error?.message || t('tasks.error.trigger', 'Failed to trigger task')}
                </div>
              )}
            </div>
          )}

          {/* Error feedback */}
          {activeMutation.error && (
            <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {activeMutation.error?.response?.data?.detail ||
               activeMutation.error?.message ||
               (editingSchedule ? t('settings.schedule.error.update', 'Failed to update schedule') : t('settings.schedule.error.create', 'Failed to create schedule'))}
            </div>
          )}

          {/* Buttons */}
          <div className="flex items-center justify-between pt-2 border-t">
            <div className="flex items-center gap-2">
              <input type="checkbox" id="enabled" checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="rounded border-input" />
              <label htmlFor="enabled" className="text-sm font-medium">{t('settings.schedule.enabled', 'Enabled')}</label>
            </div>
            <div className="flex gap-2">
              <Button type="button" variant="outline" size="sm"
                onClick={onClose} disabled={activeMutation.isPending}>{t('common.cancel', 'Cancel')}</Button>
              {tab !== 'next_run' && (
                <Button type="submit" variant="primary" size="sm"
                  disabled={activeMutation.isPending} loading={activeMutation.isPending}>
                  {editingSchedule ? t('settings.schedule.save_changes', 'Save Changes') : t('settings.schedule.create', 'Create Schedule')}
                </Button>
              )}
            </div>
          </div>
        </form>
      </div>
    </div>,
    document.body
  )
}