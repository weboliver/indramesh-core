import { useState, useEffect, useRef, useCallback, Fragment } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { useImportDefinitions, useCreateImportDefinition, useUpdateImportDefinition, useDeleteImportDefinition } from '../../shared/hooks/use-other-resources'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { useTasks, useObjectTypeAttributes, useTriggerImportTask } from '../../shared/hooks/use-other-resources'
import { useObjectTypes, useObjectType } from '../../shared/hooks/use-object-types'
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
import { useCan } from '../../shared/components/Can'

import TaskRunDetailsModal from '../../shared/components/TaskRunDetailsModal'
import api from '../../shared/hooks/use-api'
import FileUp from 'lucide-react/dist/esm/icons/file-up'
import HelpCircle from 'lucide-react/dist/esm/icons/help-circle'
import { PHASE231_IMPORT_TABS } from '../../shared/lib/phase231-dynamic-contract'

// Phase 317 (CR314-23, D-18): the button is reachable again because the folder
// now has a supported writer — POST /import-definitions/{id}/folder-files puts
// the selected CSVs into the import root, and the chart mounts that root from
// one claim shared by the backend and the worker (it used to be a per-pod
// emptyDir, so nothing written there was ever visible to the worker).
const FOLDER_IMPORT_ENABLED = true

function importStatusLabel(t, status) {
  switch (status) {
    case 'PENDING': return t('settings.import.status.pending', 'PENDING')
    case 'RUNNING': return t('settings.import.status.running', 'RUNNING')
    case 'SUCCESS': return t('settings.import.status.success', 'SUCCESS')
    case 'FAILED': return t('settings.import.status.failed', 'FAILED')
    default: return status
  }
}

