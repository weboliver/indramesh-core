import BrowseSearchInput from '../../features/browse/BrowseSearchInput'

/**
 * Shared TableToolbar primitive (Phase 252, UX-06).
 *
 * Sits below PageHeader and above Table. Always renders the standard
 * BrowseSearchInput inside role="search"; optional filter, column, and
 * action controls compose beside it and wrap on narrow widths. Filters must
 * never hide or replace search (UI-SPEC Interaction Contract).
 *
 * `searchControl` replaces the default BrowseSearchInput with a mode adapter
 * (e.g. SearchPage's AqlBrowseSearchInput) while keeping the toolbar mounted
 * and the mandatory standard-search slot occupied.
 */
export default function TableToolbar({
  urlQ,
  setQ,
  placeholder,
  ariaLabel,
  searchControl,
  filters,
  columns,
  actions,
  className = '',
  searchTestId,
  'data-testid': testId = 'table-toolbar',
}) {
  return (
    <div className={`flex items-center gap-2 flex-wrap ${className}`} role="search" data-testid={testId}>
      {filters}
      {searchControl ?? (
        <BrowseSearchInput
          urlQ={urlQ}
          setQ={setQ}
          placeholder={placeholder}
          ariaLabel={ariaLabel}
          testId={searchTestId}
          className="flex-1 min-w-[200px] max-w-sm"
        />
      )}
      {columns}
      {actions}
    </div>
  )
}