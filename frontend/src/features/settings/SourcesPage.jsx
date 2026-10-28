import { useState, useCallback, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useSources, useDeleteSource } from '../../shared/hooks/use-sources'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import SourceModal from './SourceModal'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { Can } from '../../shared/components/Can'
import PermissionsDialog from '../permissions/PermissionsDialog'
import Database from 'lucide-react/dist/esm/icons/database'
import Layers from 'lucide-react/dist/esm/icons/layers'
import Shield from 'lucide-react/dist/esm/icons/shield'
import { useTranslation } from 'react-i18next'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import {
  usePluginConnectorEditors,
  usePluginText,
} from '../../shared/plugin-runtime/PluginProvider'
import { connectorTypeOptions } from '../../shared/plugin-runtime/PluginConnectorEditor'

// D-05: the neutral core source types; `jira` is a plugin contribution.
const CORE_SOURCE_TYPES = ['internal', 'api']

export default function SourcesPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  // D-02 standard search adapter (server q surface, manifest #11): URL-synced
  // `q` drives the existing server `name` filter, preserves the unrelated
  // `type` filter, and resets page to 1 on change or clear.
  const q = searchParams.get('q') || ''
  const typeFilter = searchParams.get('type') || ''
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize
  const extraParams = {}
  if (q) extraParams.name = q
  if (typeFilter) extraParams.type = typeFilter
  const { data: sourcesData, isLoading, isError, refetch } = useSources(skip, limit, { includeTotal: true, extraParams })
  const sources = useMemo(() => sourcesData?.items || [], [sourcesData])
  const total = sourcesData?.total || 0
  const formatDate = useFormatDate()

  // D-05: the filter lists the neutral core types plus the active plugin
  // descriptor types; a persisted type no descriptor covers (inactive plugin)
  // stays filterable as its raw stored value.
  const editors = usePluginConnectorEditors('source')
  const pluginText = usePluginText()
  const coreLabels = {
    internal: t('settings.sources.type.internal', 'Internal'),
    api: t('settings.sources.type.api', 'API'),
  }
  const typeFilterOptions = useMemo(() => {
    const options = connectorTypeOptions(CORE_SOURCE_TYPES, editors, typeFilter)
    const listed = new Set(options.map((option) => option.value))
    sources.forEach((source) => {
      if (source.type && !listed.has(source.type)) {
        listed.add(source.type)
        options.push({ value: source.type, raw: true })
      }
    })
    return options
  }, [editors, typeFilter, sources])
  const [showModal, setShowModal] = useState(false)
  const [editingSource, setEditingSource] = useState(null)
  const [actionLoading, setActionLoading] = useState(null)
  const [permSourceId, setPermSourceId] = useState(null)
  const deleteSource = useDeleteSource()

  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const setTypeFilter = (value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('type', value)
      else next.delete('type')
      next.set('page', '1')
      return next
    })
  }

  const clearFilters = () => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('q')
      next.delete('type')
      next.set('page', '1')
      return next
    })
  }

  const handleDelete = async (sourceId) => {
     if (!(await confirmDialog({ message: t('settings.sources.confirm_delete', 'Delete this source? This action cannot be undone.'), danger: true }))) return
    setActionLoading(sourceId)
    try {
      await deleteSource.mutateAsync(sourceId)
    } catch {
      // Error handled by axios interceptor
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="source-list-page">
         <PageHeader title={t('settings.sources.title', 'Sources')} description={t('settings.sources.description', 'Manage data sources and their service accounts.')} icon="globe">
          <Button onClick={() => setShowModal(true)} variant="primary" size="sm" data-testid="source-new-btn">
             {t('settings.sources.new', '+ New Source')}
          </Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.sources.search_placeholder', 'Search Sources...')}
          ariaLabel={t('settings.sources.search_label', 'Search Sources')}
          filters={
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground min-w-[160px]"
              data-testid="sources-filter-type"
            >
              <option value="">{t('settings.sources.all_types', 'All types')}</option>
              {typeFilterOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.plugin
                    ? interpolateDefault(
                        t('settings.connector.type_option_plugin', '{{label}} (Plugin: {{plugin}})'),
                        {
                          label: pluginText(
                            option.plugin.pluginName,
                            option.plugin.labelKey,
                            option.plugin.label
                          ),
                          plugin: option.plugin.pluginName,
                        }
                      )
                    : option.raw
                      ? option.value
                      : coreLabels[option.value]}
                </option>
              ))}
            </select>
          }
        />

        {isLoading ? (
          <TableSkeleton rows={5} cols={4} />
        ) : isError ? (
          <EmptyState
            variant="error"
             title={t('settings.sources.load_failed_title', 'Failed to load sources')}
             description={t('settings.sources.load_failed_description', 'Failed to load sources. The server may be unavailable.')}
             buttonLabel={t('common.retry', 'Retry')}
            onButtonClick={() => refetch()}
          />
        ) : sources.length === 0 ? (
          (q || typeFilter) ? (
            <EmptyState
              variant="no-results"
              title={t('settings.sources.no_results_title', 'No matching sources')}
              description={t('settings.sources.no_results_description', 'Try adjusting your search or filters.')}
              onButtonClick={clearFilters}
            />
          ) : (
            <EmptyState
              variant="empty"
              icon={Database}
               title={t('settings.sources.empty_title', 'No sources yet')}
               description={t('settings.sources.empty_description', 'Manage data sources and their service accounts. Create your first source to get started.')}
               buttonLabel={t('settings.sources.new', '+ New Source')}
              onButtonClick={() => setShowModal(true)}
            />
          )
        ) : (
          <div className="space-y-3">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.type', 'Type')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.url', 'URL')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.created', 'Created')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((source) => (
                  <tr key={source.id} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-3 text-sm font-medium">{source.name}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{source.type}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground max-w-[200px] truncate">
                      {source.source_url || '-'}
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      {formatDate(source.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Link
                          to={`/configuration/schemas?sourceId=${source.id}`}
                          className="inline-flex items-center justify-center rounded-md p-2 text-sm font-medium hover:bg-accent hover:text-accent-foreground"
                           title={t('settings.sources.view_schemas', 'View Schemas')}
                          data-testid="view-schemas-btn"
                        >
                          <Layers className="h-4 w-4" />
                        </Link>
                        <Can resource="roles" action="manage">
                          <button
                            type="button"
                            onClick={() => setPermSourceId(source.id)}
                            className="inline-flex items-center justify-center rounded-md p-2 text-sm font-medium hover:bg-accent hover:text-accent-foreground"
                             title={t('common.permissions', 'Permissions')}
                            data-testid="source-permissions-btn"
                          >
                            <Shield className="h-4 w-4" />
                          </button>
                        </Can>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setEditingSource(source)}
                        >
                           {t('common.edit', 'Edit')}
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          onClick={() => handleDelete(source.id)}
                          disabled={actionLoading === source.id}
                        >
                           {t('common.delete', 'Delete')}
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

        <SourceModal
          isOpen={showModal || !!editingSource}
          onClose={() => { setShowModal(false); setEditingSource(null) }}
          target={editingSource}
        />

        <PermissionsDialog
          entityType="source"
          recordId={permSourceId}
          open={!!permSourceId}
          onClose={() => setPermSourceId(null)}
        />
      </div>
    </ErrorBoundary>
  )
}
