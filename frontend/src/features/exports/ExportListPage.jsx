import { useState, useEffect, useCallback, useRef, Fragment } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useExportDefinitions, useCreateExportDefinition, useUpdateExportDefinition, useDeleteExportDefinition, useTriggerExportTask, useDeleteExportRecord } from '../../shared/hooks/use-other-resources'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { useTasks, useObjectTypeAttributes } from '../../shared/hooks/use-other-resources'
import { useAuth } from '../../shared/hooks/use-auth'
import { useObjectTypes, useObjectType } from '../../shared/hooks/use-object-types'
import { useTargets } from '../../shared/hooks/use-targets'
import { useSources } from '../../shared/hooks/use-sources'
import { useSchemas, useSchema } from '../../shared/hooks/use-schemas'
import { Button } from '../../shared/components/Button'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { downloadExportFile } from '../../shared/lib/download-export'
import TaskRunDetailsModal from '../../shared/components/TaskRunDetailsModal'
import FileDown from 'lucide-react/dist/esm/icons/file-down'

function exportStatusLabel(t, status) {
  switch (status) {
    case 'PENDING': return t('settings.export.status.pending', 'PENDING')
    case 'RUNNING': return t('settings.export.status.running', 'RUNNING')
    case 'SUCCESS': return t('settings.export.status.success', 'SUCCESS')
    case 'FAILED': return t('settings.export.status.failed', 'FAILED')
    default: return status
  }
}

