import { useState } from 'react'
import TableSkeleton from './TableSkeleton'
import EmptyState from './EmptyState'
import { PaginationBar } from './PaginationBar'
import { useTranslation } from 'react-i18next'

// Local (non-URL) paging/search/filter state for an AssignmentPicker instance.
// Deliberately NOT the URL-backed usePaginationParams hook: a single page can
// host two independent pickers (e.g. RoleDetail's Users + Groups sections),
// and both would collide on the same `page`/`page_size` query-string keys.
export function useAssignmentPickerState(defaultPageSize = 25) {
  const [q, setQRaw] = useState('')
  const [filter, setFilterRaw] = useState('all') // 'all' | 'assigned' | 'unassigned'
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeRaw] = useState(defaultPageSize)

  const setQ = (value) => { setQRaw(value); setPage(1) }
  const setFilter = (value) => { setFilterRaw(value); setPage(1) }
  const setPageSize = (value) => { setPageSizeRaw(value); setPage(1) }

  const skip = (page - 1) * pageSize
  const assignedParam = filter === 'all' ? undefined : filter === 'assigned'

  return { q, setQ, filter, setFilter, page, setPage, pageSize, setPageSize, skip, assignedParam }
}

// Generic searchable + paginated + assigned/unassigned-filterable checkbox
// list. The caller owns data fetching (via useAssignmentPickerState + its own
// query hook) and passes down the CURRENT PAGE's items plus the running
// total — this component only renders controls and the list.
export default function AssignmentPicker({
  items,
  total,
  isLoading,
  isError,
  q,
  onQChange,
  filter,
  onFilterChange,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
  assignedIds,
  pendingId,
  canManage,
  onToggle,
  getId = (item) => item.id,
  renderLabel,
  renderBadge,
  searchPlaceholder,
  emptyTitle,
  emptyDescription,
  noResultsTitle,
  testIdPrefix,
}) {
  const { t } = useTranslation()
  const isFiltered = q.trim() !== '' || filter !== 'all'

  return (
    <div className="space-y-2" data-testid={`${testIdPrefix}-list`}>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => onQChange(e.target.value)}
          placeholder={searchPlaceholder}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-full max-w-sm"
          data-testid={`${testIdPrefix}-search-input`}
        />
        <select
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
          className="rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground"
          data-testid={`${testIdPrefix}-filter-select`}
        >
          <option value="all">{t('common.filter.all', 'All')}</option>
          <option value="assigned">{t('common.filter.assigned', 'Assigned')}</option>
          <option value="unassigned">{t('common.filter.unassigned', 'Not assigned')}</option>
        </select>
      </div>

      {isLoading ? (
        <TableSkeleton rows={4} cols={1} />
      ) : isError ? (
        <EmptyState variant="error" />
      ) : items.length === 0 ? (
        <EmptyState
          variant={isFiltered ? 'no-results' : 'empty'}
          title={isFiltered ? noResultsTitle : emptyTitle}
          description={isFiltered ? undefined : emptyDescription}
        />
      ) : (
        <div className="space-y-2">
          {items.map((item) => {
            const id = getId(item)
            const checked = assignedIds.has(id)
            const pending = pendingId === id
            const disabled = !canManage || pending
            return (
              <label
                key={id}
                className="flex items-center gap-2 p-2 border rounded-md text-sm font-normal cursor-pointer hover:bg-muted/50 transition-colors"
                data-testid={`${testIdPrefix}-row`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled}
                  onChange={(e) => onToggle(item, e.target.checked)}
                  data-testid={`${testIdPrefix}-toggle-${id}`}
                />
                {renderLabel(item)}
                {renderBadge?.(item)}
              </label>
            )
          })}
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={onPageChange}
            onPageSizeChange={onPageSizeChange}
          />
        </div>
      )}
    </div>
  )
}
