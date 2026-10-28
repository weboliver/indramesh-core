import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  applyObjectRelease,
  collectUnsetLockedAttributes,
  computeLockTooltipPosition,
  isReferenceAttribute,
  mergeValueAndUnsetRows,
  normalizeManagedBy,
} from './object-detail-lock.js'

describe('isReferenceAttribute (shared reference classification)', () => {
  it('is true for an attribute with a referenced object type', () => {
    assert.equal(isReferenceAttribute({ id: 'a1', referenced_object_type_id: 't1' }), true)
  })

  it('is false for a plain scalar attribute or missing input', () => {
    assert.equal(isReferenceAttribute({ id: 'a1' }), false)
    assert.equal(isReferenceAttribute({ id: 'a1', data_type: 'text' }), false)
    assert.equal(isReferenceAttribute(null), false)
  })
})

describe('collectUnsetLockedAttributes (unset locked attribute rows)', () => {
  it('returns empty lists for undefined, empty and missing inputs', () => {
    assert.deepEqual(collectUnsetLockedAttributes({}), { scalar: [], reference: [] })
    assert.deepEqual(
      collectUnsetLockedAttributes({ attributes: [], values: [], refs: [] }),
      { scalar: [], reference: [] },
    )
    assert.deepEqual(
      collectUnsetLockedAttributes({ attributes: undefined, values: undefined, refs: undefined }),
      { scalar: [], reference: [] },
    )
  })

  it('returns a locked attribute without value or reference', () => {
    const attr = { id: 'a1', name: 'Owner', is_locked: true, sort: 1 }
    const out = collectUnsetLockedAttributes({ attributes: [attr], values: [], refs: [] })
    assert.deepEqual(out.scalar, [attr])
    assert.deepEqual(out.reference, [])
  })

  it('never returns an attribute whose lock is false, missing or non-boolean', () => {
    const attributes = [
      { id: 'a1', name: 'One', is_locked: false },
      { id: 'a2', name: 'Two' },
      { id: 'a3', name: 'Three', is_locked: 'true' },
      { id: 'a4', name: 'Four', is_locked: 1 },
    ]
    const out = collectUnsetLockedAttributes({ attributes, values: [], refs: [] })
    assert.deepEqual(out, { scalar: [], reference: [] })
  })

  it('excludes a locked attribute that already has a value, including multi-value attributes', () => {
    const attributes = [{ id: 'a1', name: 'Owner', is_locked: true, sort: 1 }]
    const values = [
      { id: 'v1', object_type_attribute_id: 'a1' },
      { id: 'v2', object_type_attribute_id: 'a1' },
    ]
    const out = collectUnsetLockedAttributes({ attributes, values, refs: [] })
    assert.deepEqual(out, { scalar: [], reference: [] })
  })

  it('excludes a locked reference attribute whose ref matches it by name', () => {
    const attributes = [
      { id: 'a1', name: 'Related Server', is_locked: true, referenced_object_type_id: 't1' },
    ]
    const refs = [{ id: 'r1', from_attribute_name: 'Related Server' }]
    const out = collectUnsetLockedAttributes({ attributes, values: [], refs })
    assert.deepEqual(out, { scalar: [], reference: [] })
  })

  it('collapses duplicate attribute ids to one row', () => {
    const attributes = [
      { id: 'a1', name: 'Owner', is_locked: true, sort: 1 },
      { id: 'a1', name: 'Owner', is_locked: true, sort: 1 },
    ]
    const out = collectUnsetLockedAttributes({ attributes, values: [], refs: [] })
    assert.equal(out.scalar.length, 1)
    assert.equal(out.scalar[0].id, 'a1')
  })

  it('splits reference-kind attributes and stably sorts both lists by sort', () => {
    const attributes = [
      { id: 'a1', name: 'Second', is_locked: true, sort: 2 },
      { id: 'a2', name: 'First', is_locked: true, sort: 1 },
      { id: 'a3', name: 'Ref', is_locked: true, sort: 3, referenced_object_type_id: 't1' },
      { id: 'a4', name: 'Tie', is_locked: true, sort: 2 },
      { id: 'a5', name: 'MissingSort', is_locked: true },
    ]
    const out = collectUnsetLockedAttributes({ attributes, values: [], refs: [] })
    // Missing sort counts as 0; the equal-sort pair keeps the OTA list order.
    assert.deepEqual(out.scalar.map((a) => a.id), ['a5', 'a2', 'a1', 'a4'])
    assert.deepEqual(out.reference.map((a) => a.id), ['a3'])
  })
})

