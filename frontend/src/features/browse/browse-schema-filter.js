/**
 * Local loaded-list filter for the ObjectTypeObjects schema tab (manifest
 * surface 07, Phase 252 UX-06).
 *
 * The schema tab's already-loaded attribute list is filtered in memory by
 * the standard URL-synced q. Matching covers the attribute name, its
 * data-type display label, and its description — no backend search contract.
 *
 * The tab's rows are drag-reorderable (sort updates are applied to the full
 * loaded list), so the component pairs this predicate with original indices:
 * filtering must never renumber the rows it passes to the drop handlers.
 */
export function matchSchemaAttribute(attr, query, labelFor) {
  const q = (query || '').trim().toLowerCase()
  if (!q) return true
  const haystack = [
    attr?.name || '',
    labelFor ? labelFor(attr) || '' : '',
    attr?.description || '',
  ].map((s) => s.toLowerCase())
  return haystack.some((s) => s.includes(q))
}

export function filterSchemaAttributes(attributes, q, labelFor) {
  return (attributes || []).filter((attr) => matchSchemaAttribute(attr, q, labelFor))
}