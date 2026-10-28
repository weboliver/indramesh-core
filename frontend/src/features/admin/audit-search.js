/**
 * AuditLogPage local search adapter (Phase 252, UX-06).
 *
 * The audit endpoint (`useAuditLogs`) has no server `q` parameter — only
 * entity_type/start_date/end_date/actor_q. Per the locked 22-surface manifest
 * (surface 04) the standard search narrows the already-loaded current page in
 * memory, combining with the existing entity/date/actor filters. `entityLabel`
 * resolves a raw entity_type to its translated label so the visible text is
 * what users search against.
 */
export function filterAuditEntries(entries, q, entityLabel) {
  if (!q) return entries
  const needle = q.toLowerCase()
  return entries.filter((entry) =>
    (entityLabel ? entityLabel(entry.entity_type) : entry.entity_type || '').toLowerCase().includes(needle) ||
    (entry.action || '').toLowerCase().includes(needle) ||
    (entry.actor_display_name || '').toLowerCase().includes(needle)
  )
}
