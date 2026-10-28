import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useSavedQueries, useDeleteSavedQuery } from '../../shared/hooks/use-saved-queries'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'

// Master-list pane of the Core Saved Queries workspace (304 GQL-01/D-12).
// Keeps the historic testids (saved-queries-list/search/row/load-btn/delete-btn)
// so the surface stays regression-covered.
export default function SavedQueriesList({ onLoad, onDeleted, selectedId, canDelete }) {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const q = searchParams.get('q') || ''
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const { data, isLoading, isError, refetch } = useSavedQueries(skip, pageSize, q)
  const items = data?.items || []
  const total = data?.total || 0
  const formatDate = useFormatDate()
  const deleteMutation = useDeleteSavedQuery()
  const [deletingId, setDeletingId] = useState(null)

  const setQ = (value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    })
  }

  const handleDelete = async (event, item) => {
    event.stopPropagation()
    const confirmed = await confirmDialog({
      message: t('graphql.saved_queries.confirm_delete', 'Delete this saved query? This cannot be undone.'),
      danger: true,
      confirmLabel: t('graphql.saved_queries.delete_action', 'Delete query'),
      cancelLabel: t('graphql.saved_queries.delete_keep', 'Keep query'),
    })
    if (!confirmed) return
    setDeletingId(item.id)
    deleteMutation.mutate(item.id, {
      onSuccess: () => onDeleted?.(item),
      onSettled: () => setDeletingId(null),
    })
  }

  const handleRowKeyDown = (event, item) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onLoad(item)
    }
  }

  return (
    <div className="flex h-full flex-col" data-testid="saved-queries-list">
      <div className="shrink-0 border-b px-2 py-2">
        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('graphql.saved_queries.search_placeholder', 'Search saved queries')}
          ariaLabel={t('graphql.saved_queries.search_label', 'Search saved queries')}
          searchTestId="saved-queries-search"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto" aria-busy={isLoading}>
        {isLoading ? (
          <TableSkeleton rows={5} cols={3} />
        ) : isError ? (
          <EmptyState
            variant="error"
            title={t('graphql.saved_queries.load_failed', 'Failed to load saved queries')}
            buttonLabel={t('common.retry', 'Retry')}
            onButtonClick={() => refetch()}
          />
        ) : items.length === 0 ? (
          q ? (
            <EmptyState variant="no-results" />
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-1 px-4 py-8 text-center">
              <LucideIcon name="bookmark" size={28} className="text-muted-foreground/50" aria-hidden="true" />
              <p className="text-sm font-semibold">{t('graphql.saved_queries.empty_heading', 'No saved queries')}</p>
              <p className="max-w-xs text-sm text-muted-foreground">
                {t('graphql.saved_queries.empty_body', 'Create a query with New query, or import a JSON document.')}
              </p>
            </div>
          )
        ) : (
          <Table>
            <thead>
              <tr className="border-b">
                <th scope="col" className="px-3 py-2 text-left text-xs font-semibold">{t('graphql.saved_queries.table.name', 'Name')}</th>
                <th scope="col" className="px-3 py-2 text-left text-xs font-semibold">{t('graphql.saved_queries.table.created', 'Created')}</th>
                <th scope="col" className="px-3 py-2 text-right text-xs font-semibold">{t('graphql.saved_queries.table.actions', 'Actions')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const selected = item.id === selectedId
                return (
                  <tr
                    key={item.id}
                    className={cn(
                      'cursor-pointer border-b last:border-0 transition-colors hover:bg-muted/30',
                      selected && 'border-l-2 border-l-primary bg-muted/40'
                    )}
                    data-testid={`saved-query-row-${item.id}`}
                    data-selected={selected ? 'true' : 'false'}
                    aria-selected={selected}
                    aria-current={selected ? 'true' : undefined}
                    onClick={() => onLoad(item)}
                    onKeyDown={(event) => handleRowKeyDown(event, item)}
                    tabIndex={0}
                  >
                    <td className="min-w-0 px-3 py-2 align-top">
                      <span className="block min-w-0 break-words text-sm font-medium">{item.name}</span>
                      <span className="mt-0.5 block max-w-[18rem] truncate font-mono text-xs text-muted-foreground" title={item.query_text}>
                        {item.query_text}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 align-top text-xs text-muted-foreground">
                      {item.created_at ? formatDate(item.created_at) : t('common.date_unavailable', '-')}
                    </td>
                    <td className="px-3 py-2 align-top text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={(event) => { event.stopPropagation(); onLoad(item) }}
                          data-testid={`load-btn-${item.id}`}
                        >
                          {t('graphql.saved_queries.load', 'Load')}
                        </Button>
                        {canDelete && (
                          <Button
                            type="button"
                            variant="destructive"
                            size="sm"
                            onClick={(event) => handleDelete(event, item)}
                            disabled={deletingId === item.id}
                            loading={deletingId === item.id}
                            data-testid={`delete-btn-${item.id}`}
                          >
                            {t('common.delete', 'Delete')}
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </Table>
        )}
      </div>

      {total > 0 && (
        <PaginationBar
          page={page}
          pageSize={pageSize}
          total={total}
          onPageChange={setPage}
          onPageSizeChange={setPageSize}
        />
      )}
    </div>
  )
}