describe('mergeValueAndUnsetRows (ordered value + unset row merge)', () => {
  const attributes = [
    { id: 'a3', name: 'Three', sort: 3 },
    { id: 'a5', name: 'Five', sort: 5 },
  ]

  it('places an unset row by sort between and after value rows', () => {
    const values = [
      { id: 'v3', object_type_attribute_id: 'a3' },
      { id: 'v5', object_type_attribute_id: 'a5' },
    ]
    const unset = [{ id: 'a4', name: 'Four', sort: 4, is_locked: true }]
    const out = mergeValueAndUnsetRows(values, unset, attributes)
    assert.deepEqual(out.map((entry) => entry.kind), ['value', 'unset', 'value'])
    assert.equal(out[0].value.id, 'v3')
    assert.equal(out[1].attribute.id, 'a4')
    assert.equal(out[2].value.id, 'v5')
  })

  it('keeps the value row ahead of an unset row with the same sort', () => {
    const values = [{ id: 'v5', object_type_attribute_id: 'a5' }]
    const unset = [{ id: 'a9', name: 'Nine', sort: 5, is_locked: true }]
    const out = mergeValueAndUnsetRows(values, unset, attributes)
    assert.deepEqual(out.map((entry) => entry.kind), ['value', 'unset'])
  })

  it('sorts a value whose attribute is not in the list as 0', () => {
    const values = [{ id: 'vx', object_type_attribute_id: 'missing' }]
    const unset = [{ id: 'a3', name: 'Three', sort: 3, is_locked: true }]
    const out = mergeValueAndUnsetRows(values, unset, attributes)
    assert.deepEqual(out.map((entry) => entry.kind), ['value', 'unset'])
  })

  it('keeps the incoming order of value rows that share a sort', () => {
    const shared = [
      { id: 'a1', name: 'One', sort: 2 },
      { id: 'a2', name: 'Two', sort: 2 },
    ]
    const values = [
      { id: 'v2', object_type_attribute_id: 'a2' },
      { id: 'v1', object_type_attribute_id: 'a1' },
    ]
    const out = mergeValueAndUnsetRows(values, [], shared)
    assert.deepEqual(out.map((entry) => entry.value.id), ['v2', 'v1'])
  })

  it('tolerates undefined inputs', () => {
    assert.deepEqual(mergeValueAndUnsetRows(undefined, undefined, undefined), [])
  })
})

describe('normalizeManagedBy (tooltip owner normalization)', () => {
  it('returns null for null, undefined, empty, whitespace-only and non-string input', () => {
    assert.equal(normalizeManagedBy(null), null)
    assert.equal(normalizeManagedBy(undefined), null)
    assert.equal(normalizeManagedBy(''), null)
    assert.equal(normalizeManagedBy('   '), null)
    assert.equal(normalizeManagedBy(42), null)
  })

  it('returns the trimmed owner for a padded identifier', () => {
    assert.equal(normalizeManagedBy(' jira '), 'jira')
  })

  it('returns a 64-character identifier unchanged (no truncation)', () => {
    const long = 'sdm290-connector-' + 'a'.repeat(47)
    assert.equal(long.length, 64)
    assert.equal(normalizeManagedBy(long), long)
  })
})

