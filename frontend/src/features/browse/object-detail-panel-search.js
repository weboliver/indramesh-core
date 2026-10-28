/**
 * Local loaded-list filters for the ObjectDetailPanel locked table surfaces
 * (manifest surfaces 08 and 09, Phase 252 UX-06).
 *
 * Both tables filter their already-loaded rows in memory by the standard
 * URL-synced q — no backend search contract. Reference links, inline
 * editing, badges, and panel loading/error behavior are untouched: these
 * functions only decide which loaded rows render.
 */

export function filterAttributeAndRefRows({ values, refGroups }, q) {
  const query = (q || '').trim().toLowerCase()
  if (!query) return { values, refGroups }
  return {
    values: (values || []).filter((v) =>
      (v.attribute_name || '').toLowerCase().includes(query)
    ),
    refGroups: (refGroups || []).filter(([attrName]) =>
      (attrName || '').toLowerCase().includes(query)
    ),
  }
}

export function filterInboundGroups(groups, q) {
  const query = (q || '').trim().toLowerCase()
  if (!query) return groups
  return (groups || [])
    .map((group) => ({
      ...group,
      refs: group.refs.filter((r) =>
        (group.typeName || '').toLowerCase().includes(query) ||
        (r.from_object_name || '').toLowerCase().includes(query) ||
        (r.from_attribute_name || '').toLowerCase().includes(query)
      ),
    }))
    .filter((group) => group.refs.length > 0)
}