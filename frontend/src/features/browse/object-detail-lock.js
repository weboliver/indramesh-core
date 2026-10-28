/**
 * Pure helpers for the object detail panel's locked-attribute presentation
 * (290 D-02/D-08): which unset locked attributes get a read-only `—` row and
 * how those rows merge into the existing value rows.
 *
 * This module intentionally has no React or app imports so it also runs under
 * `node --test`.
 */

export function isReferenceAttribute(attr) {
  return Boolean(attr?.referenced_object_type_id)
}

export function collectUnsetLockedAttributes({ attributes, values, refs } = {}) {
  const valueAttrIds = new Set((values || []).map((v) => v.object_type_attribute_id))
  const refAttrNames = new Set(
    (refs || []).map((r) => r.from_attribute_name).filter((name) => name)
  )
  const seenIds = new Set()
  const scalar = []
  const reference = []
  for (const attr of attributes || []) {
    if (!attr || attr.is_locked !== true) continue
    if (seenIds.has(attr.id)) continue
    if (valueAttrIds.has(attr.id)) continue
    if (attr.name && refAttrNames.has(attr.name)) continue
    seenIds.add(attr.id)
    if (isReferenceAttribute(attr)) reference.push(attr)
    else scalar.push(attr)
  }
  const bySort = (a, b) => (a.sort ?? 0) - (b.sort ?? 0)
  return { scalar: scalar.sort(bySort), reference: reference.sort(bySort) }
}

export function mergeValueAndUnsetRows(dedupedValues, unsetScalar, attributes) {
  const attrById = new Map((attributes || []).map((attr) => [attr.id, attr]))
  const entries = [
    ...(dedupedValues || []).map((value) => ({ kind: 'value', value })),
    ...(unsetScalar || []).map((attribute) => ({ kind: 'unset', attribute })),
  ]
  const sortKey = (entry) => entry.kind === 'value'
    ? (attrById.get(entry.value.object_type_attribute_id)?.sort ?? 0)
    : (entry.attribute.sort ?? 0)
  return entries.sort((a, b) => sortKey(a) - sortKey(b))
}

export function normalizeManagedBy(value) {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

export function computeLockTooltipPosition({ anchor, tooltip, viewport, margin = 8, gap = 6 }) {
  const size = (value) => (Number.isFinite(value) && value > 0 ? value : 0)
  const coord = (value) => (Number.isFinite(value) ? value : 0)

  const anchorTop = coord(anchor?.top)
  const anchorLeft = coord(anchor?.left)
  const anchorWidth = size(anchor?.width)
  const anchorHeight = size(anchor?.height)
  const anchorBottom = Number.isFinite(anchor?.bottom) ? anchor.bottom : anchorTop + anchorHeight
  const tooltipWidth = size(tooltip?.width)
  const tooltipHeight = size(tooltip?.height)
  const viewportWidth = size(viewport?.width)
  const viewportHeight = size(viewport?.height)

  const anchorCenterX = anchorLeft + anchorWidth / 2
  const maxLeft = viewportWidth - margin - tooltipWidth
  const left = Math.min(
    Math.max(anchorCenterX - tooltipWidth / 2, margin),
    Math.max(margin, maxLeft)
  )

  const fitsAbove = anchorTop - gap - tooltipHeight >= margin
  const fitsBelow = anchorBottom + gap + tooltipHeight <= viewportHeight - margin
  let placement
  let top
  if (fitsAbove) {
    placement = 'top'
    top = anchorTop - gap - tooltipHeight
  } else if (fitsBelow) {
    placement = 'bottom'
    top = anchorBottom + gap
  } else {
    const roomAbove = anchorTop - gap - margin
    const roomBelow = viewportHeight - margin - (anchorBottom + gap)
    placement = roomAbove >= roomBelow ? 'top' : 'bottom'
    top = placement === 'top' ? anchorTop - gap - tooltipHeight : anchorBottom + gap
    top = Math.min(Math.max(top, margin), Math.max(margin, viewportHeight - margin - tooltipHeight))
  }

  return { left, top, placement }
}
// A per-object release lifts only the listed attributes; everything else keeps
// the type-level projection.
export function applyObjectRelease(attributes, releasedIds) {
  if (!Array.isArray(attributes) || !releasedIds?.length) return attributes
  const released = new Set(releasedIds)
  return attributes.map((attr) => (released.has(attr.id) ? { ...attr, is_locked: false } : attr))
}
