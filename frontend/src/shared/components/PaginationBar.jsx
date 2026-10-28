import { Button } from './Button'
import { cn } from '../lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'

const PAGE_SIZES = [10, 25, 50, 100]

export function PaginationBar({ page, pageSize, total, onPageChange, onPageSizeChange, hideRange, hidePageSize, className }) {
  const { t } = useTranslation()
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, total)
  const isFirst = page === 1
  const isLast = page === totalPages

  return (
    <div
      className={cn(
        'flex items-center justify-between h-10 px-2 border-t bg-background text-sm text-muted-foreground',
        className
      )}
      role="navigation"
       aria-label={t('common.pagination', 'Pagination')}
    >
      <div className="flex items-center gap-1.5">
        {hidePageSize ? (
          <span className="text-xs" data-testid="pagination-page-size-fixed">
            {interpolateDefault(
              t('common.items_per_page_fixed', 'Items per page: {{count}}'),
              { count: pageSize }
            )}
          </span>
        ) : (
          <>
            <label htmlFor="pagination-page-size" className="text-xs">{t('common.items_per_page', 'Items per page')}</label>
            <select
              id="pagination-page-size"
              value={pageSize}
              onChange={(e) => onPageSizeChange(Number(e.target.value))}
              className="rounded-md border border-input bg-background px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-primary"
              data-testid="pagination-page-size"
            >
              {PAGE_SIZES.map(size => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </>
        )}
      </div>

      {!hideRange && (
        <span className="text-xs whitespace-nowrap" aria-live="polite">
          {interpolateDefault(
            t('common.showing_range', 'Showing {{start}}–{{end}} of {{total}}', { start, end, total }),
            { start, end, total }
          )}
        </span>
      )}

      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          disabled={isFirst}
          onClick={() => onPageChange(1)}
           aria-label={t('common.first_page', 'First page')}
          data-testid="pagination-first"
        >
          &laquo;
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={isFirst}
          onClick={() => onPageChange(page - 1)}
           aria-label={t('common.previous_page', 'Previous page')}
          data-testid="pagination-prev"
        >
          &lsaquo;
        </Button>
        <span className="px-1 text-xs font-medium" aria-live="polite" data-testid="pagination-indicator">
          {interpolateDefault(
            t('common.page_of_pages', 'Page {{page}} of {{totalPages}}', { page, totalPages }),
            { page, totalPages }
          )}
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={isLast}
          onClick={() => onPageChange(page + 1)}
           aria-label={t('common.next_page', 'Next page')}
          data-testid="pagination-next"
        >
          &rsaquo;
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={isLast}
          onClick={() => onPageChange(totalPages)}
           aria-label={t('common.last_page', 'Last page')}
          data-testid="pagination-last"
        >
          &raquo;
        </Button>
      </div>
    </div>
  )
}
