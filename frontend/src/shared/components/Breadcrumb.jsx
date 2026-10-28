import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

/**
 * Shared Breadcrumb primitive (Phase 252, UX-07).
 *
 * Takes an explicit ordered `items` array of { label, href? }. All items
 * except the final/current one require href and render as React Router
 * Links; the final item has no href, renders as text with
 * aria-current="page". Separators are decorative (aria-hidden, not
 * focusable). Long labels wrap at item boundaries and unbroken tokens break
 * (break-words) so entity names never force horizontal page scrolling; the
 * current label keeps a native title with its full name (never a raw ID
 * fallback).
 */
export default function Breadcrumb({ items, 'data-testid': testId = 'breadcrumb' }) {
  const { t } = useTranslation()
  return (
    <nav aria-label={t('common.breadcrumb', 'Breadcrumb')} data-testid={testId}>
      <ol className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
        {items.map((item, idx) => {
          const isCurrent = idx === items.length - 1
          return (
            <li key={`${item.label}-${idx}`} className="flex items-center gap-1.5 min-w-0">
              {idx > 0 && (
                <span aria-hidden="true" className="select-none text-muted-foreground">
                  /
                </span>
              )}
              {isCurrent ? (
                <span className="min-w-0 break-words text-foreground" aria-current="page" title={item.label}>
                  {item.label}
                </span>
              ) : (
                <Link
                  to={item.href}
                  className="min-w-0 break-words hover:text-foreground hover:underline focus-visible:text-foreground"
                >
                  {item.label}
                </Link>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}