import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { useObjectValues } from '../../shared/hooks/use-object-values'
import { useObjectRefs } from '../../shared/hooks/use-object-refs'
import { useReleasedAttributeIds } from '../../shared/hooks/use-objects'
import { applyObjectRelease } from './object-detail-lock'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import TypedField from './TypedField'
import ObjectReferenceSelector from './ObjectReferenceSelector'
import { LockIndicator } from './ObjectDetailPanel'
import { buildValuePayload, extractTypedValue } from '../../shared/lib/value-utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const SYSTEM_ATTR_NAMES = ['Created', 'Updated', 'Key']
const EMPTY = []

// 290 D-02/D-08: the server-projected effective lock is the authority — only
// is_locked===false proves an attribute writable, so a missing marker never
// infers an unlocked field.
const isAttrLocked = (attr) => attr?.is_locked !== false

export default function EditObjectModal({ object, attributes: typeAttributes, labelAttributeId, open, onClose, onSaved, isCreate, schemaId, attributeTypes = [], attributeTypesLoading = false, attributesLoading = false }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const formatDate = useFormatDate()
  const fetchId = !isCreate && open ? object?.id : null
  const { data: valuesRaw, isLoading: valuesLoading } = useObjectValues(fetchId)
  const { data: refsRaw, isLoading: refsLoading } = useObjectRefs(fetchId)
  const values = valuesRaw ?? EMPTY
  const refs = refsRaw ?? EMPTY
  const { data: releasedIds, isLoading: releasedLoading } = useReleasedAttributeIds(fetchId)
  const attributes = useMemo(
    () => applyObjectRelease(typeAttributes, releasedIds),
    [typeAttributes, releasedIds]
  )
  // AttributeType reference data (typeMap) must be fully loaded before the
  // form renders — otherwise typeMap[attr.attribute_type_id] misses on a
  // still-loading (or not-yet-in-the-first-page) AttributeType and every
  // resolvedType falls back to 'text', silently swapping a SELECT/STATUS/etc.
  // editor for a plain text input (bug found live while E2E-verifying UX-09,
  // Phase 229 Plan 04 — `attributeTypesLoading` closes the race).
  // 290 D-08/UI10: no edit affordance may appear before the effective-lock
  // metadata (the OTA projection) is known.
  const dataReady = (isCreate
    ? !attributeTypesLoading
    : (!valuesLoading && !refsLoading && !attributeTypesLoading && !releasedLoading)) && !attributesLoading
  // Memoized so the reset effect's deps stay stable: typeMap/editableAttributes
  // are derived data that must not be recreated on every render (that would
  // re-run the reset effect and clobber in-progress edits).
  const typeMap = useMemo(() => {
    const map = {}
    for (const at of attributeTypes) {
      map[at.id] = at.data_type
    }
    return map
  }, [attributeTypes])

  const isReferenceAttr = (attr) => Boolean(attr.referenced_object_type_id)

  // "Name" duplicates the dedicated name field; Created/Updated are system timestamps — exclude all from editable form.
  const editableAttributes = useMemo(
    () => attributes.filter((attr) => attr.name !== 'Name' && !SYSTEM_ATTR_NAMES.includes(attr.name)),
    [attributes]
  )
  // 290 D-05/D-08: locked attributes are rendered read-only and stay out of
  // every write loop, so a mixed-locked type can still save its unlocked fields.
  const unlockedEditableAttributes = useMemo(
    () => editableAttributes.filter((attr) => !isAttrLocked(attr)),
    [editableAttributes]
  )
  const systemAttributes = !isCreate ? attributes.filter((attr) => SYSTEM_ATTR_NAMES.includes(attr.name)) : []

  // 253 D-02b: the object type's label attribute takes the place of the
  // hardcoded "Name *" field. Resolve it defensively — `attributes` can be
  // missing the label OTA (the list is fetched with limit=50, the inheritance
  // CTE de-duplicates by attribute name so a child's own row can shadow the
  // parent row label_attribute_id points at, and hidden/permission-filtered
  // attributes never arrive at all). If we cannot render the label field, fall
  // back to the Name field rather than showing no name input whatsoever.
  const labelAttr = useMemo(
    () => (labelAttributeId ? attributes.find((a) => a.id === labelAttributeId) ?? null : null),
    [attributes, labelAttributeId]
  )
  const isNameLabel = !(labelAttr && labelAttr.name !== 'Name' && !SYSTEM_ATTR_NAMES.includes(labelAttr.name))

  // 290 D-02/D-08: the effective Name/label lock. When the matching OTA is not
  // in the loaded metadata there is no server-projected Name lock to consume
  // (no mirror target), so the field keeps its normal editability instead of
  // inventing a lock no marker describes.
  const nameAttr = useMemo(() => attributes.find((a) => a.name === 'Name') ?? null, [attributes])
  const nameFieldLocked = isNameLabel
    ? (nameAttr ? isAttrLocked(nameAttr) : false)
    : (labelAttr ? isAttrLocked(labelAttr) : false)
  const nameLockManagedBy = isNameLabel ? nameAttr?.managed_by : labelAttr?.managed_by

  const [formValues, setFormValues] = useState({})
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState({})
  const [name, setName] = useState('')

  useEffect(() => {
    if (open && dataReady) {
      setName(object?.name || '')
      const initial = {}
      for (const attr of editableAttributes) {
        if (isReferenceAttr(attr)) {
          const allRefs = refs.filter((r) => r.from_attribute_name === attr.name)
          const mc = attr.maximum_cardinality ?? -1
          if (mc < 0 || mc > 1) {
            initial[attr.id] = allRefs.map((r) => r.to_object_id)
          } else {
            initial[attr.id] = allRefs[0]?.to_object_id ?? null
          }
        } else {
          const attrType = typeMap[attr.attribute_type_id] || attr.type || 'text'
          if (attrType === 'select' && (attr.maximum_cardinality < 0 || attr.maximum_cardinality > 1)) {
            const allForAttr = values.filter((v) => v.object_type_attribute_id === attr.id)
            initial[attr.id] = allForAttr.map((v) => extractTypedValue(v, attrType)).filter(Boolean)
          } else {
            const existing = values.find((v) => v.object_type_attribute_id === attr.id)
            // 253 Pitfall 5: objects created before the label swap carry the
            // label only in the denormalized Object.name column, so seed the
            // label field from it when no value row exists — otherwise the
            // first save would blank the label out.
            const fallback = !isNameLabel && attr.id === labelAttributeId ? (object?.name ?? null) : null
            initial[attr.id] = existing ? extractTypedValue(existing, attrType) : fallback
          }
        }
      }
      setFormValues(initial)
      setErrors({})
    }
  }, [open, dataReady, object, attributes, values, refs, editableAttributes, typeMap, isNameLabel, labelAttributeId])

  // Compare a form value against the value the field was loaded with, so
  // handleSave only writes attributes the user actually touched — a blind
  // PATCH of every attribute on every save was resaving untouched fields,
  // producing "changed" audit log entries (and History rows) for values
  // that never differed from what was already stored.
  function normalizeForCompare(v) {
    return v === undefined || v === '' ? null : v
  }
  function valuesEqual(a, b) {
    const na = normalizeForCompare(a)
    const nb = normalizeForCompare(b)
    if (na === nb) return true
    if (na === null || nb === null) return false
    if (typeof na === 'object' && typeof nb === 'object') {
      return JSON.stringify(na) === JSON.stringify(nb)
    }
    return false
  }

  function sanitizeUrl(url) {
    if (!url || typeof url !== 'string') return null
    const trimmed = url.trim()
    if (!trimmed) return null
    try {
      const parsed = new URL(trimmed)
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        return trimmed
      }
      return null
    } catch {
      if (/^https?:\/\//i.test(trimmed)) return trimmed
      return null
    }
  }

  const handleChange = (attrId, val) => {
    setFormValues((prev) => ({ ...prev, [attrId]: val }))
  }

  const handleSave = async () => {
    // 290 D-08/UI09: a locked Name/label makes a create unable to produce a
    // valid object, so the button is disabled and this is pure defense.
    if (isCreate && nameFieldLocked) return
    setSaving(true)
    setErrors({})
    let objId = object?.id
    // 253 D-02c: with a foreign label the label field IS the name field —
    // Object.name is derived from its value. `??` alone would let '' through,
    // which the create path swallows via the New Object fallback but the edit
    // path would happily PATCH as an empty name.
    const derivedName = !isNameLabel ? (formValues[labelAttributeId] ?? name) : name
    let cachedSourceId = null
    const resolveSourceId = async () => {
      if (cachedSourceId) return cachedSourceId
      const res = await api.get(`/schemas/${object?.schema_id || schemaId}`)
      cachedSourceId = res.data.source_id
      return cachedSourceId
    }
    try {
      if (isCreate) {
        const res = await api.post('/objects/', {
          schema_id: schemaId,
          object_type_id: object.object_type_id,
           name: derivedName || t('browse.objects.new_object', 'New Object'),
        })
        objId = res.data.id
      } else {
        const objectPayload = {}
        if (!nameFieldLocked && derivedName && derivedName !== object?.name) objectPayload.name = derivedName
        if (Object.keys(objectPayload).length > 0) {
          await api.patch(`/objects/${objId}`, objectPayload)
        }
      }

      // 290 D-05/D-08: locked attributes never reach a write payload — only the
      // unlocked subset is processed, so mixed-locked types stay saveable.
      for (const attr of unlockedEditableAttributes) {
        const val = formValues[attr.id]

        if (isReferenceAttr(attr)) {
          const existingRefs = refs.filter((r) => r.from_attribute_name === attr.name)
          const mc = attr.maximum_cardinality ?? -1
          const isMultiRef = mc < 0 || mc > 1

          if (isMultiRef) {
            const newIds = new Set((Array.isArray(val) ? val : []).filter(Boolean))
            const existingIds = new Set(existingRefs.map((r) => r.to_object_id))
            for (const er of existingRefs) {
              if (!newIds.has(er.to_object_id)) {
                await api.delete(`/object-refs/${er.id}`).catch(() => {})
              }
            }
            for (const newId of newIds) {
              if (!existingIds.has(newId)) {
                await api.post('/object-refs/', {
                  from_object_id: objId,
                  to_object_id: newId,
                  object_type_id: object.object_type_id,
                  source_id: await resolveSourceId(),
                  from_attribute_name: attr.name,
                })
              }
            }
          } else {
            const currentVal = existingRefs[0]?.to_object_id ?? null
            if (val === currentVal) continue
            if (val) {
              if (existingRefs.length > 0) {
                await api.patch(`/object-refs/${existingRefs[0].id}`, { to_object_id: val })
                for (const dup of existingRefs.slice(1)) {
                  await api.delete(`/object-refs/${dup.id}`).catch(() => {})
                }
              } else {
                await api.post('/object-refs/', {
                  from_object_id: objId,
                  to_object_id: val,
                  object_type_id: object.object_type_id,
                  source_id: await resolveSourceId(),
                  from_attribute_name: attr.name,
                })
              }
            } else {
              for (const r of existingRefs) {
                await api.delete(`/object-refs/${r.id}`).catch(() => {})
              }
            }
          }
          continue
        }

        const type = typeMap[attr.attribute_type_id] || attr.type || 'text'

        // Multi-value: save each selected option as a separate object-value
        if (type === 'select' && (attr.maximum_cardinality < 0 || attr.maximum_cardinality > 1) && Array.isArray(val)) {
          const existingVals = values.filter((v) => v.object_type_attribute_id === attr.id)
          const valSet = new Set(val.filter(Boolean).map(String))

          for (const ev of existingVals) {
            const evSimple = ev.value_simple
            if (!valSet.has(evSimple)) {
              await api.delete(`/object-values/${ev.id}`).catch(() => {})
            }
          }
          for (const vStr of valSet) {
            const exists = existingVals.find((ev) => ev.value_simple === vStr)
            if (!exists) {
              await api.post('/object-values/', {
                object_id: objId,
                object_type_attribute_id: attr.id,
                value_simple: vStr,
              })
            }
          }
          continue
        }

        // URL XSS prevention — reject non-http(s) protocols
        if (type === 'url' && val) {
          const sanitized = sanitizeUrl(val)
          if (!sanitized) {
             setErrors(prev => ({ ...prev, [attr.id]: t('browse.validation.valid_url', 'Enter a valid URL starting with http:// or https://') }))
            setSaving(false)
            return
          }
          formValues[attr.id] = sanitized
        }

        // Email basic validation
        if (type === 'email' && val && typeof val === 'string') {
          if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val)) {
             setErrors(prev => ({ ...prev, [attr.id]: t('browse.validation.valid_email', 'Enter a valid email address') }))
            setSaving(false)
            return
          }
        }

        const existing = values.find((v) => v.object_type_attribute_id === attr.id)
        const initialVal = existing ? extractTypedValue(existing, type) : null
        if (valuesEqual(val, initialVal)) continue

        if (existing) {
          if (val === null || val === undefined || val === '') {
            await api.delete(`/object-values/${existing.id}`).catch(() => {})
          } else {
            const payload = buildValuePayload(typeMap[attr.attribute_type_id] || attr.type || 'text', val)
            await api.patch(`/object-values/${existing.id}`, payload)
          }
        } else if (val !== null && val !== undefined && val !== '') {
          try {
            const res = await api.get('/object-values/', { params: { object_id: objId } })
            const all = Array.isArray(res.data) ? res.data : []
            const match = all.find((v) => v.object_type_attribute_id === attr.id)
            if (match) {
              await api.patch(`/object-values/${match.id}`, buildValuePayload(typeMap[attr.attribute_type_id] || attr.type || 'text', val))
              for (const dup of all.filter((v) => v.object_type_attribute_id === attr.id).slice(1)) {
                await api.delete(`/object-values/${dup.id}`).catch(() => {})
              }
              continue
            }
          } catch {}
          await api.post('/object-values/', {
            object_id: objId,
            object_type_attribute_id: attr.id,
            ...buildValuePayload(typeMap[attr.attribute_type_id] || attr.type || 'text', val),
          })
        }
      }

      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['object-refs'] }),
        queryClient.invalidateQueries({ queryKey: ['object-values'] }),
        queryClient.invalidateQueries({ queryKey: ['objects'] }),
        queryClient.invalidateQueries({ queryKey: ['audit-logs'] }),
        queryClient.invalidateQueries({ queryKey: ['object-graph'] }),
      ])
      onSaved(objId)
      onClose()
    } catch (err) {
      // 290 D-08/UI11: a stale 409 keeps the modal and untouched fields intact
      // and replaces the raw server detail with the localized reload
      // instruction — the locked write is never retried or swallowed.
      if (err.response?.status === 409) {
        setErrors({ _form: t('browse.lock.stale_conflict', 'This value is externally managed. Reload the object.') })
        return
      }
      const detail = err.response?.data?.detail
      if (typeof detail === 'string') setErrors({ _form: detail })
      else if (Array.isArray(detail)) {
        const newErrors = {}
        detail.forEach((d) => {
          const field = d.loc?.slice(-1)[0]
          if (field) newErrors[field] = d.msg
        })
        setErrors(newErrors)
      } else {
         setErrors({ _form: t('browse.errors.save_failed', 'Save failed') })
      }
    } finally {
      setSaving(false)
    }
  }

  // 290 D-08: read-only rendering for a locked scalar — the current persisted
  // value (or —) with existing wrap behavior; never an editable widget.
  function lockedScalarDisplay(attr, baseType) {
    const attrType = attr.structure?.status_options ? 'status' : baseType
    const existing = values.find((v) => v.object_type_attribute_id === attr.id)
    const raw = existing ? extractTypedValue(existing, attrType) : null
    if (attrType === 'boolean') {
      return raw === true ? t('common.yes_marked', '✓ Yes') : (raw === false ? t('common.no_marked', '— No') : '—')
    }
    if (raw === null || raw === undefined || raw === '') return '—'
    if (attrType === 'status') return typeof raw === 'object' ? (raw?.name || '—') : String(raw)
    if (attrType === 'datetime') return formatDate(raw)
    if (typeof raw === 'object') return raw.name ?? raw.value_simple ?? JSON.stringify(raw)
    return String(raw)
  }

  function lockedReferenceDisplay(attr) {
    const names = refs
      .filter((r) => r.from_attribute_name === attr.name)
      .map((r) => r.to_object_name || r.to_object_id)
    return names.length > 0 ? names.join(', ') : '—'
  }

  if (!open) return null

  const sortedAttrs = [...editableAttributes].sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0))
  // The label field stands in for "Name *", so it belongs where that field was:
  // first.
  if (!isNameLabel) {
    const labelIdx = sortedAttrs.findIndex((a) => a.id === labelAttributeId)
    if (labelIdx > 0) sortedAttrs.unshift(sortedAttrs.splice(labelIdx, 1)[0])
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/50" onClick={onClose} />
      <div data-testid="edit-object-modal" className="relative z-50 w-full max-w-3xl max-h-[85vh] overflow-y-auto rounded-lg border bg-card shadow-lg">
        <div className="sticky top-0 bg-card border-b px-6 py-4">
          <h2 className="text-lg font-semibold">
             {isCreate ? t('browse.objects.create_title', 'Create New Object') : interpolateDefault(t('browse.objects.edit_title', 'Edit {{name}}'), { name: object?.name || '' })}
          </h2>
        </div>

        <div className="px-6 py-4 space-y-4">
          {!dataReady ? (
            <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">{t('common.loading', 'Loading…')}</div>
          ) : null}
          <div className={`space-y-4 ${!dataReady ? 'hidden' : ''}`}>
          {isNameLabel && (nameFieldLocked ? (
            <div className="space-y-1.5" data-testid="locked-name-field">
              <span className="text-sm font-medium text-foreground inline-flex items-center gap-1.5">
                {t('common.name_required', 'Name *')}
                <LockIndicator managedBy={nameLockManagedBy} />
              </span>
              <div data-testid="locked-name-value" className="text-sm text-muted-foreground break-words">
                {isCreate ? '—' : (object?.name || '—')}
              </div>
            </div>
          ) : (
            <div className="space-y-1.5">
               <label className="text-sm font-medium text-foreground">{t('common.name_required', 'Name *')}</label>
              <input
                type="text"
                data-testid="object-name-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                required
              />
            </div>
          ))}

          {sortedAttrs.map((attr) => {
            const resolvedType = attr.structure?.status_options ? 'STATUS' : (typeMap[attr.attribute_type_id] || attr.type || 'text')
            // 253 D-02b: the label field replaces the "Name *" input, which was
            // always `required` — carry that over so swapping the field does not
            // quietly drop the constraint.
            const isLabelField = !isNameLabel && attr.id === labelAttributeId
            const attrWithType = { ...attr, type: resolvedType, is_required: attr.is_required || isLabelField }
            // 290 D-08/UI09: locked fields render the current value (or —)
            // read-only with their neutral lock instead of an input, and are
            // omitted from the save loop entirely.
            if (isAttrLocked(attr)) {
              const isRef = isReferenceAttr(attr)
              const min = attr.minimum_cardinality ?? 0
              const max = attr.maximum_cardinality ?? -1
              let cardinalityText = ''
              if (min > 0 && max === -1) cardinalityText = `min ${min}`
              else if (max >= 0) cardinalityText = `${min}..${max}`
              return (
                <div key={attr.id} className="space-y-1.5" data-testid="locked-attribute-field">
                  <span className="text-sm font-medium text-foreground inline-flex items-center gap-2">
                    {attr.name}
                    <LockIndicator managedBy={attr.managed_by} />
                    {isRef && cardinalityText && (
                      <span className="text-xs text-muted-foreground bg-muted/50 rounded px-1.5 py-0.5">
                        {cardinalityText}
                      </span>
                    )}
                  </span>
                  {isRef ? (
                    <div data-testid="locked-attribute-ref" className="text-sm text-muted-foreground break-words">
                      {lockedReferenceDisplay(attr)}
                    </div>
                  ) : (
                    <div data-testid="locked-attribute-value" className="text-sm text-muted-foreground whitespace-pre-wrap break-words">
                      {lockedScalarDisplay(attr, typeMap[attr.attribute_type_id] || attr.type || 'text')}
                    </div>
                  )}
                </div>
              )
            }
            if (isReferenceAttr(attr)) {
              const min = attr.minimum_cardinality ?? 0
              const max = attr.maximum_cardinality ?? -1
              let cardinalityText = ''
              if (min > 0 && max === -1) cardinalityText = `min ${min}`
              else if (max >= 0) cardinalityText = `${min}..${max}`
              return (
                <div key={attr.id} className="space-y-1.5">
                  <label className="text-sm font-medium text-foreground inline-flex items-center gap-2">
                    {attr.name}
                    {cardinalityText && (
                      <span className="text-xs text-muted-foreground bg-muted/50 rounded px-1.5 py-0.5">
                        {cardinalityText}
                      </span>
                    )}
                  </label>
                  <ObjectReferenceSelector
                    referencedObjectTypeId={attr.referenced_object_type_id || null}
                    structure={attr.structure || null}
                    value={formValues[attr.id] ?? ((attr.maximum_cardinality ?? -1) < 0 || (attr.maximum_cardinality ?? -1) > 1 ? [] : null)}
                    onChange={(v) => handleChange(attr.id, v)}
                    isMulti={(attr.maximum_cardinality ?? -1) < 0 || (attr.maximum_cardinality ?? -1) > 1}
                  />
                </div>
              )
            }
            return (
              <TypedField
                key={attr.id}
                attribute={attrWithType}
                value={formValues[attr.id] ?? null}
                onChange={(v) => handleChange(attr.id, v)}
                error={errors[attr.id] || null}
              />
            )
          })}

          {systemAttributes.length > 0 && (
            <div className="border-t pt-3 space-y-1.5">
              {systemAttributes.map(attr => {
                const attrType = typeMap[attr.attribute_type_id] || 'text'
                const v = values.find(val => val.object_type_attribute_id === attr.id)
                const raw = v ? (attrType === 'datetime'
                  ? (v.value_datetime ?? v.value_simple ?? null)
                  : (v.value_simple ?? null)
                ) : null
                let display = '—'
                if (raw !== null && raw !== undefined) {
                  if (attrType === 'datetime') {
                    display = formatDate(raw)
                  } else {
                    display = String(raw)
                  }
                }
                return (
                  <div key={attr.id} className="flex items-center justify-between text-sm">
                    <span className="text-muted-foreground">{attr.name}</span>
                    <span className="text-muted-foreground font-mono text-xs">{display}</span>
                  </div>
                )
              })}
            </div>
          )}

          {errors._form && (
            <div data-testid="object-modal-form-error" className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {errors._form}
            </div>
          )}
          </div>
        </div>

        <div className="sticky bottom-0 bg-card border-t px-6 py-4 flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            size="md"
            onClick={onClose}
            disabled={saving}
          >
             {t('common.cancel', 'Cancel')}
          </Button>
          <Button
            type="button"
            data-testid="object-modal-save"
            variant="primary"
            size="md"
            onClick={handleSave}
            disabled={saving || (isCreate && nameFieldLocked)}
            loading={saving}
          >
             {t('browse.save_object_changes', 'Save Object Changes')}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