export default function ExportListPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const { data: defsData, isLoading } = useExportDefinitions(skip, pageSize)
  const definitions = defsData?.items || []
  const total = defsData?.total || 0
  const formatDate = useFormatDate()

  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #19): URL-synced q via useSearchParams, preserves unrelated
  // params, resets page to 1 on change or clear. The loaded current page is
  // filtered in memory by definition name — no backend search contract.
  const q = searchParams.get('q') || ''
  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const query = q.trim().toLowerCase()
  const filteredDefs = query
    ? definitions.filter((def) => (def.name || '').toLowerCase().includes(query))
    : definitions

  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingDef, setEditingDef] = useState(null)
  const [expandedId, setExpandedId] = useState(null)
  const [detailsTask, setDetailsTask] = useState(null)
  const [triggeringId, setTriggeringId] = useState(null)
  const [triggerFeedback, setTriggerFeedback] = useState({})
  // Presentation-only projection of a just-queued run, keyed by definition id. The POST
  // response does not carry export_definition_id (the worker persists it when the job
  // starts), so this keeps the run visible in that definition's history immediately.
  const [queuedRuns, setQueuedRuns] = useState({})
  // Runs deleted from the history: their rows are dropped locally at once, so the
  // presentation-only queued projection cannot reappear while the task list refetches.
  const [deletedRunIds, setDeletedRunIds] = useState([])
  const [deletingRunId, setDeletingRunId] = useState(null)
  const historyRowRefs = useRef({})
  const createDef = useCreateExportDefinition()
  const updateDef = useUpdateExportDefinition()
  const deleteDef = useDeleteExportDefinition()
  const deleteExportRun = useDeleteExportRecord()
  const triggerExport = useTriggerExportTask()

  // Form state
  const [formName, setFormName] = useState('')
  const [formSourceId, setFormSourceId] = useState('')
  const [formSchemaId, setFormSchemaId] = useState('')
  const [formObjectTypeId, setFormObjectTypeId] = useState('')
  const [formTargetId, setFormTargetId] = useState('')
  const [formIsPermanent, setFormIsPermanent] = useState(false)
  const [formFieldMappings, setFormFieldMappings] = useState([])
  const [submitting, setSubmitting] = useState(false)

  const { data: sources = [] } = useSources(0, 1000)
  const { data: schemas = [] } = useSchemas(formSourceId || undefined)
  const { data: objTypesData } = useObjectTypes(formSchemaId || undefined, 0, 200, { enabled: !!formSchemaId })
  const objectTypes = objTypesData?.items || objTypesData || []
  const { data: targetsData } = useTargets(0, 200)
  const targets = targetsData?.items || targetsData || []
  const attributesObjectTypeId = editingDef ? editingDef.object_type_id : formObjectTypeId
  const { data: attributesData } = useObjectTypeAttributes(attributesObjectTypeId, 0, 200)
  const attributes = attributesData?.items || attributesData || []

  // ExportDefinition only stores object_type_id — Source/Schema are client-side filter
  // helpers with no backend counterpart, so editing an existing definition has to
  // reverse-lookup Schema (via ObjectType) then Source (via Schema) to pre-select both.
  const { data: editingObjectType } = useObjectType(showCreateModal || editingDef ? editingDef?.object_type_id : undefined)
  const { data: editingSchemaForObjType } = useSchema(editingObjectType?.schema_id)

  // Fetch recent export runs (last 50 tasks of type EXPORT). This is Task.export_definition_id
  // (persisted by the worker per Plan 01 D-16/D-17) — the per-definition history below filters
  // on it. The endpoint is creator-scoped server-side, so the history only ever shows the
  // caller's own runs; the per-definition filter keeps each definition's history distinct.
  const { data: tasksData } = useTasks(0, 50, 'EXPORT')

  useEffect(() => {
    if (editingDef) {
      setFormName(editingDef.name || '')
      setFormObjectTypeId(editingDef.object_type_id || '')
      setFormTargetId(editingDef.target_id || '')
      setFormIsPermanent(editingDef?.is_permanent || false)
      setFormFieldMappings(editingDef.field_mappings || [])
      setFormSourceId('')
      setFormSchemaId('')
    } else {
      setFormName('')
      setFormSourceId('')
      setFormSchemaId('')
      setFormObjectTypeId('')
      setFormTargetId('')
      setFormIsPermanent(false)
      setFormFieldMappings([])
    }
  }, [editingDef, showCreateModal])

  useEffect(() => {
    if (editingDef && editingObjectType?.schema_id) {
      setFormSchemaId(editingObjectType.schema_id)
    }
  }, [editingDef, editingObjectType])

  useEffect(() => {
    if (editingDef && editingSchemaForObjType?.source_id) {
      setFormSourceId(editingSchemaForObjType.source_id)
    }
  }, [editingDef, editingSchemaForObjType])

  // Bring the newly expanded history pair-row into view; `nearest` scrolls only when
  // the row sits outside the viewport, so a normal toggle does not jump the page.
  useEffect(() => {
    if (!expandedId) return
    const row = historyRowRefs.current[expandedId]
    if (row && typeof row.scrollIntoView === 'function') {
      row.scrollIntoView({ block: 'nearest' })
    }
  }, [expandedId])

  const handleDelete = async (id) => {
    if (!(await confirmDialog({ message: t('settings.export.confirm_delete', 'Delete this export definition? This cannot be undone.'), danger: true }))) return
    try {
      await deleteDef.mutateAsync(id)
    } catch { /* handled by interceptor */ }
  }

  const handleTriggerExport = async (defId) => {
    setTriggeringId(defId)
    setTriggerFeedback((prev) => ({ ...prev, [defId]: null }))
    try {
      const createdTask = await triggerExport.mutateAsync({ tasktype: 'EXPORT', export_definition_id: defId })
      // Success only: open THIS definition's history (the single expandedId already
      // collapses any other) and project the POST response as its queued run.
      setQueuedRuns((prev) => ({ ...prev, [defId]: { ...createdTask, export_definition_id: defId } }))
      setExpandedId(defId)
       setTriggerFeedback((prev) => ({ ...prev, [defId]: { type: 'success', message: t('settings.export.success.enqueued', 'Export task enqueued!') } }))
    } catch (err) {
       const detail = err.response?.data?.detail || err.message || t('settings.export.error.trigger', 'Failed to trigger export task')
      setTriggerFeedback((prev) => ({ ...prev, [defId]: { type: 'error', message: typeof detail === 'string' ? detail : JSON.stringify(detail) } }))
    }
    setTriggeringId(null)
    setTimeout(() => {
      setTriggerFeedback((prev) => ({ ...prev, [defId]: null }))
    }, 4000)
  }

  const handleDownload = async (exportId) => {
    try {
      await downloadExportFile(exportId)
    } catch { /* handled by interceptor */ }
  }

  const handleDeleteRun = async (task) => {
    if (!(await confirmDialog({ message: t('common.delete_confirm', 'Delete?'), danger: true }))) return
    setDeletingRunId(task.id)
    try {
      await deleteExportRun.mutateAsync(task.export_id)
      setDeletedRunIds((prev) => [...prev, task.id])
    } catch { /* handled by interceptor */ }
    setDeletingRunId(null)
  }

  const handleAddMapping = () => {
    setFormFieldMappings((prev) => [...prev, { attr_id: '', csv_column: '', column_order: prev.length }])
  }

  const handleAddAllMappings = () => {
    setFormFieldMappings((prev) => {
      const mappedIds = new Set(prev.map((m) => m.attr_id))
      const additions = attributes
        .filter((attr) => !mappedIds.has(attr.id))
        .map((attr) => ({ attr_id: attr.id, csv_column: attr.name, column_order: 0 }))
      return [...prev, ...additions].map((m, i) => ({ ...m, column_order: i }))
    })
  }

  const handleMappingChange = (index, field, value) => {
    setFormFieldMappings((prev) => prev.map((m, i) => (i === index ? { ...m, [field]: value } : m)))
  }

  const handleRemoveMapping = (index) => {
    setFormFieldMappings((prev) => prev.filter((_, i) => i !== index).map((m, i) => ({ ...m, column_order: i })))
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!formName.trim() || !formObjectTypeId) return
    setSubmitting(true)
    try {
      const payload = {
        name: formName.trim(),
        object_type_id: formObjectTypeId,
        field_mappings: formFieldMappings
          .filter((m) => m.attr_id && m.csv_column.trim())
          .map((m, i) => ({ attr_id: m.attr_id, csv_column: m.csv_column.trim(), column_order: i })),
      }
      if (formTargetId) payload.target_id = formTargetId
      payload.is_permanent = formIsPermanent
      if (editingDef) {
        await updateDef.mutateAsync({ id: editingDef.id, ...payload })
      } else {
        await createDef.mutateAsync(payload)
      }
      setShowCreateModal(false)
      setEditingDef(null)
    } catch { /* handled by interceptor */ }
    setSubmitting(false)
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="export-list-page">
        <PageHeader title={t('settings.export.title', 'Export Definitions')} description={t('settings.export.description', 'Manage reusable CSV export configurations')} icon="upload">
          <Button onClick={() => setShowCreateModal(true)} variant="primary" size="sm">{t('settings.export.new', '+ New Export Definition')}</Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.export.search_placeholder', 'Search Export Definitions...')}
          ariaLabel={t('settings.export.search_label', 'Search Export Definitions')}
        />

        {isLoading ? (
          <TableSkeleton rows={5} cols={6} />
        ) : definitions.length === 0 ? (
          <EmptyState
            variant="empty"
            icon={FileDown}
            title={t('settings.export.empty.title', 'No export definitions')}
            description={t('settings.export.empty.description', 'Create an export definition to define CSV export field mappings.')}
            buttonLabel={t('settings.export.create', 'Create Export Definition')}
            onButtonClick={() => setShowCreateModal(true)}
          />
        ) : q && filteredDefs.length === 0 ? (
          <EmptyState
            variant="no-results"
            title={t('settings.export.no_results_title', 'No matching export definitions')}
            description={t('settings.export.no_results_description', 'Try adjusting your search or filters.')}
          />
        ) : (
          <div className="space-y-4">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filteredDefs.map((def) => (
                  <Fragment key={def.id}>
                    <tr className="border-b last:border-0 hover:bg-muted/30" data-testid={`export-def-row-${def.id}`}>
                      <td className="px-4 py-3">
                        <span className="font-medium">{def.name}</span>
                        <span className="ml-2 text-sm text-muted-foreground">
                           {t('settings.export.object_type_id', 'ObjectType:')} {def.object_type_id?.substring(0, 8)}...
                        </span>
                        {triggerFeedback[def.id] && (
                          <span className={`ml-2 text-xs ${triggerFeedback[def.id].type === 'success' ? 'text-green-600' : 'text-destructive'}`}>
                            {triggerFeedback[def.id].message}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <Button type="button" variant="outline" size="sm"
                            onClick={() => handleTriggerExport(def.id)}
                            disabled={triggeringId === def.id}
                            loading={triggeringId === def.id}
                            data-testid={`trigger-export-btn-${def.id}`}>
                            {t('settings.export.action.trigger', 'Trigger Export')}
                          </Button>
                          <Button type="button" variant="outline" size="sm"
                            onClick={() => setExpandedId(expandedId === def.id ? null : def.id)}>
                            {expandedId === def.id ? t('settings.export.action.hide_history', 'Hide History') : t('settings.export.action.history', 'Execution History')}
                          </Button>
                          <Button type="button" variant="outline" size="sm"
                             onClick={() => setEditingDef(def)}>{t('common.edit', 'Edit')}</Button>
                          <Button type="button" variant="destructive" size="sm"
                             onClick={() => handleDelete(def.id)}>{t('common.delete', 'Delete')}</Button>
                        </div>
                      </td>
                    </tr>

                    {/* Execution History — filtered to THIS definition via Task.export_definition_id
                        (persisted by the worker, Plan 01 D-16). Without this filter every
                        definition would show the same unfiltered last-50-EXPORT-tasks list.
                        The just-queued POST response is merged ahead until the server row with
                        the same id appears. Paired-row layout retained from the pre-migration
                        card expansion. */}
                    {expandedId === def.id && (() => {
                      const definitionTasks = (tasksData?.items || [])
                        .filter(t => t.export_definition_id === def.id)
                      const queuedRun = queuedRuns[def.id]
                      const defTasks = [
                        ...(queuedRun && !definitionTasks.some(t => t.id === queuedRun.id) ? [queuedRun] : []),
                        ...definitionTasks,
                      ].filter(t => !deletedRunIds.includes(t.id)).slice(0, 10)
                      return (
                      <tr ref={(node) => { historyRowRefs.current[def.id] = node }}
                        className="border-b last:border-0" data-testid={`export-history-row-${def.id}`}>
                        <td colSpan={2} className="border-t bg-muted/20 px-4 pt-3 pb-0">
                           <h4 className="text-sm font-medium mb-2">{t('settings.export.history.title', 'Recent Export Runs')}</h4>
                          {defTasks.length === 0 ? (
                             <p className="text-sm text-muted-foreground pb-3">{t('settings.export.history.empty', 'No export runs yet.')}</p>
                          ) : (
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="border-b">
                                  <th className="text-left align-middle py-1 pr-2">{t('settings.export.history.status', 'Status')}</th>
                                  <th className="text-left align-middle py-1 pr-2">{t('settings.export.history.started', 'Started')}</th>
                                  <th className="text-left align-middle py-1 pr-2">{t('settings.export.history.completed', 'Completed')}</th>
                                  <th className="text-center align-middle py-1 pr-2">{t('settings.export.history.file', 'File')}</th>
                                  <th className="text-center align-middle py-1 pr-2"></th>
                                </tr>
                              </thead>
                              <tbody>
                                {defTasks.map((task) => (
                                  <tr key={task.id} className="border-b last:border-0" data-testid={`export-run-row-${task.id}`}>
                                    <td className="align-middle py-2 pr-2">
                                      <span className={`text-xs font-medium ${
                                        task.current_status === 'SUCCESS' ? 'text-green-600' :
                                        task.current_status === 'FAILED' ? 'text-red-600' : ''
                                       }`}>{exportStatusLabel(t, task.current_status)}</span>
                                    </td>
                                    <td className="align-middle py-2 pr-2 text-muted-foreground">{formatDate(task.started_at)}</td>
                                    <td className="align-middle py-2 pr-2 text-muted-foreground">{formatDate(task.completed_at)}</td>
                                    <td className="align-middle py-2 pr-2 text-center">
                                      {task.current_status === 'SUCCESS' && task.export_id && (
                                         <Button type="button" variant="outline" size="sm"
                                           onClick={() => handleDownload(task.export_id)}
                                           data-testid={`export-download-btn-${task.id}`}>{t('settings.export.action.download', 'Download')}</Button>
                                      )}
                                    </td>
                                    <td className="align-middle py-2 pr-2">
                                      <div className="flex items-center justify-center gap-2">
                                        <Button type="button" variant="outline" size="sm"
                                          onClick={() => setDetailsTask(task)}
                                          data-testid={`export-run-details-btn-${task.id}`}>
                                           {t('common.details', 'Details')}
                                        </Button>
                                        {/* The UI mirrors the backend's terminal-only rule: an
                                            active run must be stopped before it can be removed. */}
                                        {can('exports', 'delete') && task.export_id
                                          && task.current_status !== 'PENDING'
                                          && task.current_status !== 'RUNNING' && (
                                          <Button type="button" variant="destructive" size="sm"
                                            onClick={() => handleDeleteRun(task)}
                                            disabled={deletingRunId === task.id}
                                            data-testid={`export-delete-btn-${task.id}`}>
                                            {t('common.delete', 'Delete')}
                                          </Button>
                                        )}
                                      </div>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </td>
                      </tr>
                      )
                    })()}
                  </Fragment>
                ))}
              </tbody>
            </Table>

            <PaginationBar page={page} pageSize={pageSize} total={total}
              onPageChange={setPage} onPageSizeChange={setPageSize} />
          </div>
        )}

        {/* Create/Edit modal with form fields */}
        {(showCreateModal || editingDef) && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
            onClick={() => { setShowCreateModal(false); setEditingDef(null) }}>
            <div className="rounded-lg border bg-card p-4 w-full max-w-2xl shadow-xl"
              onClick={(e) => e.stopPropagation()}>
              <h2 className="text-lg font-semibold mb-3" data-testid="export-def-modal">
                 {editingDef ? t('settings.export.edit', 'Edit Export Definition') : t('settings.export.create', 'Create Export Definition')}
              </h2>
              <form onSubmit={handleSubmit} className="space-y-3">
                <div>
                   <label className="text-sm font-medium">{t('common.name', 'Name')}</label>
                  <input type="text" value={formName}
                    onChange={(e) => setFormName(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                     placeholder={t('settings.export.name_placeholder', 'Export definition name')} required />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                   <label className="text-sm font-medium">{t('tasks.form.source', 'Source')}</label>
                    <select value={formSourceId}
                      onChange={(e) => { setFormSourceId(e.target.value); setFormSchemaId(''); setFormObjectTypeId('') }}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                      disabled={!!editingDef}>
                       <option value="">{t('tasks.form.select_source', 'Select a source...')}</option>
                      {sources.map((s) => (
                        <option key={s.id} value={s.id}>{s.name}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                   <label className="text-sm font-medium">{t('tasks.form.schema', 'Schema')}</label>
                    <select value={formSchemaId}
                      onChange={(e) => { setFormSchemaId(e.target.value); setFormObjectTypeId('') }}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                      disabled={!!editingDef || !formSourceId}>
                       <option value="">{t('tasks.form.select_schema', 'Select a schema...')}</option>
                      {schemas.map((s) => (
                        <option key={s.id} value={s.id}>{s.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                     <label className="text-sm font-medium">{t('settings.export.object_type', 'Object Type')}</label>
                    <select value={formObjectTypeId}
                      onChange={(e) => { setFormObjectTypeId(e.target.value); setFormFieldMappings([]) }}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                      disabled={!!editingDef || !formSchemaId} required>
                       <option value="">{t('settings.export.select_object_type', 'Select object type...')}</option>
                      {objectTypes.map((ot) => (
                        <option key={ot.id} value={ot.id}>{ot.name}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                     <label className="text-sm font-medium">{t('tasks.form.target', 'Target')}</label>
                    <select value={formTargetId}
                      onChange={(e) => setFormTargetId(e.target.value)}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm">
                       <option value="">{t('settings.export.no_target', 'No target')}</option>
                      {targets.map((t) => (
                        <option key={t.id} value={t.id}>{t.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
                {formObjectTypeId && (
                  <div className="space-y-2">
                     <label className="text-sm font-medium">{t('settings.export.field_mappings', 'Field Mappings')}</label>
                    {formFieldMappings.length === 0 && (
                      <p className="text-xs text-muted-foreground">
                         {t('settings.export.mapping.empty', 'No fields mapped yet — the export will produce an empty CSV until at least one is added.')}
                      </p>
                    )}
                    {formFieldMappings.length > 0 && (
                      <div className="max-h-[132px] overflow-y-auto space-y-2 pr-1">
                        {formFieldMappings.map((mapping, index) => (
                          <div key={index} className="flex items-center gap-2">
                            <select value={mapping.attr_id}
                              onChange={(e) => handleMappingChange(index, 'attr_id', e.target.value)}
                              className="flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm">
                               <option value="">{t('settings.export.select_attribute', 'Select attribute...')}</option>
                              {attributes.map((attr) => (
                                <option key={attr.id} value={attr.id}>{attr.name}</option>
                              ))}
                            </select>
                            <input type="text" value={mapping.csv_column}
                              onChange={(e) => handleMappingChange(index, 'csv_column', e.target.value)}
                              className="flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                               placeholder={t('settings.export.csv_column_placeholder', 'CSV column name')} />
                            <Button type="button" variant="outline" size="sm"
                               onClick={() => handleRemoveMapping(index)}>{t('common.remove', 'Remove')}</Button>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="flex gap-2">
                      <Button type="button" variant="outline" size="sm" onClick={handleAddMapping}>
                         {t('settings.export.add_mapping', '+ Add mapping')}
                      </Button>
                      <Button type="button" variant="outline" size="sm" onClick={handleAddAllMappings}
                        disabled={attributes.length === 0}>
                         {t('settings.export.add_all', '+ Add all')}
                      </Button>
                    </div>
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 pt-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={formIsPermanent}
                      onChange={(e) => setFormIsPermanent(e.target.checked)}
                      className="rounded border-input bg-background dark:bg-gray-800"
                      data-testid="is-permanent-checkbox" />
                     {t('settings.export.protect_deletion', 'Protect from deletion (used by a Schedule)')}
                  </label>
                  <div className="flex gap-2">
                    <Button type="button" variant="outline" size="sm"
                      onClick={() => { setShowCreateModal(false); setEditingDef(null) }}
                       disabled={submitting}>{t('common.cancel', 'Cancel')}</Button>
                    <Button type="submit" variant="primary" size="sm"
                      disabled={submitting || !formName.trim() || !formObjectTypeId}
                      loading={submitting}>
                       {editingDef ? t('common.save', 'Save') : t('common.create', 'Create')}
                    </Button>
                  </div>
                </div>
              </form>
            </div>
          </div>
        )}

        {detailsTask && (
          <TaskRunDetailsModal task={detailsTask} kind="export" onClose={() => setDetailsTask(null)} />
        )}
      </div>
    </ErrorBoundary>
  )
}
