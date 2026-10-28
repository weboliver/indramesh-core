import { useState, useCallback, useEffect, useRef } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useSchemas, useCreateSchema } from '../../shared/hooks/use-schemas'
import { useSources } from '../../shared/hooks/use-sources'
import api from '../../shared/hooks/use-api'
import { useActiveSchema } from '../../shared/components/ActiveSchemaProvider'
import { Button } from '../../shared/components/Button'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { showToast } from '../../shared/components/Toast'
import SchemaWizardModal from '../wizard/SchemaWizardModal'
import Layers from 'lucide-react/dist/esm/icons/layers'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

export default function SchemasListPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const sourceId = searchParams.get('sourceId')
  // D-02 standard search adapter (server q surface, manifest #10): URL-synced
  // `q` drives the existing server `name` filter contract, preserves unrelated
  // params (sourceId), and resets page to 1 on change or clear.
  const q = searchParams.get('q') || ''
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize

  // Unfiltered (all schemas) when no sourceId; use `enabled: true` override
  // because useSchemas has enabled: !!sourceId by default
  const { data: schemasData, isLoading, isError, refetch } = useSchemas(
    sourceId, skip, limit,
    { includeTotal: true, enabled: true, extraParams: q ? { name: q } : {} }
  )
  const schemas = schemasData?.items || []
  const total = schemasData?.total || 0

  // Fetch all sources to resolve source names in the table, and to populate the Source filter dropdown
  const { data: sources = [] } = useSources(0, 1000)

  const { activeSourceId, setActiveSchema } = useActiveSchema()
  const navigate = useNavigate()

  // Make the schema active (source + schema together) and open its first root
  // object type, like a schema switch in the Sidebar; an empty schema (or a
  // failed lookup) lands on the browse root instead.
  const viewSchema = async (schema) => {
    setActiveSchema(schema.source_id, schema.id)
    let target = '/browse/object-types'
    try {
      const { data } = await api.get('/object-types/', { params: { schema_id: schema.id, skip: 0, limit: 50 } })
      const objectTypes = Array.isArray(data) ? data : []
      const first = objectTypes.find((ot) => !ot.parent_id) || objectTypes[0]
      if (first) target = `/browse/object-types/${first.id}`
    } catch {
      // Fall back to the browse root; the Sidebar still shows the schema's tree.
    }
    navigate(target)
  }

  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const setSourceFilter = (value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('sourceId', value)
      else next.delete('sourceId')
      next.set('page', '1')
      return next
    })
  }

  const [wizardOpen, setWizardOpen] = useState(false)
  const [showCreateSchema, setShowCreateSchema] = useState(false)
  const [newSchemaName, setNewSchemaName] = useState('')
  const [scratchSourceId, setScratchSourceId] = useState('')
  const [createSchemaError, setCreateSchemaError] = useState(null)
  const createSchema = useCreateSchema()

  // Pre-fill the scratch form's Source picker with the app-global active
  // Source, but only in the no-URL-source branch (D-01/D-02): the URL sourceId
  // filter stays authoritative and keeps hiding the picker. The fallback is
  // validated against the loaded source list (D-03). A once-per-open guard
  // (scratchPrefillRef) prevents the effect from re-filling a deliberately
  // cleared picker on later sources refetches, while the user-edit ref keeps
  // a selection the user made (even to empty) authoritative over the default
  // (WR-01).
  const scratchPrefillRef = useRef(false)
  const scratchEditedRef = useRef(false)
  useEffect(() => {
    if (showCreateSchema) {
      scratchPrefillRef.current = false
      scratchEditedRef.current = false
    }
  }, [showCreateSchema])

  useEffect(() => {
    if (!showCreateSchema || sourceId || !activeSourceId) return
    if (scratchPrefillRef.current || scratchEditedRef.current) return
    if (!sources.some(s => s.id === activeSourceId)) return
    scratchPrefillRef.current = true
    setScratchSourceId(activeSourceId)
  }, [showCreateSchema, sourceId, sources, activeSourceId])

  const handleCreateSchemaFromScratch = async (e) => {
    e.preventDefault()
    setCreateSchemaError(null)
    const targetSourceId = sourceId || scratchSourceId
    try {
      const result = await createSchema.mutateAsync({
        source_id: targetSourceId,
        name: newSchemaName.trim(),
      })
       showToast(interpolateDefault(t('toasts.schemas.created', 'Schema "{{name}}" created.'), { name: result.name }), { type: 'success' })
      setNewSchemaName('')
      setScratchSourceId('')
      setShowCreateSchema(false)
    } catch (err) {
       const detail = err?.response?.data?.detail || err?.message || t('settings.schemas.unknown_error', 'Unknown error')
      setCreateSchemaError(typeof detail === 'string' ? detail : JSON.stringify(detail))
    }
  }

  const getSourceName = (sourceId) => {
    return sources.find(s => s.id === sourceId)?.name || sourceId
  }

  const clearFilter = () => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('sourceId')
      next.set('page', '1')
      return next
    })
  }

  const clearAllFilters = () => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('sourceId')
      next.delete('q')
      next.set('page', '1')
      return next
    })
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="schemas-list-page">
         <PageHeader title={t('settings.schemas.title', 'Schemas')} description={t('settings.schemas.description', 'Manage asset schemas and their object type definitions.')} icon="layers">
          <Button onClick={() => setWizardOpen(true)} variant="primary" size="sm" data-testid="schema-new-btn">
             {t('settings.schemas.new', '+ New Schema')}
          </Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.schemas.search_placeholder', 'Search Schemas...')}
          ariaLabel={t('settings.schemas.search_label', 'Search Schemas')}
          filters={
            <select
              value={sourceId || ''}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground min-w-[160px]"
              data-testid="schemas-filter-source"
            >
              <option value="">{t('settings.schemas.all_sources', 'All sources')}</option>
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{source.name}</option>
              ))}
            </select>
          }
        />

        {/* Filter indicator — shown only when sourceId is active */}
        {sourceId && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="schemas-filter-indicator">
            <span>
               {t('settings.schemas.filtered_by_source', 'Showing schemas for source:')} <strong>{getSourceName(sourceId)}</strong>
            </span>
            <Button variant="outline" size="sm" onClick={clearFilter}>
               {t('common.clear_filter', 'Clear filter')}
            </Button>
          </div>
        )}

        {isLoading ? (
          <TableSkeleton rows={5} cols={3} />
        ) : isError ? (
          <EmptyState
            variant="error"
             title={t('settings.schemas.load_failed_title', 'Failed to load schemas')}
             description={t('settings.schemas.load_failed_description', 'Failed to load schemas. The server may be unavailable.')}
             buttonLabel={t('common.retry', 'Retry')}
            onButtonClick={() => refetch()}
          />
        ) : schemas.length === 0 ? (
          (sourceId || q) ? (
            <EmptyState
              variant="no-results"
              title={t('settings.schemas.no_results_title', 'No matching schemas')}
              description={t('settings.schemas.no_results_description', 'Try adjusting your search or filters.')}
              onButtonClick={clearAllFilters}
            />
          ) : (
            <EmptyState
              variant="empty"
              icon={Layers}
               title={t('settings.schemas.empty_title', 'No schemas yet')}
               description={t('settings.schemas.empty_description', 'No schemas configured. Schemas are created through sources.')}
            />
          )
        ) : (
          <div className="space-y-3">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.source', 'Source')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.key', 'Key')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {schemas.map((schema) => (
                  <tr key={schema.id} className="border-b last:border-0 hover:bg-muted/30" data-testid={`schema-row-${schema.id}`}>
                    <td className="px-4 py-3 text-sm font-medium">{schema.name}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      {getSourceName(schema.source_id)}
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{schema.key || '-'}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          leftIcon="eye"
                          onClick={() => viewSchema(schema)}
                          data-testid={`schema-view-${schema.id}`}
                        >
                          {t('settings.schemas.view', 'View')}
                        </Button>
                        <Button asChild variant="outline" size="sm">
                          <Link to={`/configuration/schemas/${schema.id}`}>
                             {t('common.edit', 'Edit')}
                          </Link>
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <PaginationBar
              page={page}
              pageSize={pageSize}
              total={total}
              onPageChange={setPage}
              onPageSizeChange={setPageSize}
            />
          </div>
        )}

        <SchemaWizardModal
          isOpen={wizardOpen}
          onClose={() => setWizardOpen(false)}
          sourceId={sourceId}
          onSelectStartFromScratch={() => setShowCreateSchema(true)}
        />

        {showCreateSchema && (
          <form onSubmit={handleCreateSchemaFromScratch} className="flex flex-wrap gap-2 p-3 border rounded-md bg-muted/30" data-testid="create-schema-form">
            {!sourceId && (
              <select
                value={scratchSourceId}
                onChange={(e) => {
                  scratchEditedRef.current = true
                  setScratchSourceId(e.target.value)
                }}
                required
                className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground min-w-[160px]"
                data-testid="create-schema-source-select"
              >
                 <option value="">{t('settings.schemas.select_source', 'Select a source...')}</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>{source.name}</option>
                ))}
              </select>
            )}
            <input
              value={newSchemaName}
              onChange={(e) => setNewSchemaName(e.target.value)}
               placeholder={t('settings.schemas.name_placeholder', 'Schema name')}
              required
              autoFocus
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground flex-1 min-w-[200px]"
            />
            <Button type="submit" variant="primary" size="sm" disabled={createSchema.isPending}>
               {createSchema.isPending ? t('common.creating', 'Creating...') : t('common.create', 'Create')}
            </Button>
            <Button type="button" variant="outline" size="sm"
                    onClick={() => { setShowCreateSchema(false); setNewSchemaName(''); setScratchSourceId(''); setCreateSchemaError(null) }}>
               {t('common.cancel', 'Cancel')}
            </Button>
          </form>
        )}
        {createSchemaError && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive" role="alert" data-testid="create-schema-error">
             {interpolateDefault(t('settings.schemas.creation_failed', 'Schema creation failed: {{detail}}'), { detail: createSchemaError })}
          </div>
        )}
      </div>
    </ErrorBoundary>
  )
}