describe('computeLockTooltipPosition (tooltip clamp and flip)', () => {
  const viewport = { width: 1000, height: 800 }
  const tooltip = { width: 256, height: 40 }
  const anchor = { top: 300, bottom: 316, left: 500, width: 16, height: 16 }

  it('centers the tooltip above the anchor when it fits', () => {
    const pos = computeLockTooltipPosition({ anchor, tooltip, viewport })
    assert.deepEqual(pos, { left: 380, top: 254, placement: 'top' })
  })

  it('clamps the tooltip to the left viewport edge', () => {
    const leftAnchor = { top: 300, bottom: 316, left: 12, width: 16, height: 16 }
    const pos = computeLockTooltipPosition({ anchor: leftAnchor, tooltip, viewport })
    assert.equal(pos.left, 8)
  })

  it('clamps the tooltip to the right viewport edge', () => {
    const rightAnchor = { top: 300, bottom: 316, left: 990, width: 16, height: 16 }
    const pos = computeLockTooltipPosition({ anchor: rightAnchor, tooltip, viewport })
    assert.equal(pos.left, 1000 - 8 - 256)
  })

  it('flips below the anchor when there is no room above', () => {
    const topAnchor = { top: 10, bottom: 26, left: 500, width: 16, height: 16 }
    const pos = computeLockTooltipPosition({ anchor: topAnchor, tooltip, viewport })
    assert.equal(pos.placement, 'bottom')
    assert.equal(pos.top, 26 + 6)
  })

  it('never intersects the anchor in a fitted placement', () => {
    const above = computeLockTooltipPosition({ anchor, tooltip, viewport })
    assert.ok(above.top + tooltip.height <= anchor.top)
    const belowAnchor = { top: 10, bottom: 26, left: 500, width: 16, height: 16 }
    const below = computeLockTooltipPosition({ anchor: belowAnchor, tooltip, viewport })
    assert.ok(below.top >= belowAnchor.bottom)
  })

  it('takes the roomier side and clamps into the viewport when neither side fits', () => {
    const shortViewport = { width: 400, height: 60 }
    const crowded = { top: 30, bottom: 46, left: 200, width: 16, height: 16 }
    const pos = computeLockTooltipPosition({
      anchor: crowded,
      tooltip: { width: 200, height: 40 },
      viewport: shortViewport,
    })
    assert.ok(pos.placement === 'top' || pos.placement === 'bottom')
    assert.ok(pos.top >= 8)
    assert.ok(pos.top <= Math.max(8, 60 - 8 - 40))
    assert.ok(Number.isFinite(pos.top) && Number.isFinite(pos.left))
  })

  it('clamps a tooltip wider than the viewport to the left margin', () => {
    const pos = computeLockTooltipPosition({
      anchor,
      tooltip: { width: 290, height: 40 },
      viewport: { width: 300, height: 800 },
    })
    assert.equal(pos.left, 8)
  })

  it('treats negative or missing sizes as zero and stays finite', () => {
    const zero = computeLockTooltipPosition({
      anchor: { top: 0, bottom: 0, left: 0, width: 0, height: 0 },
      tooltip: { width: 0, height: 0 },
      viewport: { width: 0, height: 0 },
    })
    assert.ok(Number.isFinite(zero.left) && Number.isFinite(zero.top))
    const negative = computeLockTooltipPosition({
      anchor: { top: 100, bottom: 100, left: 100, width: -4, height: -4 },
      tooltip: { width: -10, height: -10 },
      viewport,
    })
    assert.equal(negative.left, 100)
    assert.equal(negative.top, 94)
    assert.equal(negative.placement, 'top')
  })
})
describe('applyObjectRelease (per-object release projection)', () => {
  it('unlocks only the released attribute ids', () => {
    const attrs = [{ id: 'a1', is_locked: true }, { id: 'a2', is_locked: true }]
    const result = applyObjectRelease(attrs, ['a1'])
    assert.deepEqual(result, [{ id: 'a1', is_locked: false }, { id: 'a2', is_locked: true }])
    assert.equal(attrs[0].is_locked, true)
  })

  it('returns the input unchanged without a release', () => {
    const attrs = [{ id: 'a1', is_locked: true }]
    assert.equal(applyObjectRelease(attrs, []), attrs)
    assert.equal(applyObjectRelease(attrs, undefined), attrs)
  })
})
