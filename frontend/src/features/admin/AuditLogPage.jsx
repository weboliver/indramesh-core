import { useState, useMemo, useCallback, Fragment } from 'react'
import { useSearchParams } from 'react-router-dom'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { useAuditLogs } from '../../shared/hooks/use-audit-logs'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { AUDIT_ENTITY_TYPES } from '../../shared/constants/audit-entity-types'
import AdminAuditConfigSection from './AdminAuditConfigSection'
import { Can } from '../../shared/components/Can'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { filterAuditEntries } from './audit-search'

const ENTITY_TYPE_OPTIONS = [
  { value: '' },
  ...AUDIT_ENTITY_TYPES,
]

const DATE_PRESETS = [
  { value: '24h' },
  { value: '7d' },
  { value: '30d' },
  { value: 'custom' },
]

const ACTION_BADGE_STYLES = {
  CREATE: 'bg-green-500/10 text-green-600 dark:text-green-400',
  UPDATE: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  DELETE: 'bg-red-500/10 text-red-600 dark:text-red-400',
}

function entityTypeLabel(type, t) {
  switch (type) {
    case 'sources': return t('admin.audit.entity.sources', 'Sources')
    case 'schemas': return t('admin.audit.entity.schemas', 'Schemas')
    case 'object_types': return t('admin.audit.entity.object_types', 'Object Types')
    case 'objects': return t('admin.audit.entity.objects', 'Objects')
    case 'object_values': return t('admin.audit.entity.object_values', 'Object Values')
    case 'object_refs': return t('admin.audit.entity.object_refs', 'Object Refs')
    case 'attribute_types': return t('admin.audit.entity.attribute_types', 'Attribute Types')
    case 'reference_types': return t('admin.audit.entity.reference_types', 'Reference Types')
    case 'statuses': return t('admin.audit.entity.statuses', 'Statuses')
    case 'icons': return t('admin.audit.entity.icons', 'Icons')
    case 'targets': return t('admin.audit.entity.targets', 'Targets')
    case 'service_accounts': return t('admin.audit.entity.service_accounts', 'Service Accounts')
    case 'schema_templates': return t('admin.audit.entity.schema_templates', 'Schema Templates')
    case 'import_definitions': return t('admin.audit.entity.import_definitions', 'Import Definitions')
    case 'export_definitions': return t('admin.audit.entity.export_definitions', 'Export Definitions')
    case 'tasks': return t('admin.audit.entity.tasks', 'Tasks')
    case 'audit_log_config': return t('admin.audit.entity.audit_log_config', 'Audit Log Config')
    case 'settings': return t('admin.audit.entity.settings', 'Settings')
    case 'permissions': return t('admin.audit.entity.permissions', 'Permissions')
    case 'users': return t('admin.audit.entity.users', 'Users')
    case 'roles': return t('admin.audit.entity.roles', 'Roles')
    default: return type
  }
}

function datePresetLabel(value, t) {
  switch (value) {
    case '24h': return t('admin.audit_log.last_24h', 'Last 24h')
    case '7d': return t('admin.audit_log.last_7d', 'Last 7d')
    case '30d': return t('admin.audit_log.last_30d', 'Last 30d')
    default: return t('admin.audit_log.custom_date', 'Custom')
  }
}

function moreFieldsLabel(count, t) {
  return selectPluralKey('admin.audit_log.more_fields', count) === 'admin.audit_log.more_fields_one'
    ? interpolateDefault(t('admin.audit_log.more_fields_one', '+{{count}} more field'), { count })
    : interpolateDefault(t('admin.audit_log.more_fields_other', '+{{count}} more fields'), { count })
}