export default function ImportListPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const { data: defsData, isLoading } = useImportDefinitions(skip, pageSize)
  const definitions = defsData?.items || []
  const total = defsData?.total || 0
  const formatDate = useFormatDate()
  // 261022: starting an import writes objects, so the trigger/upload/folder
  // affordances are Administrator-only by default. The backend gate is the
  // authority; this only avoids dead buttons.
  const canImport = useCan('imports', 'create')

  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #18): URL-synced q via useSearchParams, preserves unrelated
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
  const [triggeringFolderId, setTriggeringFolderId] = useState(null)
  const [folderFeedback, setFolderFeedback] = useState({})
  const [triggeringUploadId, setTriggeringUploadId] = useState(null)
  const [uploadFeedback, setUploadFeedback] = useState({})
  const fileInputRef = useRef(null)
  const folderFileInputRef = useRef(null)
  const pendingTriggerDef = useRef(null)
  const pendingFolderDef = useRef(null)
  const queryClient = useQueryClient()
  const createDef = useCreateImportDefinition()
  const updateDef = useUpdateImportDefinition()
  const deleteDef = useDeleteImportDefinition()
  const triggerImport = useTriggerImportTask()

  // Form state
  const [formName, setFormName] = useState('')
  const [formSourceId, setFormSourceId] = useState('')
  const [formSchemaId, setFormSchemaId] = useState('')
  const [formObjectTypeId, setFormObjectTypeId] = useState('')
  const [formValueSeparator, setFormValueSeparator] = useState(';')
  const [formEncoding, setFormEncoding] = useState('utf-8')
  const [formIsPermanent, setFormIsPermanent] = useState(false)
  const [formIdentityAttrId, setFormIdentityAttrId] = useState('')
  const [formFieldMappings, setFormFieldMappings] = useState([])
  const [formActiveTab, setFormActiveTab] = useState('Field Mappings')
  const [submitting, setSubmitting] = useState(false)

  const { data: sources = [] } = useSources(0, 1000)
  const { data: schemas = [] } = useSchemas(formSourceId || undefined)
  const { data: objTypesData } = useObjectTypes(formSchemaId || undefined, 0, 200, { enabled: !!formSchemaId })
  const objectTypes = objTypesData?.items || objTypesData || []
  const attributesObjectTypeId = editingDef ? editingDef.object_type_id : formObjectTypeId
  const { data: attributesData } = useObjectTypeAttributes(attributesObjectTypeId, 0, 200)
  const attributes = attributesData?.items || attributesData || []

  // ImportDefinition only stores object_type_id — Source/Schema are client-side filter
  // helpers with no backend counterpart, so editing an existing definition has to
  // reverse-lookup Schema (via ObjectType) then Source (via Schema) to pre-select both.
  const { data: editingObjectType } = useObjectType(editingDef?.object_type_id)
  const { data: editingSchemaForObjType } = useSchema(editingObjectType?.schema_id)

  // Fetch recent import runs (last 50 tasks of type IMPORT). Filtered per-definition below
  // via Task.import_definition_id (persisted by the worker per Plan 01 D-17) — see the same
  // note on the Export page above.
  const { data: tasksData } = useTasks(0, 50, 'IMPORT')

  useEffect(() => {
    if (editingDef) {
      setFormName(editingDef.name || '')
      setFormObjectTypeId(editingDef.object_type_id || '')
      setFormValueSeparator(editingDef.value_separator || ';')
      setFormEncoding(editingDef.encoding || 'utf-8')
      setFormIsPermanent(editingDef?.is_permanent || false)
      setFormIdentityAttrId(editingDef.identity_attr_id || '')
      setFormFieldMappings(editingDef.field_mappings || [])
      setFormSourceId('')
      setFormSchemaId('')
      setFormActiveTab('Field Mappings')
    } else {
      setFormName('')
      setFormSourceId('')
      setFormSchemaId('')
      setFormObjectTypeId('')
      setFormValueSeparator(';')
      setFormEncoding('utf-8')
      setFormIsPermanent(false)
      setFormIdentityAttrId('')
      setFormFieldMappings([])
      setFormActiveTab('Field Mappings')
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

  const handleDelete = async (id) => {
    if (!(await confirmDialog({ message: t('settings.import.confirm_delete', 'Delete this import definition? This cannot be undone.'), danger: true }))) return
    try {
      await deleteDef.mutateAsync(id)
    } catch { /* handled by interceptor */ }
  }

  const handleTriggerImport = (defId) => {
    pendingTriggerDef.current = defId
    fileInputRef.current?.click()
  }

  const handleFileSelected = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    const defId = pendingTriggerDef.current
    if (!defId) return
    setTriggeringUploadId(defId)
    setUploadFeedback((prev) => ({ ...prev, [defId]: null }))
    try {
      await triggerImport.mutateAsync({ file, import_definition_id: defId })
       setUploadFeedback((prev) => ({ ...prev, [defId]: { type: 'success', message: t('settings.import.success.enqueued', 'Import task enqueued!') } }))
    } catch (err) {
       const detail = err.response?.data?.detail || err.message || t('settings.import.error.trigger', 'Failed to trigger import task')
      setUploadFeedback((prev) => ({ ...prev, [defId]: { type: 'error', message: typeof detail === 'string' ? detail : JSON.stringify(detail) } }))
    }
    setTriggeringUploadId(null)
    pendingTriggerDef.current = null
    if (fileInputRef.current) fileInputRef.current.value = ''
    setTimeout(() => {
      setUploadFeedback((prev) => ({ ...prev, [defId]: null }))
    }, 4000)
  }

  const handleImportFromFolder = (defId) => {
    pendingFolderDef.current = defId
    folderFileInputRef.current?.click()
  }

  const handleFolderFilesSelected = async (e) => {
    const files = Array.from(e.target.files || [])
    const defId = pendingFolderDef.current
    if (!files.length || !defId) return
    setTriggeringFolderId(defId)
    setFolderFeedback((prev) => ({ ...prev, [defId]: null }))
    try {
      // Upload first (CR314-23): the scan below only sees files that are already
      // in the import folder, so uploading after it would enqueue nothing.
      const formData = new FormData()
      files.forEach((file) => formData.append('files', file))
      const upload = await api.post(`/import-definitions/${defId}/folder-files`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      const uploaded = upload.data.length
      const response = await api.post(`/import-definitions/${defId}/import-from-folder`)
      const count = response.data.length
      const pluralKey = selectPluralKey('settings.import.success.folder_uploaded', count)
      setFolderFeedback((prev) => ({ ...prev, [defId]: { type: 'success', message:
        interpolateDefault(t(
          pluralKey === 'settings.import.success.folder_uploaded_one'
            ? 'settings.import.success.folder_uploaded_one'
            : 'settings.import.success.folder_uploaded_other',
          '{{uploaded}} file(s) uploaded, {{count}} enqueued!'
        ), { uploaded, count }) } }))
      // Refresh the execution-history list so the new tasks show up.
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    } catch (err) {
       const detail = err.response?.data?.detail || err.message || t('settings.import.error.folder', 'Failed to import from folder')
      setFolderFeedback((prev) => ({ ...prev, [defId]: { type: 'error', message: typeof detail === 'string' ? detail : JSON.stringify(detail) } }))
    }
    setTriggeringFolderId(null)
    pendingFolderDef.current = null
    if (folderFileInputRef.current) folderFileInputRef.current.value = ''
    setTimeout(() => {
      setFolderFeedback((prev) => ({ ...prev, [defId]: null }))
    }, 4000)
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
    if (!formName.trim() || !formObjectTypeId || !formSourceId) return
    setSubmitting(true)
    try {
      const payload = {
        name: formName.trim(),
        object_type_id: formObjectTypeId,
        source_id: formSourceId,
        value_separator: formValueSeparator,
        encoding: formEncoding,
        is_permanent: formIsPermanent,
        field_mappings: formFieldMappings
          .filter((m) => m.attr_id && m.csv_column.trim())
          .map((m, i) => ({ attr_id: m.attr_id, csv_column: m.csv_column.trim(), column_order: i })),
      }
      if (formIdentityAttrId) payload.identity_attr_id = formIdentityAttrId
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
      <div className="space-y-6" data-testid="import-list-page">
         <PageHeader title={t('settings.import.title', 'Import Definitions')} description={t('settings.import.description', 'Manage reusable CSV import configurations')} icon="download">
           <Button onClick={() => setShowCreateModal(true)} variant="primary" size="sm">{t('settings.import.new', '+ New Import Definition')}</Button>
        </PageHeader>

        {canImport && (
          <input type="file" accept=".csv" ref={fileInputRef} onChange={handleFileSelected}
            className="hidden" data-testid="trigger-import-file-input" />
        )}

        {canImport && (
          <input type="file" accept=".csv" multiple ref={folderFileInputRef} onChange={handleFolderFilesSelected}
            className="hidden" data-testid="folder-import-file-input" />
        )}

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.import.search_placeholder', 'Search Import Definitions...')}
          ariaLabel={t('settings.import.search_label', 'Search Import Definitions')}
        />

        {isLoading ? (
          <TableSkeleton rows={5} cols={6} />
        ) : definitions.length === 0 ? (
          <EmptyState
            variant="empty"
            icon={FileUp}
             title={t('settings.import.empty.title', 'No import definitions')}
             description={t('settings.import.empty.description', 'Create an import definition to define CSV import field mappings and identity attribute.')}
             buttonLabel={t('settings.import.create', 'Create Import Definition')}
            onButtonClick={() => setShowCreateModal(true)}
          />
        ) : q && filteredDefs.length === 0 ? (
          <EmptyState
            variant="no-results"
            title={t('settings.import.no_results_title', 'No matching import definitions')}
            description={t('settings.import.no_results_description', 'Try adjusting your search or filters.')}
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
                    <tr className="border-b last:border-0 hover:bg-muted/30" data-testid={`import-def-row-${def.id}`}>
                      <td className="px-4 py-3">
                        <span className="font-medium">{def.name}</span>
                        <span className="ml-2 text-sm text-muted-foreground">
                           {t('settings.import.object_type_id', 'ObjectType:')} {def.object_type_id?.substring(0, 8)}...
                        </span>
                        {folderFeedback[def.id] && (
                          <span className={`ml-2 text-xs ${folderFeedback[def.id].type === 'success' ? 'text-green-600' : 'text-destructive'}`}>
                            {folderFeedback[def.id].message}
                          </span>
                        )}
                        {uploadFeedback[def.id] && (
                          <span className={`ml-2 text-xs ${uploadFeedback[def.id].type === 'success' ? 'text-green-600' : 'text-destructive'}`}>
                            {uploadFeedback[def.id].message}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          {canImport && FOLDER_IMPORT_ENABLED && (
                            <Button type="button" variant="outline" size="sm"
                              onClick={() => handleImportFromFolder(def.id)}
                              disabled={triggeringFolderId === def.id}
                              loading={triggeringFolderId === def.id}
                              data-testid={`import-from-folder-btn-${def.id}`}>
                              {t('settings.import.action.from_folder', 'Import from folder')}
                            </Button>
                          )}
                          {canImport && (
                            <Button type="button" variant="outline" size="sm"
                              onClick={() => handleTriggerImport(def.id)}
                              disabled={triggeringUploadId === def.id}
                              loading={triggeringUploadId === def.id}
                              data-testid={`trigger-import-btn-${def.id}`}>
                              {t('settings.import.action.trigger', 'Trigger Import')}
                            </Button>
                          )}
                          <Button type="button" variant="outline" size="sm"
                            onClick={() => setExpandedId(expandedId === def.id ? null : def.id)}>
                            {expandedId === def.id ? t('settings.import.action.hide_history', 'Hide History') : t('settings.import.action.history', 'Execution History')}
                          </Button>
                          <Button type="button" variant="outline" size="sm"
                             onClick={() => setEditingDef(def)}>{t('common.edit', 'Edit')}</Button>
                          <Button type="button" variant="destructive" size="sm"
                             onClick={() => handleDelete(def.id)}>{t('common.delete', 'Delete')}</Button>
                        </div>
                      </td>
                    </tr>

                    {/* Execution History — filtered to THIS definition via Task.import_definition_id
                        (persisted by the worker, Plan 01 D-17). Without this filter every
                        definition would show the same unfiltered last-50-IMPORT-tasks list.
                        Paired-row layout retained from the pre-migration card expansion. */}
                    {expandedId === def.id && (() => {
                      const defTasks = (tasksData?.items || [])
                        .filter(t => t.import_definition_id === def.id && t.current_status !== 'PENDING')
                        .slice(0, 10)
                      return (
                      <tr className="border-b last:border-0" data-testid={`import-history-row-${def.id}`}>
                        <td colSpan={2} className="border-t bg-muted/20 px-4 py-3">
                           <h4 className="text-sm font-medium mb-2">{t('settings.import.history.title', 'Recent Import Runs')}</h4>
                          {defTasks.length === 0 ? (
                             <p className="text-sm text-muted-foreground">{t('settings.import.history.empty', 'No import runs yet.')}</p>
                          ) : (
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="border-b">
                                  <th className="text-left py-1 pr-2">{t('settings.import.history.status', 'Status')}</th>
                                  <th className="text-left py-1 pr-2">{t('settings.import.history.started', 'Started')}</th>
                                  <th className="text-left py-1 pr-2">{t('settings.import.history.completed', 'Completed')}</th>
                                  <th className="text-left py-1 pr-2"></th>
                                </tr>
                              </thead>
                              <tbody>
                                {defTasks.map((task) => (
                                  <tr key={task.id} className="border-b last:border-0">
                                    <td className="py-1 pr-2">
                                      <span className={`text-xs font-medium ${
                                        task.current_status === 'SUCCESS' ? 'text-green-600' :
                                        task.current_status === 'FAILED' ? 'text-red-600' : ''
                                       }`}>{importStatusLabel(t, task.current_status)}</span>
                                    </td>
                                    <td className="py-1 pr-2 text-muted-foreground">{formatDate(task.started_at)}</td>
                                    <td className="py-1 pr-2 text-muted-foreground">{formatDate(task.completed_at)}</td>
                                    <td className="py-1 pr-2">
                                      <Button type="button" variant="outline" size="sm"
                                        onClick={() => setDetailsTask(task)}
                                        data-testid={`import-run-details-btn-${task.id}`}>
                                         {t('common.details', 'Details')}
                                      </Button>
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
              <h2 className="text-lg font-semibold mb-3" data-testid="import-def-modal">
                 {editingDef ? t('settings.import.edit', 'Edit Import Definition') : t('settings.import.create', 'Create Import Definition')}
              </h2>
              <form onSubmit={handleSubmit} className="space-y-3">
                <div>
                   <label className="text-sm font-medium">{t('common.name', 'Name')}</label>
                  <input type="text" value={formName}
                    onChange={(e) => setFormName(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                     placeholder={t('settings.import.name_placeholder', 'Import definition name')} required />
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
                     <label className="text-sm font-medium">{t('settings.import.object_type', 'Object Type')}</label>
                    <select value={formObjectTypeId}
                      onChange={(e) => { setFormObjectTypeId(e.target.value); setFormFieldMappings([]); setFormIdentityAttrId('') }}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                      disabled={!!editingDef || !formSchemaId} required>
                       <option value="">{t('settings.import.select_object_type', 'Select object type...')}</option>
                      {objectTypes.map((ot) => (
                        <option key={ot.id} value={ot.id}>{ot.name}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-sm font-medium inline-flex items-center gap-1">
                       {t('settings.import.identity_attribute', 'Identity Attribute')}
                      <HelpCircle
                        size={14}
                        className="text-muted-foreground"
                         title={t('settings.import.identity_attribute_title', 'Used to match CSV rows against existing Objects. Required for the import to run.')}
                      />
                    </label>
                    <select value={formIdentityAttrId}
                      onChange={(e) => setFormIdentityAttrId(e.target.value)}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                      disabled={!formObjectTypeId}>
                       <option value="">{t('settings.import.select_identity_attribute', 'Select identity attribute...')}</option>
                      {attributes.map((attr) => (
                        <option key={attr.id} value={attr.id}>{attr.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="rounded-lg border">
                  <div className="border-b border-border px-3">
                    <nav className="flex gap-4">
                       {PHASE231_IMPORT_TABS.map(({ value: tab, key, defaultText }) => (
                        <button
                          key={tab}
                          type="button"
                          onClick={() => setFormActiveTab(tab)}
                          className={`py-2 text-sm font-medium transition-colors ${
                            formActiveTab === tab
                              ? 'border-b-2 border-primary text-foreground'
                              : 'text-muted-foreground hover:text-foreground'
                          }`}
                        >
                            {t(key, defaultText)}
                        </button>
                      ))}
                    </nav>
                  </div>
                  <div className="p-3">
                    {formActiveTab === 'Field Mappings' && (
                      <div className="space-y-2">
                        {formFieldMappings.length === 0 && (
                          <p className="text-xs text-muted-foreground">
                             {t('settings.import.mapping.empty', 'No fields mapped yet — the import will have nothing to write until at least one is added.')}
                          </p>
                        )}
                        {formFieldMappings.length > 0 && (
                          <div className="max-h-[132px] overflow-y-auto space-y-2 pr-1">
                            {formFieldMappings.map((mapping, index) => (
                              <div key={index} className="flex items-center gap-2">
                                <input type="text" value={mapping.csv_column}
                                  onChange={(e) => handleMappingChange(index, 'csv_column', e.target.value)}
                                  className="flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                                   placeholder={t('settings.import.csv_column_placeholder', 'CSV column name')} />
                                <select value={mapping.attr_id}
                                  onChange={(e) => handleMappingChange(index, 'attr_id', e.target.value)}
                                  className="flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm">
                                   <option value="">{t('settings.import.select_attribute', 'Select attribute...')}</option>
                                  {attributes.map((attr) => (
                                    <option key={attr.id} value={attr.id}>{attr.name}</option>
                                  ))}
                                </select>
                                <Button type="button" variant="outline" size="sm"
                                   onClick={() => handleRemoveMapping(index)}>{t('common.remove', 'Remove')}</Button>
                              </div>
                            ))}
                          </div>
                        )}
                        <div className="flex gap-2">
                          <Button type="button" variant="outline" size="sm" onClick={handleAddMapping}>
                             {t('settings.import.add_mapping', '+ Add mapping')}
                          </Button>
                          <Button type="button" variant="outline" size="sm" onClick={handleAddAllMappings}
                            disabled={attributes.length === 0}>
                             {t('settings.import.add_all', '+ Add all')}
                          </Button>
                        </div>
                      </div>
                    )}
                    {formActiveTab === 'Settings' && (
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                           <label className="text-sm font-medium">{t('settings.import.value_separator', 'Value Separator')}</label>
                          <input type="text" value={formValueSeparator}
                            onChange={(e) => setFormValueSeparator(e.target.value)}
                            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                             placeholder={t('settings.import.separator_placeholder', ';')} />
                        </div>
                        <div>
                           <label className="text-sm font-medium">{t('settings.import.encoding', 'Encoding')}</label>
                          <select value={formEncoding}
                            onChange={(e) => setFormEncoding(e.target.value)}
                            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm">
                             <option value="utf-8">{t('settings.import.encoding.utf8', 'UTF-8')}</option>
                             <option value="iso-8859-1">{t('settings.import.encoding.iso88591', 'ISO-8859-1')}</option>
                             <option value="windows-1252">{t('settings.import.encoding.windows1252', 'Windows-1252')}</option>
                          </select>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center justify-between gap-2 pt-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={formIsPermanent}
                      onChange={(e) => setFormIsPermanent(e.target.checked)}
                      className="rounded border-input bg-background dark:bg-gray-800"
                      data-testid="is-permanent-checkbox" />
                     {t('settings.import.protect_deletion', 'Protect from deletion (used by a Schedule)')}
                  </label>
                  <div className="flex gap-2">
                    <Button type="button" variant="outline" size="sm"
                       onClick={() => { setShowCreateModal(false); setEditingDef(null) }}
                       disabled={submitting}>{t('common.cancel', 'Cancel')}</Button>
                    <Button type="submit" variant="primary" size="sm"
                      disabled={submitting || !formName.trim() || !formObjectTypeId || !formSourceId}
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
          <TaskRunDetailsModal task={detailsTask} kind="import" onClose={() => setDetailsTask(null)} />
        )}
      </div>
    </ErrorBoundary>
  )
}
