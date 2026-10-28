import { useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useTargets, useDeleteTarget } from '../../shared/hooks/use-targets'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import TargetModal from './TargetModal'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import Target from 'lucide-react/dist/esm/icons/target'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import {
  usePluginConnectorEditors,
  usePluginText,
} from '../../shared/plugin-runtime/PluginProvider'
import { connectorTypeOptions } from '../../shared/plugin-runtime/PluginConnectorEditor'

// D-05: CSV is the only built-in target connector.
const CORE_TARGET_TYPES = ['csv']

export default function TargetListPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #14): URL-synced q, preserves unrelated params, resets page to 1.
  // The loaded current page is filtered in memory — no backend search contract.
  const q = searchParams.get('q') || ''
  const typeFilter = searchParams.get('connectorType') || ''
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize
  const extraParams = {}
  if (typeFilter) extraParams.connector_type = typeFilter
  const { data: targetsData, isLoading, isError, refetch } = useTargets(skip, limit, { includeTotal: true, extraParams })
  const targets = useMemo(() => targetsData?.items || [], [targetsData])
  const total = targetsData?.total || 0
  const formatDate = useFormatDate()

  // D-05: the filter lists CSV plus the active plugin descriptor types; a
  // persisted type no descriptor covers (inactive plugin) stays filterable as
  // its raw stored value.
  const editors = usePluginConnectorEditors('target')
  const pluginText = usePluginText()
  const coreLabels = { csv: t('settings.targets.connector.csv', 'CSV') }
  const typeFilterOptions = useMemo(() => {
    const options = connectorTypeOptions(CORE_TARGET_TYPES, editors, typeFilter)
    const listed = new Set(options.map((option) => option.value))
    targets.forEach((target) => {
      if (target.connector_type && !listed.has(target.connector_type)) {
        listed.add(target.connector_type)
        options.push({ value: target.connector_type, raw: true })
      }
    })
    return options
  }, [editors, typeFilter, targets])

  const query = q.trim().toLowerCase()
  const filteredTargets = query
    ? targets.filter((target) => (target.name || '').toLowerCase().includes(query))
    : targets

  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const [showModal, setShowModal] = useState(false)
  const [editingTarget, setEditingTarget] = useState(null)
  const [actionLoading, setActionLoading] = useState(null)
  const deleteTarget = useDeleteTarget()

  const setTypeFilter = (value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('connectorType', value)
      else next.delete('connectorType')
      next.set('page', '1')
      return next
    })
  }

  const clearFilters = () => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('q')
      next.delete('connectorType')
      next.set('page', '1')
      return next
    })
  }

  const handleDelete = async (targetId) => {
    if (!(await confirmDialog({ message: t('settings.targets.confirm_delete', 'Delete this target? This action cannot be undone.'), danger: true }))) return
    setActionLoading(targetId)
    try {
      await deleteTarget.mutateAsync(targetId)
    } catch {
      // Error handled by axios interceptor
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="target-list-page">
        <PageHeader title={t('settings.targets.title', 'Targets')} description={t('settings.targets.description', 'Target connectors let IndraMesh Core push data to external systems.')} icon="crosshair">
          <Button onClick={() => setShowModal(true)} variant="primary" size="sm" data-testid="target-new-btn">
            {t('settings.targets.new', '+ New Target')}
          </Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.targets.search_placeholder', 'Search Targets...')}
          ariaLabel={t('settings.targets.search_label', 'Search Targets')}
          filters={
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground min-w-[160px]"
              data-testid="targets-filter-type"
            >
              <option value="">{t('settings.targets.all_types', 'All types')}</option>
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
            title={t('settings.targets.load_failed_title', 'Failed to load targets')}
            description={t('settings.targets.load_failed_description', 'Failed to load targets. The server may be unavailable.')}
            buttonLabel={t('common.retry', 'Retry')}
            onButtonClick={() => refetch()}
          />
        ) : filteredTargets.length === 0 ? (
          (q || typeFilter) ? (
            <EmptyState
              variant="no-results"
              title={t('settings.targets.no_results_title', 'No matching targets')}
              description={t('settings.targets.no_results_description', 'Try adjusting your search or filters.')}
              onButtonClick={clearFilters}
            />
          ) : (
            <EmptyState
              variant="empty"
              icon={Target}
              title={t('settings.targets.empty.title', 'No targets yet')}
              description={t('settings.targets.empty.description', 'Target connectors let IndraMesh Core push data to external systems. Create your first target to get started.')}
              buttonLabel={t('settings.targets.new', '+ New Target')}
              onButtonClick={() => setShowModal(true)}
            />
          )
        ) : (
          <div className="space-y-3">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.targets.table.name', 'Name')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.targets.table.connector_type', 'Connector Type')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.targets.table.created', 'Created')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('settings.targets.table.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filteredTargets.map((target) => (
                  <tr key={target.id} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-3 text-sm font-medium">{target.name}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{target.connector_type}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{formatDate(target.created_at)}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setEditingTarget(target)}
                        >
                          {t('common.edit', 'Edit')}
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          onClick={() => handleDelete(target.id)}
                          disabled={actionLoading === target.id}
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

        <TargetModal
          isOpen={showModal || !!editingTarget}
          onClose={() => { setShowModal(false); setEditingTarget(null) }}
          target={editingTarget}
        />
      </div>
    </ErrorBoundary>
  )
}
