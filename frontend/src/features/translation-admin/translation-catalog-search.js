/**
 * TranslationCatalogTable local search (Phase 252, UX-06).
 *
 * Surface 21 ("local loaded-list filter"): the standard search narrows the
 * already-loaded `rows` the table receives from its parent in memory, while
 * the parent's namespace/language/missing server filters remain intact. Pure
 * so the migration to the shared BrowseSearchInput control is unit-testable.
 */
export function filterCatalogRows(rows, q) {
  const needle = (q || '').trim().toLowerCase()
  if (!needle) return rows
  return rows.filter((row) =>
    (row.namespace || '').toLowerCase().includes(needle) ||
    (row.key || '').toLowerCase().includes(needle) ||
    (row.default_text || '').toLowerCase().includes(needle) ||
    (row.effective_value || '').toLowerCase().includes(needle) ||
    (row.stored_value || '').toLowerCase().includes(needle)
  )
}
