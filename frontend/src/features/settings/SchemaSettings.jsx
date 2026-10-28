import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { keepPreviousData } from '@tanstack/react-query'
import { useSchema, useUpdateSchema, useDeleteSchema } from '../../shared/hooks/use-schemas'
import { useSource } from '../../shared/hooks/use-sources'
import { useObjectTypes, useCreateObjectType, useDeleteObjectType } from '../../shared/hooks/use-object-types'
import TemplateTab from './TemplateTab'
import SchemaLanguagesTab from './SchemaLanguagesTab'
import TranslationPanel from '../../shared/components/TranslationPanel'
import { useStatuses, useCreateStatus, useDeleteStatus } from '../../shared/hooks/use-other-resources'
import { StatusBadge } from '../../shared/components/StatusBadge'
import {
  useReferenceTypes,
  useCreateReferenceType,
  useUpdateReferenceType,
  useDeleteReferenceType,
  useIcons,
  useCreateIcon,
  useDeleteIcon,
} from '../../shared/hooks/use-other-resources'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PluginSectionRegion } from '../../shared/plugin-runtime/PluginSections'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import SlidersHorizontal from 'lucide-react/dist/esm/icons/sliders-horizontal'
import PermissionsDialog from '../permissions/PermissionsDialog'
import { Can } from '../../shared/components/Can'
import { PaginationBar } from '../../shared/components/PaginationBar'
import SaveTemplateDialog from '../wizard/SaveTemplateDialog'
import { useCreateSchemaTemplate } from '../../shared/hooks/use-create-schema-template'
import { retireTranslationJobPointers } from '../../shared/lib/retire-translation-job-pointers'
import { showToast } from '../../shared/components/Toast'
import api from '../../shared/hooks/use-api'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

// D-04 / UI-SPEC: Object Types first — the central schema content — followed
// by the remaining supporting-configuration tabs in this exact order.
const TABS = ['object_types', 'status', 'languages', 'references', 'icons', 'template']

function tabLabel(tab, t) {
  switch (tab) {
    case 'status': return t('settings.schema.tab.status', 'Status')
    case 'languages': return t('settings.schema.tab.languages', 'Languages')
    case 'references': return t('settings.schema.tab.references', 'References')
    case 'icons': return t('settings.schema.tab.icons', 'Icons')
    case 'object_types': return t('settings.schema.tab.object_types', 'Object Types')
    default: return t('settings.schema.tab.template', 'Template')
  }
}

function objectTypesTitle(count, t) {
  return selectPluralKey('settings.schema.object_types_title', count) === 'settings.schema.object_types_title_one'
    ? interpolateDefault(t('settings.schema.object_types_title_one', 'Object Types ({{count}})'), { count })
    : interpolateDefault(t('settings.schema.object_types_title_other', 'Object Types ({{count}})'), { count })
}

function statusFilterSummary(count, t) {
  return selectPluralKey('settings.schema.status_filter_summary', count) === 'settings.schema.status_filter_summary_one'
    ? interpolateDefault(t('settings.schema.status_filter_summary_one', 'Status filter — {{count}} status type(s) configured for this schema'), { count })
    : interpolateDefault(t('settings.schema.status_filter_summary_other', 'Status filter — {{count}} status type(s) configured for this schema'), { count })
}

