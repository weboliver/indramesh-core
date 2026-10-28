import { useTranslation } from 'react-i18next'

export default function TableSkeleton({ rows = 5, cols = 4 }) {
  const { t } = useTranslation()
  if (rows === 0 || cols === 0) return null

  return (
    <div
      className="rounded-lg border"
      data-testid="skeleton"
      role="status"
       aria-label={t('common.loading_table_content', 'Loading table content')}
    >
      {Array.from({ length: rows }).map((_, rowIdx) => (
        <div key={rowIdx} className="flex gap-4 border-b last:border-0 p-4">
          {Array.from({ length: cols }).map((_, colIdx) => (
            <div key={colIdx} className="h-4 flex-1 rounded animate-shimmer motion-safe:animate-shimmer" />
          ))}
        </div>
      ))}
    </div>
  )
}