export default function AuditLogPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()

  // Filter state from URL params
  const selectedEntityType = searchParams.get('entity_type') || ''
  const datePreset = searchParams.get('date_preset') || ''
  const customStartDate = searchParams.get('start_date') || ''
  const customEndDate = searchParams.get('end_date') || ''
  const actorQuery = searchParams.get('actor_q') || ''
  // D-02 standard search adapter (surface 04, local loaded-list filter): the
  // audit endpoint has no server `q`, so the standard control narrows the
  // already-loaded current page in memory, combining with entity/date/actor.
  const q = searchParams.get('q') || ''

  // Accordion state
  const [expandedId, setExpandedId] = useState(null)
  const [expandedExtraFields, setExpandedExtraFields] = useState(new Set())

  // Compute dates from presets
  const computedDates = useMemo(() => {
    const now = new Date()
    if (datePreset === '24h') return { start_date: new Date(now - 86400000).toISOString(), end_date: undefined }
    if (datePreset === '7d') return { start_date: new Date(now - 7 * 86400000).toISOString(), end_date: undefined }
    if (datePreset === '30d') return { start_date: new Date(now - 30 * 86400000).toISOString(), end_date: undefined }
    if (datePreset === 'custom') return { start_date: customStartDate || undefined, end_date: customEndDate || undefined }
    return { start_date: undefined, end_date: undefined }
  }, [datePreset, customStartDate, customEndDate])

  // Pagination
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()

  // Timestamp formatting
  const formatDate = useFormatDate()

  // Data fetching
  const { data, isLoading, isError, refetch } = useAuditLogs({
    entity_type: selectedEntityType || undefined,
    start_date: computedDates.start_date,
    end_date: computedDates.end_date,
    actor_q: actorQuery || undefined,
    page,
    page_size: pageSize,
  })

  const total = data?.total || 0
  const hasFilters = selectedEntityType || datePreset || actorQuery || q

  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  // Local loaded-list filter: narrows the server-paginated current page by q,
  // combined with (never replacing) the entity/date/actor server filters above.
  const displayedItems = useMemo(
    () => filterAuditEntries(data?.items || [], q, (type) => entityTypeLabel(type, t)),
    [data, q, t]
  )

  // Filter update helpers
  const updateFilter = useCallback((key, value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const handleDatePreset = useCallback((preset) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (preset) next.set('date_preset', preset)
      else next.delete('date_preset')
      if (preset !== 'custom') {
        next.delete('start_date')
        next.delete('end_date')
      }
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  // Accordion handlers
  const handleToggleExpand = useCallback((id) => {
    setExpandedId(prev => prev === id ? null : id)
  }, [])

  const handleToggleExtraFields = useCallback((id) => {
    setExpandedExtraFields(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleRowKeyDown = useCallback((e, id) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      handleToggleExpand(id)
    }
  }, [handleToggleExpand])

  // Diff renderer
  function renderDiffValue(value) {
    if (value === null || value === undefined) {
      return <span className="text-muted-foreground italic">{t('admin.audit_log.null_value', 'null')}</span>
    }
    if (typeof value === 'object') {
      return (
        <pre className="font-mono text-xs bg-background/50 rounded p-2 overflow-x-auto">
          {JSON.stringify(value, null, 2)}
        </pre>
      )
    }
    return String(value)
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="audit-log-page">
        <PageHeader
           title={t('admin.audit_log.title', 'Audit Log')}
           description={t('admin.audit_log.description', 'Browse audit trail for entity CRUD operations')}
          icon="scroll-text"
        />

        {/* AdminAuditConfigSection gated by audit-log-config:read */}
        <Can resource="audit-log-config" action="read">
          <AdminAuditConfigSection />
        </Can>

        {/* Filter bar — always visible (D-15). D-02/D-04: feature filters sit
            ABOVE the standard search (TableToolbar) and never replace it. */}
        <div className="flex flex-wrap items-end gap-3 p-3 rounded-lg border bg-card">
          {/* Entity type select */}
          <select
            value={selectedEntityType}
            onChange={(e) => updateFilter('entity_type', e.target.value)}
             aria-label={t('admin.audit_log.entity_filter', 'Entity type filter')}
            disabled={isLoading}
            className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          >
            {ENTITY_TYPE_OPTIONS.map((opt) => (
               <option key={opt.value} value={opt.value}>
                 {opt.value ? entityTypeLabel(opt.value, t) : t('admin.audit_log.all_entity_types', 'All entity types')}
               </option>
            ))}
          </select>

          {/* Date preset buttons */}
          <div className="flex rounded-md border border-input bg-background p-0.5">
            {DATE_PRESETS.map((preset) => (
              <button
                key={preset.value}
                type="button"
                onClick={() => handleDatePreset(preset.value === datePreset ? '' : preset.value)}
                disabled={isLoading}
                className={cn(
                  'px-2.5 py-1 text-xs rounded-sm transition-colors',
                  datePreset === preset.value
                    ? 'bg-accent text-accent-foreground font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                )}
              >
                 {datePresetLabel(preset.value, t)}
              </button>
            ))}
          </div>

          {/* Custom date inputs (visible only when custom preset is active) */}
          {datePreset === 'custom' && (
            <>
              <input
                type="date"
                value={customStartDate}
                onChange={(e) => updateFilter('start_date', e.target.value)}
                 aria-label={t('admin.audit_log.date_start', 'Date range start')}
                disabled={isLoading}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
              />
              <input
                type="date"
                value={customEndDate}
                onChange={(e) => updateFilter('end_date', e.target.value)}
                 aria-label={t('admin.audit_log.date_end', 'Date range end')}
                disabled={isLoading}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
              />
            </>
          )}

          {/* Actor search */}
          <div className="relative flex-1 min-w-[200px] max-w-sm">
            <LucideIcon name="search" size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
            <input
              type="text"
              value={actorQuery}
              onChange={(e) => updateFilter('actor_q', e.target.value)}
               placeholder={t('admin.audit_log.actor_placeholder', 'Search by actor name...')}
               aria-label={t('admin.audit_log.actor_search', 'Actor search')}
              disabled={isLoading}
              className="w-full rounded-md border border-input bg-background pl-8 pr-3 py-1.5 text-sm placeholder:text-muted-foreground"
            />
          </div>
        </div>

        {/* Standard search — D-02: always present, combines with the feature
            filter bar above (never silently clears it). */}
        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('admin.audit_log.search_placeholder', 'Search audit entries...')}
          ariaLabel={t('admin.audit_log.search_label', 'Search audit entries')}
          searchTestId="browse-search-input"
        />

        {/* Loading state */}
        {isLoading && <TableSkeleton rows={5} cols={4} />}

        {/* Error state */}
        {isError && !isLoading && (
          <div className="rounded-md bg-destructive/10 p-4 text-destructive text-sm">
               {t('admin.audit_log.load_failed', 'Failed to load audit entries.')}{' '}
               <Button variant="link" onClick={() => refetch()}>{t('common.retry', 'Retry')}</Button>
          </div>
        )}

        {/* Empty state (no data, no filters) */}
        {!isLoading && !isError && displayedItems.length === 0 && !hasFilters && (
          <EmptyState
            variant="empty"
             title={t('admin.audit_log.empty_title', 'No audit entries yet')}
             description={t('admin.audit_log.empty_description', 'Audit entries will appear here as entity CRUD operations are performed.')}
          />
        )}

        {/* Empty state (filters active, no results) */}
        {!isLoading && !isError && displayedItems.length === 0 && hasFilters && (
          <EmptyState
            variant="no-results"
             title={t('admin.audit_log.no_matches_title', 'No matching entries')}
             description={t('admin.audit_log.no_matches_description', 'No audit entries match your current filters. Try adjusting your search criteria.')}
          />
        )}

        {/* Data table */}
        {!isLoading && !isError && displayedItems.length > 0 && (
          <Table>
            <thead>
              <tr className="border-b bg-muted/50 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                 <th scope="col" className="text-left px-4 py-3">{t('admin.audit_log.entity_type', 'Entity Type')}</th>
                 <th scope="col" className="text-left px-4 py-3">{t('common.action', 'Action')}</th>
                 <th scope="col" className="text-left px-4 py-3">{t('admin.audit_log.actor', 'Actor')}</th>
                 <th scope="col" className="text-right px-4 py-3">{t('common.timestamp', 'Timestamp')}</th>
              </tr>
            </thead>
            <tbody>
              {displayedItems.map((entry) => (
                  <Fragment key={entry.id}>
                    {/* Main data row */}
                    <tr
                      onClick={() => handleToggleExpand(entry.id)}
                      onKeyDown={(e) => handleRowKeyDown(e, entry.id)}
                      className={cn(
                        'cursor-pointer border-b transition-colors hover:bg-accent/50',
                        expandedId === entry.id && 'bg-muted/30'
                      )}
                      data-expanded={expandedId === entry.id}
                      data-testid={`audit-row-${entry.id}`}
                      role="button"
                      tabIndex={0}
                      aria-expanded={expandedId === entry.id}
                    >
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center rounded-md bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
                          {entityTypeLabel(entry.entity_type, t)}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${ACTION_BADGE_STYLES[entry.action] || ''}`}>
                          {entry.action}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-2">
                          <span className="h-7 w-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center text-xs font-bold">
                            {entry.actor_initials}
                          </span>
                          {entry.actor_display_name}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <span>{formatDate(entry.created_at)}</span>
                      </td>
                    </tr>
                    {/* Expanded diff row (shown when expandedId matches) */}
                    {expandedId === entry.id && (
                     <tr key={`${entry.id}-diff`} role="region" aria-label={interpolateDefault(t('admin.audit_log.changes_label', 'Changes for {{entity}} {{action}}'), { entity: entry.entity_type, action: entry.action })}>
                        <td colSpan={4} className="p-0 border-b">
                          <div className="px-4 py-3 space-y-1 bg-muted/30">
                            {(() => {
                              const changeFields = Object.entries(entry.changes || {})
                              const totalChangeFields = changeFields.length
                              let displayFields = changeFields

                              // Diff field truncation logic (D-05)
                              if (entry.action === 'CREATE' && !expandedExtraFields.has(entry.id) && totalChangeFields > 10) {
                                displayFields = changeFields.slice(0, 10)
                              }

                              if (totalChangeFields === 0) {
                                return (
                                 <div className="text-sm text-muted-foreground italic">{t('admin.audit_log.no_field_data', 'No field data available')}</div>
                                )
                              }

                              return (
                                <>
                                  {displayFields.map(([field, change]) => (
                                    <div key={field} className="flex gap-2 text-sm">
                                      <span className="font-medium text-muted-foreground min-w-[140px]">{field}:</span>
                                      <span className="text-red-600 dark:text-red-400 line-through decoration-red-400/30">
                                        {renderDiffValue(change.old)}
                                      </span>
                                      <span className="text-muted-foreground px-1">→</span>
                                      <span className="text-green-600 dark:text-green-400">
                                        {renderDiffValue(change.new)}
                                      </span>
                                    </div>
                                  ))}
                                  {/* "+N more" link for CREATE (D-05) */}
                                  {entry.action === 'CREATE' && !expandedExtraFields.has(entry.id) && displayFields.length < totalChangeFields && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); handleToggleExtraFields(entry.id) }}
                                      className="text-xs text-muted-foreground hover:text-foreground underline"
                                    >
                                       {moreFieldsLabel(totalChangeFields - displayFields.length, t)}
                                    </button>
                                  )}
                                  {/* "Show less" for CREATE when expanded */}
                                  {entry.action === 'CREATE' && expandedExtraFields.has(entry.id) && totalChangeFields > 10 && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); handleToggleExtraFields(entry.id) }}
                                      className="text-xs text-muted-foreground hover:text-foreground underline"
                                    >
                                       {t('admin.audit_log.show_less', 'Show less')}
                                    </button>
                                  )}
                                </>
                              )
                            })()}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </Table>
        )}

        {/* Pagination */}
        {!isLoading && displayedItems.length > 0 && (
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={setPage}
            onPageSizeChange={setPageSize}
          />
        )}
      </div>
    </ErrorBoundary>
  )
}
