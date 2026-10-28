import { useTranslation } from 'react-i18next'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useBrowseSearchController } from './browse-search-controller'

/**
 * Presentational search control (Phase 252, UX-06 render-control API).
 *
 * Renders the standard Lucide search icon + debounced input + clear button
 * from a controller returned by `useBrowseSearchController`. BrowseSearchInput
 * wires its own controller through this; SearchPage composes its AQL textarea
 * adapter from the same controller primitives so both modes share the single
 * baseline debounce/gate/clear/URL-sync implementation.
 */
export function BrowseSearchControl({ controller, className = '', placeholder, ariaLabel, testId = 'browse-search-input', autoFocus }) {
  const { t } = useTranslation()

  return (
    <div className={`relative ${className}`}>
      <LucideIcon name="search" size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
      <input
        type="text"
        value={controller.searchInput}
        onChange={(e) => controller.handleChange(e.target.value)}
        placeholder={placeholder ?? t('browse.search.placeholder', 'Search objects...')}
        aria-label={ariaLabel ?? t('browse.search.accessible_label', 'Search objects')}
        data-testid={testId}
        autoFocus={autoFocus}
        className="w-full rounded-lg border border-input bg-background pl-9 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary transition-colors"
      />
      {controller.searchInput && (
        <button
          type="button"
          onClick={controller.handleClear}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
          aria-label={t('browse.search.clear', 'Clear search')}
          data-testid="browse-search-clear"
        >
          <LucideIcon name="x" size={14} />
        </button>
      )}
    </div>
  )
}

export default function BrowseSearchInput({ urlQ, setQ, className = '', placeholder, ariaLabel, testId = 'browse-search-input' }) {
  const controller = useBrowseSearchController({ urlQ, setQ })
  return (
    <BrowseSearchControl
      controller={controller}
      className={className}
      placeholder={placeholder}
      ariaLabel={ariaLabel}
      testId={testId}
    />
  )
}