export default function SchemaSettingsPage() {
  const { t } = useTranslation()
  const { schemaId } = useParams()
  const navigate = useNavigate()
  const { data: schema, isLoading: schemaLoading } = useSchema(schemaId)
  // D-04: Object Types is the default active tab.
  const [activeTab, setActiveTab] = useState('object_types')
  const [permDialogOpen, setPermDialogOpen] = useState(false)

  const updateSchema = useUpdateSchema()
  const deleteSchema = useDeleteSchema()
  const saveTemplate = useCreateSchemaTemplate()

  const [isSaveTemplateOpen, setIsSaveTemplateOpen] = useState(false)
  const [isSavingTemplate, setIsSavingTemplate] = useState(false)
  const [saveTemplateError, setSaveTemplateError] = useState(null)

  // D-16/T-307-18: the schema host lifecycle retires the old Core
  // translate-job-* localStorage pointer family once. The ai-translation plugin
  // re-finds its run from the Core task log, so the stale pointer must go; no
  // unrelated key is touched.
  useEffect(() => {
    retireTranslationJobPointers()
  }, [])

  if (schemaLoading) return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span className="h-4 w-20 rounded bg-muted animate-skeleton" />
        <span>/</span>
        <span className="h-4 w-32 rounded bg-muted animate-skeleton" />
      </div>
      <TableSkeleton rows={4} cols={1} />
    </div>
  )
   if (!schema) return <div className="text-destructive p-6">{t('settings.schemas.not_found', 'Schema not found')}</div>

  const handleDelete = async () => {
     if (await confirmDialog({ message: t('settings.schemas.confirm_delete', 'Delete schema?'), danger: true })) {
      deleteSchema.mutate(schema.id, {
        onSuccess: () => {
           showToast(interpolateDefault(t('toasts.schemas.deleted', 'Schema "{{name}}" deleted.'), { name: schema.name }), { type: 'success' })
          navigate('/configuration/sources')
        },
        onError: (err) => {
           // 290 D-15: a 409 means a member ObjectType is still externally
           // managed — detaching in the connector is the way out.
           showToast(
             err?.response?.status === 409
               ? t('common.delete_externally_managed', 'Externally managed — detach in the connector before deleting.')
               : t('toasts.schemas.delete_failed', 'Failed to delete schema.'),
             { type: 'error' }
           )
        },
      })
    }
  }

  const handleSaveAsTemplate = async ({ name, description }) => {
    setSaveTemplateError(null)
    setIsSavingTemplate(true)
    try {
      const templateData = await api.get(`/schemas/${schemaId}/template-data`).then(r => r.data)
      await saveTemplate.mutateAsync({
        name: name.trim(),
        description: description?.trim() || null,
        category: null,
        template_data: templateData,
      })
       showToast(interpolateDefault(t('toasts.templates.saved', 'Template "{{name}}" saved.'), { name: name.trim() }), { type: 'success', duration: 4000 })
      setIsSaveTemplateOpen(false)
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || 'Unknown error'
      setSaveTemplateError(typeof detail === 'string' ? detail : JSON.stringify(detail))
    } finally {
      setIsSavingTemplate(false)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      {/* D-03 (UX-07): generic Breadcrumb — deterministic Configuration →
          Schemas overview chain with the loaded schema name as current page
          (no raw-ID fallback) */}
      <Breadcrumb
        items={[
          { label: t('nav.configuration', 'Configuration'), href: '/configuration' },
          { label: t('settings.schemas.title', 'Schemas'), href: '/configuration/schemas' },
          { label: schema.name },
        ]}
      />
      {/* D-01/D-02/D-03: informative Overview surface replacing the sparse header */}
      <SchemaOverview
        schema={schema}
        schemaId={schemaId}
        updateSchema={updateSchema}
        onOpenPermissions={() => setPermDialogOpen(true)}
        onOpenSaveTemplate={() => setIsSaveTemplateOpen(true)}
        onDelete={handleDelete}
      />

      {/* Tabs */}
      <div className="rounded-lg border">
        <div className="border-b border-border px-6 overflow-x-auto">
          <nav className="flex gap-6" role="tablist" aria-label={t('settings.schema.tabs_label', 'Schema configuration sections')}>
            {TABS.map((tab) => (
              <button
                key={tab}
                id={`schema-tab-${tab}`}
                role="tab"
                aria-selected={activeTab === tab}
                aria-controls={`schema-tabpanel-${tab}`}
                tabIndex={activeTab === tab ? 0 : -1}
                onClick={() => setActiveTab(tab)}
                data-testid={`schema-tab-${tab}`}
                className={`py-3 whitespace-nowrap text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
                  activeTab === tab
                    ? 'border-b-2 border-primary text-foreground'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                 {tabLabel(tab, t)}
              </button>
            ))}
          </nav>
        </div>

        {/* Tab Content */}
        <div
          className="p-6"
          role="tabpanel"
          id={`schema-tabpanel-${activeTab}`}
          aria-labelledby={`schema-tab-${activeTab}`}
        >
           {activeTab === 'object_types' && <ObjectTypesTab schemaId={schemaId} />}
           {activeTab === 'status' && <StatusTab schemaId={schemaId} schema={schema} />}
           {activeTab === 'languages' && (
            <SchemaLanguagesTab schemaId={schemaId} schema={schema} />
          )}
           {activeTab === 'references' && <ReferencesTab schemaId={schemaId} sourceId={schema.source_id} schema={schema} />}
           {activeTab === 'icons' && <IconsTab schemaId={schemaId} />}
           {activeTab === 'template' && <TemplateTab schema={schema} />}
        </div>
      </div>

      {/* D-01: plugin sections live after the tabbed card and outside every tab
          panel, so they are visible whichever tab is active; no region at zero
          contributions keeps this page pixel-identical to today. */}
      <PluginSectionRegion
        family="schemaSettingsSections"
        headingLevel="h2"
        gapClass="space-y-6"
        componentProps={{ schema }}
        presenceContext={{ schemaId }}
      />

      <SaveTemplateDialog
        isOpen={isSaveTemplateOpen}
        onClose={() => { setIsSaveTemplateOpen(false); setSaveTemplateError(null) }}
        onSave={handleSaveAsTemplate}
        isSaving={isSavingTemplate}
        error={saveTemplateError}
      />
      <PermissionsDialog
        entityType="schema"
        recordId={schemaId}
        open={permDialogOpen}
        onClose={() => setPermDialogOpen(false)}
      />
    </div>
    </ErrorBoundary>
  )
}

/* ─── Schema Overview (D-01..D-04): identity, source, native key generator
   (with Phase-246 rename confirmation), API-derived Object Type total, and
   secondary/destructive actions ─── */
function SchemaOverview({ schema, schemaId, updateSchema, onOpenPermissions, onOpenSaveTemplate, onDelete }) {
  const { t } = useTranslation()
  const { data: source, isLoading: sourceLoading } = useSource(schema.source_id)
  // Reuses the Object Types tab's own paginated query contract (includeTotal)
  // — the API total is the single source of truth, never objectTypes.length.
  const { data: otData } = useObjectTypes(schemaId, 0, 1, {
    includeTotal: true,
    placeholderData: keepPreviousData,
  })
  const objectTypeTotal = otData?.total

  const [editingName, setEditingName] = useState(false)
  const [name, setName] = useState('')

  const [editingKeygen, setEditingKeygen] = useState(false)
  const [keygenValue, setKeygenValue] = useState('')
  const [keygenSaving, setKeygenSaving] = useState(false)
  const [keygenError, setKeygenError] = useState(null)

  const handleRenameSubmit = async (e) => {
    e.preventDefault()
    await updateSchema.mutateAsync({ id: schemaId, name })
    setEditingName(false)
  }

  const handleStartKeygenEdit = () => {
    setKeygenValue(schema.keygen || '')
    setKeygenError(null)
    setEditingKeygen(true)
  }

  // Phase 246 D-12: renaming keygen regenerates native object/attribute keys
  // (Jira-synced keys are left untouched) — the warning must be shown and
  // explicitly confirmed before the PATCH is sent.
  const handleSaveKeygen = async () => {
    const warning = t(
      'settings.schema.overview.keygen_warning',
      'Changing the key generator recreates generated keys for native objects and attributes. Keys in connected external systems are not changed.'
    )
    if (!(await confirmDialog({ message: warning, danger: true }))) return
    setKeygenError(null)
    setKeygenSaving(true)
    try {
      await updateSchema.mutateAsync({ id: schemaId, keygen: keygenValue })
      setEditingKeygen(false)
    } catch (err) {
      const detail = err?.response?.data?.detail
      setKeygenError(typeof detail === 'string' ? detail : t('common.error_generic', 'Something went wrong. Please try again.'))
    } finally {
      setKeygenSaving(false)
    }
  }

  return (
    <div className="rounded-lg border p-6 space-y-4" data-testid="schema-overview">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap min-w-0">
          <h1 className="text-2xl font-bold tracking-tight break-words">{schema.name}</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Can resource="roles" action="manage">
            <Button type="button" variant="outline" size="sm" onClick={onOpenPermissions} data-testid="schema-permissions-button">
              {t('common.permissions', 'Permissions')}
            </Button>
          </Can>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onOpenSaveTemplate}
            leftIcon="save"
            data-testid="schema-save-template-button"
          >
            {t('settings.schemas.save_as_template', 'Save as Template')}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => { setName(schema.name); setEditingName(true) }}
            data-testid="schema-edit-button"
          >
            {t('common.edit', 'Edit')}
          </Button>
        </div>
      </div>

      {editingName && (
        <form onSubmit={handleRenameSubmit} className="space-y-3 border-t pt-4">
          <div className="space-y-1.5">
            <label htmlFor="schema-name" className="text-sm font-medium">{t('common.name', 'Name')}</label>
            <input
              id="schema-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" size="sm" data-testid="schema-rename-save">{t('common.save', 'Save')}</Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setEditingName(false)} data-testid="schema-rename-cancel">{t('common.cancel', 'Cancel')}</Button>
          </div>
        </form>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 text-sm border-t pt-4">
        <div>
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            {t('settings.schema.overview.source', 'Source')}
          </div>
          <div className="mt-1" data-testid="schema-overview-source">
            {sourceLoading ? t('common.loading', 'Loading...') : (source?.name || t('common.empty_value', '—'))}
          </div>
        </div>
        <div>
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            {t('settings.schema.overview.keygen', 'Native key generator')}
          </div>
          {editingKeygen ? (
            <div className="mt-1 space-y-2">
              <input
                value={keygenValue}
                onChange={(e) => setKeygenValue(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-2 py-1 text-sm text-foreground"
                data-testid="schema-keygen-input"
                aria-label={t('settings.schema.overview.keygen', 'Native key generator')}
              />
              {keygenError && (
                <p className="text-xs text-destructive" data-testid="schema-keygen-error">{keygenError}</p>
              )}
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="primary"
                  size="sm"
                  loading={keygenSaving}
                  onClick={handleSaveKeygen}
                  data-testid="schema-keygen-save"
                >
                  {t('common.save', 'Save')}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setEditingKeygen(false)}
                  data-testid="schema-keygen-cancel"
                >
                  {t('common.cancel', 'Cancel')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-1 flex items-center gap-2 flex-wrap">
              <span data-testid="schema-overview-keygen">{schema.keygen || t('common.empty_value', '—')}</span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={handleStartKeygenEdit}
                data-testid="schema-edit-keygen-button"
              >
                {t('settings.schema.overview.edit_keygen', 'Edit key generator')}
              </Button>
            </div>
          )}
        </div>
        <div>
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
            {t('settings.schema.overview.object_type_count', 'Object Types')}
          </div>
          <div className="mt-1" data-testid="schema-overview-object-type-count">
            {objectTypeTotal === undefined ? t('common.loading', 'Loading...') : objectTypeTotal}
          </div>
        </div>
      </div>

      <div className="border-t pt-4">
        <div className="rounded-md border border-destructive/30 bg-destructive/5 p-4 flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h4 className="text-sm font-semibold text-destructive">
              {t('settings.schema.overview.destructive_title', 'Delete this schema')}
            </h4>
            <p className="text-xs text-muted-foreground">
              {t('settings.schema.overview.destructive_description', 'This removes the schema and all its object types, objects, and settings. This cannot be undone.')}
            </p>
          </div>
          <Button type="button" variant="destructive" size="sm" onClick={onDelete} data-testid="schema-delete-button">
            {t('common.delete', 'Delete')}
          </Button>
        </div>
      </div>
    </div>
  )
}

/* ─── Status Tab ─── */
function StatusTab({ schemaId, schema }) {
  const { t } = useTranslation()
  const { data: statuses = [], isLoading } = useStatuses(schemaId)
  const createStatus = useCreateStatus()
  const deleteStatus = useDeleteStatus()

  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState('')
  const [newCategory, setNewCategory] = useState('')
  const [statusPanelId, setStatusPanelId] = useState(null)

  const handleCreate = async (e) => {
    e.preventDefault()
    await createStatus.mutateAsync({
      schema_id: schemaId,
      name: newName,
      color: newColor || undefined,
      status_category: newCategory || undefined,
    })
    setNewName(''); setNewColor(''); setNewCategory(''); setShowCreate(false)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
         <h3 className="text-lg font-semibold">{t('settings.schema.statuses_title', 'Statuses')}</h3>
        <Button onClick={() => setShowCreate(true)} variant="primary" size="sm">
           {t('settings.schema.add_status', '+ Add Status')}
        </Button>
      </div>

      {showCreate && (
        <form onSubmit={handleCreate} className="flex flex-wrap gap-2 p-3 border rounded-md bg-muted/30 mb-4">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
             placeholder={t('settings.schema.status_name_placeholder', 'Status name')}
            required
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm flex-1 min-w-[150px]"
          />
          <select
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm"
          >
             <option value="">{t('settings.schema.no_category', 'No category')}</option>
             <option value="Active">{t('common.status.active', 'Active')}</option>
             <option value="Pending">{t('common.status.pending', 'Pending')}</option>
             <option value="Inactive">{t('common.status.inactive', 'Inactive')}</option>
          </select>
          <input
            type="color"
            value={newColor || '#94a3b8'}
            onChange={(e) => setNewColor(e.target.value)}
            className="h-9 w-9 rounded-md border border-input bg-background p-1"
             title={t('settings.schema.color_optional', 'Color (optional)')}
          />
          <Button type="submit" variant="primary" size="sm">
             {t('common.create', 'Create')}
          </Button>
          <Button type="button" onClick={() => setShowCreate(false)} variant="outline" size="sm">
             {t('common.cancel', 'Cancel')}
          </Button>
        </form>
      )}

      {/* Status filter / Constraints section — shown in all states once schema is loaded */}
      {schema && (
        <div className="mb-4 p-3 border rounded-md bg-muted/20">
          <h4 className="text-sm font-semibold mb-2 flex items-center gap-1.5">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M9 9h6" /><path d="M9 12h6" /><path d="M9 15h6" />
            </svg>
             {t('settings.schema.constraints', 'Constraints')}
          </h4>
          {schema.status_filter_config ? (
            <div className="text-sm space-y-1">
              <p className="text-muted-foreground">
                 {statusFilterSummary(schema.status_filter_config.status_type_count || 0, t)}
              </p>
              <p className="text-xs text-muted-foreground">
                 {t('settings.schema.status_filter_description', 'Only these status types are valid for status-type attributes in this schema. The filter configuration is set via the API or by a connector plugin.')}
              </p>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
               {t('settings.schema.no_status_filter', 'No status filter configured — all global status types are valid.')}
            </p>
          )}
        </div>
      )}

      {isLoading ? (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-6 w-32 animate-pulse rounded bg-muted" />
          ))}
        </div>
      ) : statuses.length === 0 ? (
        <p className="text-sm text-muted-foreground">
           {t('settings.schema.no_statuses', 'No statuses defined for this schema yet. Add one above, or import them via a connector plugin.')}
        </p>
      ) : (
        <div className="space-y-2">
          {statuses.map((status) => (
            <div key={status.id} className="flex items-center justify-between py-1 border-b border-border last:border-0">
              <div className="flex items-center gap-2">
                <StatusBadge value={status} />
                {status.status_category && (
                  <span className="text-xs text-muted-foreground">({status.status_category})</span>
                )}
              </div>
              <div className="flex items-center gap-1">
                {schema?.multilingual && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    leftIcon="globe"
                    onClick={() => setStatusPanelId(status.id)}
                    className="text-muted-foreground"
                     aria-label={interpolateDefault(t('settings.schema.translate_status', 'Translate {{name}}'), { name: status.name })}
                     title={t('settings.schema.translate_name', 'Translate name')}
                    data-testid={`lang-globe-status-${status.id}`}
                  />
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  leftIcon="trash-2"
                  onClick={async () => {
                     if (await confirmDialog({ message: interpolateDefault(t('settings.schema.confirm_delete_status', 'Delete status "{{name}}"?'), { name: status.name }), danger: true })) deleteStatus.mutate(status.id)
                  }}
                  className="text-muted-foreground hover:text-destructive"
                    aria-label={interpolateDefault(t('settings.schema.delete_status', 'Delete {{name}}'), { name: status.name })}
                />
              </div>
            </div>
          ))}
        </div>
      )}
      {statusPanelId && (
        <TranslationPanel
          entityType="status"
          entityId={statusPanelId}
          schemaId={schemaId}
          fields={[{ key: 'name' }]}
          fieldValues={{ name: statuses.find(s => s.id === statusPanelId)?.name || '' }}
          open={!!statusPanelId}
          onClose={() => setStatusPanelId(null)}
        />
      )}
    </div>
  )
}

/* ─── References Tab ─── */
function ReferencesTab({ schemaId, sourceId, schema }) {
  const { t } = useTranslation()
  const { data: references = [], isLoading } = useReferenceTypes(0, 100, schemaId)
  const createReference = useCreateReferenceType()
  const updateReference = useUpdateReferenceType()
  const deleteReference = useDeleteReferenceType()

  const [showCreate, setShowCreate] = useState(false)
  const [newRef, setNewRef] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [editingId, setEditingId] = useState(null)
  const [editRef, setEditRef] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [refPanelId, setRefPanelId] = useState(null)

  const handleCreate = async (e) => {
    e.preventDefault()
    await createReference.mutateAsync({ reference: newRef, description: newDesc || undefined, schema_id: schemaId, source_id: sourceId })
    setNewRef(''); setNewDesc(''); setShowCreate(false)
  }

  if (isLoading) return <TableSkeleton rows={3} cols={1} />

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
         <h3 className="text-lg font-semibold">{t('settings.schema.reference_types_title', 'Reference Types')}</h3>
        <Button
          onClick={() => setShowCreate(true)}
          variant="primary"
          size="sm"
        >
           {t('settings.schema.add_reference', '+ Add Reference')}
        </Button>
      </div>

      {showCreate && (
        <form onSubmit={handleCreate} className="flex flex-wrap gap-2 p-3 border rounded-md bg-muted/30 mb-4">
          <input
            value={newRef}
            onChange={(e) => setNewRef(e.target.value)}
             placeholder={t('settings.schema.reference_name_placeholder', 'Reference name')}
            required
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm flex-1 min-w-[150px]"
          />
          <input
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
             placeholder={t('settings.schema.description_optional', 'Description (optional)')}
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm flex-1 min-w-[200px]"
          />
          <Button type="submit" variant="primary" size="sm">
             {t('common.create', 'Create')}
          </Button>
          <Button type="button" onClick={() => setShowCreate(false)} variant="outline" size="sm">
             {t('common.cancel', 'Cancel')}
          </Button>
        </form>
      )}

      {references.length === 0 ? (
        <EmptyState
          icon={SlidersHorizontal}
           title={t('settings.schema.no_references', 'No references')}
           description={t('settings.schema.no_references_description', 'This schema has no referenced schemas.')}
        />
      ) : (
        <div className="space-y-2">
          {references.map((ref) => (
            <div key={ref.id} className="flex items-center justify-between p-3 border rounded-md">
              {editingId === ref.id ? (
                <div className="flex gap-2 flex-1">
                  <input
                    value={editRef}
                    onChange={(e) => setEditRef(e.target.value)}
                    className="rounded-md border border-input bg-background text-foreground px-2 py-1 text-sm flex-1"
                  />
                  <input
                    value={editDesc}
                    onChange={(e) => setEditDesc(e.target.value)}
                    className="rounded-md border border-input bg-background text-foreground px-2 py-1 text-sm flex-1"
                     placeholder={t('common.description', 'Description')}
                  />
                </div>
              ) : (
                <div>
                  <span>{ref.reference}</span>
                  {ref.description && (
                    <span className="text-sm text-muted-foreground ml-2">{ref.description}</span>
                  )}
                </div>
              )}
              <div className="flex gap-2 shrink-0">
                {editingId === ref.id ? (
                  <>
                    <Button
                      onClick={async () => {
                        await updateReference.mutateAsync({ id: ref.id, reference: editRef, description: editDesc || undefined })
                        setEditingId(null)
                      }}
                      variant="primary"
                      size="md"
                    >
                         {t('common.save', 'Save')}
                    </Button>
                       <Button onClick={() => setEditingId(null)} variant="ghost" size="md">
                       {t('common.cancel', 'Cancel')}
                    </Button>
                  </>
                ) : (
                  <>
                    {schema?.multilingual && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        leftIcon="globe"
                        onClick={() => setRefPanelId(ref.id)}
                        className="text-muted-foreground"
                         aria-label={interpolateDefault(t('settings.schema.translate_reference', 'Translate {{name}}'), { name: ref.reference })}
                         title={t('settings.schema.translate_reference_title', 'Translate reference')}
                        data-testid={`lang-globe-ref-${ref.id}`}
                      />
                    )}
                    <Button
                      onClick={() => { setEditingId(ref.id); setEditRef(ref.reference); setEditDesc(ref.description || '') }}
                      variant="outline"
                      size="md"
                    >
                       {t('common.edit', 'Edit')}
                    </Button>
                    <Button
                       onClick={async () => { if (await confirmDialog({ message: t('settings.schema.confirm_delete_reference', 'Delete reference type?'), danger: true })) deleteReference.mutate(ref.id) }}
                      variant="destructive"
                      size="md"
                    >
                       {t('common.delete', 'Delete')}
                    </Button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {refPanelId && (
        <TranslationPanel
          entityType="reference_type"
          entityId={refPanelId}
          schemaId={schemaId}
          fields={[{ key: 'reference' }, { key: 'description' }]}
          fieldValues={{
            reference: references.find(r => r.id === refPanelId)?.reference || '',
            description: references.find(r => r.id === refPanelId)?.description || '',
          }}
          open={!!refPanelId}
          onClose={() => setRefPanelId(null)}
        />
      )}
    </div>
  )
}

/* ─── Icons Tab ─── */
function IconsTab({ schemaId }) {
  const { t } = useTranslation()
  const { data: allIcons = [], isLoading } = useIcons()
  // Filter icons client-side by schemaId since the API returns all icons
  const icons = allIcons.filter((icon) => icon.schema_id === schemaId)
  const createIcon = useCreateIcon()
  const deleteIcon = useDeleteIcon()

  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')

  const handleCreate = async (e) => {
    e.preventDefault()
    await createIcon.mutateAsync({ schema_id: schemaId, name, url })
    setName(''); setUrl(''); setShowCreate(false)
  }

  if (isLoading) return <TableSkeleton rows={3} cols={1} />

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
         <h3 className="text-lg font-semibold">{t('settings.schema.icons_title', 'Icons')}</h3>
        <Button
          onClick={() => setShowCreate(true)}
          variant="primary"
          size="sm"
        >
           {t('settings.schema.add_icon', '+ Add Icon')}
        </Button>
      </div>

      {showCreate && (
        <form onSubmit={handleCreate} className="flex flex-wrap gap-2 p-3 border rounded-md bg-muted/30 mb-4">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
             placeholder={t('settings.schema.icon_name_placeholder', 'Icon name')}
            required
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm flex-1 min-w-[150px]"
          />
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
             placeholder={t('settings.schema.icon_url_placeholder', 'Icon URL')}
            required
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm flex-1 min-w-[250px]"
          />
          <Button type="submit" variant="primary" size="sm">
             {t('common.add', 'Add')}
          </Button>
          <Button type="button" onClick={() => setShowCreate(false)} variant="outline" size="sm">
             {t('common.cancel', 'Cancel')}
          </Button>
        </form>
      )}

      {icons.length === 0 ? (
         <p className="text-sm text-muted-foreground">{t('settings.schema.no_icons', 'No icons assigned to this schema.')}</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {icons.map((icon) => (
            <div key={icon.id} className="flex flex-col items-center gap-2 p-3 border rounded-md">
              {icon.local_name ? (
                <LucideIcon
                  name={icon.local_name}
                  size={40}
                  className="w-10 h-10 text-foreground"
                />
              ) : (
                <img
                  src={icon.url}
                  alt={icon.name}
                  className="w-10 h-10 object-contain"
                  onError={(e) => { e.target.style.display = 'none'; e.target.nextSibling.style.display = 'block' }}
                />
              )}
              <span className="text-xs text-muted-foreground hidden">⚠</span>
              <span className="text-sm truncate max-w-full">{icon.name}</span>
              <Button
                 onClick={async () => { if (await confirmDialog({ message: t('settings.schema.confirm_delete_icon', 'Delete this icon?'), danger: true })) deleteIcon.mutate(icon.id) }}
                variant="destructive"
                size="md"
              >
                 {t('common.delete', 'Delete')}
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ─── Object Types Tab ─── */
function ObjectTypesTab({ schemaId }) {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(t)
  }, [search])

  useEffect(() => {
    setPage(1)
  }, [debouncedSearch])

  const skip = (page - 1) * pageSize
  const limit = pageSize
  const { data: otData, isLoading } = useObjectTypes(schemaId, skip, limit, {
    includeTotal: true,
    extraParams: debouncedSearch ? { name: debouncedSearch } : {},
    placeholderData: keepPreviousData,
  })
  const objectTypes = otData?.items || []
  const total = otData?.total || 0
  const createObjectType = useCreateObjectType()
  const deleteObjectType = useDeleteObjectType()

  const [newOtName, setNewOtName] = useState('')
  const [showCreateOt, setShowCreateOt] = useState(false)

  const handleCreateOt = async (e) => {
    e.preventDefault()
    await createObjectType.mutateAsync({ schema_id: schemaId, name: newOtName })
    setNewOtName('')
    setShowCreateOt(false)
  }

  if (isLoading) return <TableSkeleton rows={6} cols={1} />

  return (
    <div className="space-y-3" data-testid="object-type-list">
      <div className="flex items-center justify-between gap-3 flex-wrap">
         <h2 className="text-lg font-semibold" data-testid="object-types-section">{objectTypesTitle(objectTypes.length, t)}</h2>
        <div className="flex items-center gap-2">
          <TableToolbar
            urlQ={search}
            setQ={setSearch}
            placeholder={t('settings.schema.search_object_types', 'Search object types...')}
            ariaLabel={t('settings.schema.search_object_types_label', 'Search object types')}
            searchTestId="object-type-search-input"
          />
          <Button onClick={() => setShowCreateOt(true)} variant="primary" size="sm">
             {t('settings.schema.new_object_type', '+ New Object Type')}
          </Button>
        </div>
      </div>

      {showCreateOt && (
        <form onSubmit={handleCreateOt} className="flex gap-2">
           <input value={newOtName} onChange={(e) => setNewOtName(e.target.value)} placeholder={t('settings.schema.object_type_name_placeholder', 'Object type name')} required
            className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground" />
           <Button type="submit" variant="primary" size="sm">{t('common.create', 'Create')}</Button>
           <Button type="button" variant="outline" size="sm" onClick={() => setShowCreateOt(false)}>{t('common.cancel', 'Cancel')}</Button>
        </form>
      )}

      {objectTypes.length === 0 ? (
        <EmptyState
          variant="empty"
           title={t('settings.schema.no_object_types', 'No object types')}
            description={debouncedSearch ? interpolateDefault(t('settings.schema.no_object_types_match', 'No object types match "{{name}}".'), { name: debouncedSearch }) : t('settings.schema.no_object_types_description', 'No object types in this schema yet. Create your first one.')}
           buttonLabel={t('settings.schema.create_object_type', 'Create Object Type')}
          onButtonClick={() => setShowCreateOt(true)}
        />
      ) : (
        <Table>
          <thead className="sticky top-0 z-10">
            <tr className="border-b bg-background">
               <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
               <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
            </tr>
          </thead>
          <tbody>
            {objectTypes.map((ot) => (
              <tr key={ot.id} className="border-b last:border-0 hover:bg-muted/30">
                <td className="px-4 py-3">
                  <Link to={`/browse/object-types/${ot.id}`} className="text-primary hover:underline">
                    {ot.name}
                  </Link>
                 </td>
                 <td className="px-4 py-3 text-right">
                   <Button type="button" variant="destructive" size="sm" onClick={async () => { if (await confirmDialog({ message: t('settings.schema.confirm_delete_object_type', 'Delete object type?'), danger: true })) deleteObjectType.mutate(ot.id, { onError: (err) => showToast(err?.response?.status === 409 ? t('common.delete_externally_managed', 'Externally managed — detach in the connector before deleting.') : t('common.error_generic', 'Something went wrong. Please try again.'), { type: 'error' }) }) }}>
                     {t('common.delete', 'Delete')}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <PaginationBar
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
      />
    </div>
  )
}

