/**
 * ObjectTypeBreadcrumb adapter data logic (Phase 252, UX-07).
 *
 * The browse hierarchy breadcrumb keeps its Source → Schema → ObjectType
 * parent-chain loading, but presentation is delegated to the generic
 * Breadcrumb component through the D-03 explicit-items contract. These pure
 * helpers build the item list ONLY from loaded permitted display data —
 * never a raw identifier fallback (T-252-13).
 */

export function buildTypeChain(objectType, schemaObjectTypes) {
  if (!objectType) return []
  const byId = {}
  for (const t of schemaObjectTypes || []) byId[t.id] = t
  byId[objectType.id] = objectType
  const typeChain = []
  let node = objectType
  const seen = new Set()
  while (node && !seen.has(node.id)) {
    seen.add(node.id)
    typeChain.unshift(node)
    node = node.parent_id ? byId[node.parent_id] : null
  }
  return typeChain
}

export function buildObjectTypeBreadcrumbItems({ source, schema, typeChain, trailing }) {
  const items = []
  if (source?.name) {
    items.push({ label: source.name, href: '/configuration/sources' })
  }
  if (schema?.name && schema.id) {
    items.push({ label: schema.name, href: `/configuration/schemas/${schema.id}` })
  }
  for (const t of typeChain || []) {
    items.push({
      label: t.name,
      href: `/browse/object-types/${t.id}`,
    })
  }
  // Without a trailing label the final ObjectType IS the current page, so it
  // renders as text with aria-current instead of a link. With a trailing
  // label the object name becomes the current item and the ObjectType stays
  // navigable.
  if (!trailing && items.length > 0) {
    const last = items[items.length - 1]
    delete last.href
  }
  if (trailing) {
    items.push({ label: trailing })
  }
  return items
}