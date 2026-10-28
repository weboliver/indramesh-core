import { useState, useMemo, useCallback, memo, lazy, Suspense, useEffect, useLayoutEffect, useRef } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQueryClient, useMutation } from '@tanstack/react-query'
import { useObject, useReleasedAttributeIds } from '../../shared/hooks/use-objects'
import { useObjectValues } from '../../shared/hooks/use-object-values'
import { useObjectRefs, useInboundRefs } from '../../shared/hooks/use-object-refs'
import { useObjectTypeAttributes, useSystemSettings } from '../../shared/hooks/use-other-resources'
import { useAttributeTypes } from '../../shared/hooks/use-attribute-types'
import { useObjectTypes } from '../../shared/hooks/use-object-types'
import { useSchema } from '../../shared/hooks/use-schemas'
import { getLanguageName } from '../../shared/hooks/use-active-language'
import api from '../../shared/hooks/use-api'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { FallbackBadge, isFallingBack } from '../../shared/components/FallbackBadge'
import TranslationPanel from '../../shared/components/TranslationPanel'
import { extractTypedValue, buildValuePayload } from '../../shared/lib/value-utils'
import TableSkeleton from '../../shared/components/TableSkeleton'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { filterInboundGroups } from './object-detail-panel-search'
import { applyObjectRelease, collectUnsetLockedAttributes, mergeValueAndUnsetRows, normalizeManagedBy, computeLockTooltipPosition } from './object-detail-lock'
import InlineCellValue from './InlineCellValue'
import InlineCellEditor from './InlineCellEditor'
import Boxes from 'lucide-react/dist/esm/icons/boxes'
import PermissionsDialog from '../permissions/PermissionsDialog'
import { Can, useCan } from '../../shared/components/Can'
import ObjectCommentsSection from './ObjectCommentsSection'
import ObjectFilesSection from './ObjectFilesSection'
import ObjectHistorySection from './ObjectHistorySection'
import { PluginSectionRegion } from '../../shared/plugin-runtime/PluginSections'
import { PluginItemBadges, usePluginItemBadges } from '../../shared/plugin-runtime/PluginItemBadges'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import LoadingSpinner from '../../shared/components/LoadingSpinner'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

// D-12: ObjectGraphModal (and its @xyflow/react + dagre bundle) must not
// download on the Browse route until the user opens it — see the graphOpen
// gate at the render site below (Pitfall 1: an unconditional render still
// fires the dynamic import on mount even with lazy()).
const ObjectGraphModal = lazy(() => import('./ObjectGraphModal'))

// D-13: the Suspense fallback shown while the ObjectGraphModal chunk loads.
// Mirrors ObjectGraphModal.jsx's outer markup (overlay/dialog/header/content
// area) and testids byte-for-byte so object-graph.spec.js's openGraphModal
// helper (which asserts [data-testid="graph-modal"] visible right after the
// click) never races the chunk load. ObjectGraphModal.jsx itself stays
// untouched (D-16).
function GraphModalShell({ onClose }) {
  const { t } = useTranslation()
  // Escape-close, mirroring ObjectGraphModal.jsx's own keydown effect
  // (:62-67) — the fallback can legitimately be the visible surface for a
  // noticeable window under real chunk-load latency, so Escape must work
  // here too, not only once the real modal has mounted (D-16 parity).
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center" onClick={onClose}>
      <div
        className="relative z-50 w-[640px] max-w-[calc(100vw-2rem)] max-h-[85vh] rounded-lg border bg-card shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t('browse.graph.view_object_graph', 'View Object Graph')}
        data-testid="graph-modal"
      >
        <div className="sticky top-0 z-10 bg-card border-b px-6 py-2 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t('browse.graph.view_object_graph', 'View Object Graph')}</h2>
          <Button variant="ghost" size="icon" className="h-8 w-8" leftIcon="x" aria-label={t('common.close', 'Close')} data-testid="graph-modal-close" onClick={onClose} />
        </div>
        <div className="h-[480px] w-full">
          <LoadingSpinner className="h-full" />
        </div>
      </div>
    </div>
  )
}

function ReferenceTypeBadge({ referenceType }) {
  if (!referenceType) return null
  const hex = (referenceType.color || '#6B7280').replace('#', '')
  const r = parseInt(hex.substring(0, 2), 16) / 255
  const g = parseInt(hex.substring(2, 4), 16) / 255
  const b = parseInt(hex.substring(4, 6), 16) / 255
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  const textColor = luminance < 0.4 ? '#FFFFFF' : '#1F2937'
  return (
    <span
      className="inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium"
      style={{ backgroundColor: referenceType.color || '#6B7280', color: textColor }}
    >
      {referenceType.reference}
    </span>
  )
}

const SYSTEM_ATTR_NAMES = new Set(['Key', 'Created', 'Updated'])
// 'select' intentionally excluded — see the matching comment in
// InlineCellEditor.jsx (this file's translate button shares the same bug:
// no option-label lookup at the value-translation layer).
const MULTILINGUAL_ELIGIBLE_TYPES = new Set(['text', 'status'])

// 290 D-08: the neutral read-only cue for an externally managed value — the
// value/link itself stays the primary visual anchor and the lock is a
// secondary, keyboard-focusable icon (never an action). Fixed positioning
// keeps the tooltip from being clipped by the table's overflow shell; the
// position is measured and clamped/flipped so the tooltip stays inside the
// viewport next to its anchor even with a long connector identifier.
const LOCK_TOOLTIP_MARGIN = 8
const LOCK_TOOLTIP_GAP = 6

export function LockIndicator({ managedBy }) {
  const { t } = useTranslation()
  const [anchorRect, setAnchorRect] = useState(null)
  const [position, setPosition] = useState(null)
  const anchorRef = useRef(null)
  const tooltipRef = useRef(null)
  const owner = normalizeManagedBy(managedBy)
  const message = owner
    ? interpolateDefault(t('browse.lock.externally_managed_by', 'Externally managed by {{managedBy}}'), { managedBy: owner })
    : t('browse.lock.externally_managed', 'Externally managed')

  const showTooltip = () => {
    const rect = anchorRef.current?.getBoundingClientRect()
    if (!rect) return
    setAnchorRect(rect)
    setPosition(null)
  }
  const hideTooltip = () => {
    setAnchorRect(null)
    setPosition(null)
  }

  // Measure the rendered tooltip (hidden pass at the widest allowed width)
  // before paint, then place it from the measured box. The anchor box is read
  // live at effect time: the focus/mouse event snapshot can predate the
  // committed layout (the tooltip's own first render can shift the anchor by
  // its padding), and a stale anchor box would let the tooltip cover it.
  useLayoutEffect(() => {
    if (!anchorRect || !tooltipRef.current) return
    const rect = tooltipRef.current.getBoundingClientRect()
    const liveAnchor = anchorRef.current?.getBoundingClientRect() ?? anchorRect
    const viewport = {
      width: document.documentElement.clientWidth || window.innerWidth,
      height: document.documentElement.clientHeight || window.innerHeight,
    }
    setPosition(computeLockTooltipPosition({
      anchor: liveAnchor,
      tooltip: { width: rect.width, height: rect.height },
      viewport,
      margin: LOCK_TOOLTIP_MARGIN,
      gap: LOCK_TOOLTIP_GAP,
    }))
  }, [anchorRect])

  // Any scroll in any container (capture phase) or a window resize closes the
  // tooltip, so it can never float detached from its anchor.
  useEffect(() => {
    if (!anchorRect) return undefined
    const hide = () => {
      setAnchorRect(null)
      setPosition(null)
    }
    window.addEventListener('scroll', hide, true)
    window.addEventListener('resize', hide)
    return () => {
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('resize', hide)
    }
  }, [anchorRect])

  return (
    <span
      className="relative inline-flex shrink-0 items-center"
      onMouseEnter={showTooltip}
      onMouseLeave={hideTooltip}
    >
      <span
        ref={anchorRef}
        data-testid="lock-indicator"
        tabIndex={0}
        aria-label={message}
        onFocus={showTooltip}
        onBlur={hideTooltip}
        className="inline-flex items-center rounded text-muted-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <LucideIcon name="lock-keyhole" size={16} aria-hidden="true" />
      </span>
      {anchorRect && (
        <span
          ref={tooltipRef}
          role="tooltip"
          data-testid="lock-tooltip"
          style={position
            ? { left: position.left, top: position.top }
            : { left: 0, top: 0, visibility: 'hidden' }}
          className="pointer-events-none fixed z-[100] max-w-[min(16rem,calc(100vw-1rem))] [overflow-wrap:anywhere] whitespace-normal rounded-md border bg-muted px-2 py-1 text-xs text-muted-foreground shadow-sm"
        >
          {message}
        </span>
      )}
    </span>
  )
}

const ObjectDetailPanel = memo(function ObjectDetailPanel({ objectId, objectTypeId, onEdit, onDelete, lang, languageList, readOnly = false }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [editingValueId, setEditingValueId] = useState(null)
  const [permDialogOpen, setPermDialogOpen] = useState(false)
  const [graphOpen, setGraphOpen] = useState(false)
  const [translationPanel, setTranslationPanel] = useState(null)
  const [searchParams, setSearchParams] = useSearchParams()

  // D-02 standard search adapter for both local loaded-list filter surfaces
  // (manifest #08 Attributes & References, #09 Referenced By): URL-synced q,
  // preserves unrelated params, resets page to 1. The loaded rows of the two
  // tables are filtered in memory — no backend search contract.
  const panelQ = searchParams.get('q') || ''
  const setPanelQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const { data: obj, isLoading: objLoading } = useObject(objectId, lang)
  const { data: values = [] } = useObjectValues(objectId, lang)
  const { data: refs = [] } = useObjectRefs(objectId)
  const { data: inboundRefs = [], isPending: inboundRefsPending } = useInboundRefs(objectId)
  const { data: typeAttributes } = useObjectTypeAttributes(objectTypeId)
  const { data: releasedIds } = useReleasedAttributeIds(objectId)
  const attributes = useMemo(
    () => applyObjectRelease(typeAttributes ?? [], releasedIds),
    [typeAttributes, releasedIds]
  )
  const { data: attributeTypes = [] } = useAttributeTypes()
  const { data: allObjectTypes = [] } = useObjectTypes(obj?.schema_id)
  const { data: schema } = useSchema(obj?.schema_id)
  const defaultLanguageCode = schema?.default_language_code
  const { data: settings = {} } = useSystemSettings()
  const canReadHistory = useCan('audit-log', 'read')
  // Defense-in-depth (D-16 pattern): duplicates the exact check
  // ObjectHistorySection's own default export already performs internally,
  // so the parent's tab-button visibility and the child's own content
  // gating agree independently.
  const showHistoryTab = settings.history_enabled === 'true' && canReadHistory

  // `values` above is fetched WITH `lang` — while previewing/viewing a
  // non-default language its raw value fields already hold the translated
  // text, not the original. The TranslationPanel's read-only "Default"
  // field must always show the true base-language value regardless of what
  // language is currently on screen, so it needs its own base-language
  // fetch to seed from (dedupes for free with `values` when lang is already
  // the default — same query key, react-query shares the cache entry).
  const { data: defaultValues = [] } = useObjectValues(objectId, defaultLanguageCode)
  const defaultValueById = useMemo(() => {
    const map = new Map()
    for (const dv of defaultValues) map.set(dv.id, dv)
    return map
  }, [defaultValues])

  // FEEXT-01/S2: the attribute-kind batch resolves before the component's early
  // returns (rules of hooks). Ids come from the same data the rendered rows use
  // — value rows, reference rows (matched by attribute name) and the explicitly
  // locked unset rows — tolerating not-yet-loaded data (empty -> no call). The
  // hook de-duplicates and sorts, so a superset here is harmless.
  const attributeIds = useMemo(() => {
    const ids = []
    for (const v of values) if (v.object_type_attribute_id) ids.push(v.object_type_attribute_id)
    const refNames = new Set((refs || []).map((r) => r.from_attribute_name).filter(Boolean))
    refNames.forEach((name) => {
      const attr = attributes.find((a) => a.name === name)
      if (attr?.id) ids.push(attr.id)
    })
    const { scalar, reference } = collectUnsetLockedAttributes({ attributes, values, refs })
    for (const attr of scalar) ids.push(attr.id)
    for (const attr of reference) ids.push(attr.id)
    return ids
  }, [values, attributes, refs])
  const attributeBadgeContext = useMemo(() => ({ objectId }), [objectId])
  const attributeBadges = usePluginItemBadges('attribute', attributeIds, attributeBadgeContext)

  // Shared tab-strip state driving Comments/Files/History as mutually
  // exclusive controlled panels (null | 'comments' | 'files' | 'history').
  // D-16/CR-01 parity (deviation, Rule 1): the state is keyed by the Object
  // id the open tab belongs to, not a bare section string reset via a
  // useLayoutEffect — a plain boolean/string reset-on-effect still let the
  // stale "open" value flow through for the ONE render where the parent has
  // already switched objectId but the reset effect has not yet run, and
  // React Query starts its fetch during render — firing an unwanted request
  // for the newly selected Object's list before the reset could take effect
  // (proven by object-comments.spec.js's 280-03 in-place-switch regression
  // test). Deriving `activeSection` from identity closes the gate in that
  // same render, exactly mirroring ObjectCommentsSection's own original
  // `expandedObjectId === objectId` fix.
  const [activeSectionState, setActiveSectionState] = useState({ forObjectId: objectId, section: 'comments' })
  const activeSection = activeSectionState.forObjectId === objectId ? activeSectionState.section : 'comments'
  // Explicit reset when leaving an Object (not merely the render-time
  // derivation above): without this, returning to an Object that was
  // previously expanded would read its old `forObjectId === objectId` match
  // as still-true, re-expanding a tab the user never re-opened. Mirrors
  // ObjectCommentsSection's original prevObjectIdRef reset effect.
  // Defaults to 'comments' (not null) on every Object switch — Comments is
  // the panel users check first after a sync/edit, so it opens by default.
  const prevDetailObjectIdRef = useRef(objectId)
  useLayoutEffect(() => {
    if (prevDetailObjectIdRef.current === objectId) return
    prevDetailObjectIdRef.current = objectId
    setActiveSectionState({ forObjectId: objectId, section: 'comments' })
  }, [objectId])

  function openTranslationPanel(v, effectiveType) {
    const baseValue = defaultValueById.get(v.id) ?? v
    const baseRawValue = extractTypedValue(baseValue, effectiveType)
    setTranslationPanel({
      entityId: v.id,
      fieldValues: { value: baseRawValue ?? '' },
    })
  }

  const typeMap = useMemo(() => {
    const map = {}
    for (const at of attributeTypes) {
      map[at.id] = at.data_type
    }
    return map
  }, [attributeTypes])

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => api.patch(`/object-values/${id}`, data).then(r => r.data),
    onSuccess: () => {
      // useObjectValues() keys its cache as ['object-values', { objectId, lang }] —
      // lang varies (global switcher / preview), so a targeted setQueryData at
      // { objectId } (no lang) silently misses every lang-scoped cache entry and
      // the UI never reflects the save. Invalidate the whole ['object-values']
      // prefix for this object instead, so every lang variant refetches.
      queryClient.invalidateQueries({ queryKey: ['object-values'], predicate: (q) => q.queryKey[1]?.objectId === objectId })
      queryClient.invalidateQueries({ queryKey: ['audit-logs'] })
      setEditingValueId(null)
    }
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => api.delete(`/object-values/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['object-values'], predicate: (q) => q.queryKey[1]?.objectId === objectId })
      queryClient.invalidateQueries({ queryKey: ['audit-logs'] })
      setEditingValueId(null)
    }
  })

  function getEffectiveDataType(attr) {
    if (!attr) return 'text'
    if (attr.structure?.status_options) return 'status'
    return typeMap[attr.attribute_type_id] || 'text'
  }

  async function handlePanelSave(v, attr, newValue) {
    const effectiveType = getEffectiveDataType(attr)
    const isEmpty = newValue === null || newValue === undefined || newValue === ''
    try {
      if (isEmpty) {
        await deleteMutation.mutateAsync(v.id)
      } else {
        await updateMutation.mutateAsync({ id: v.id, data: buildValuePayload(effectiveType, newValue) })
      }
    } catch (err) {
      // UI03: a stale 409 (the marker flipped while this editor was open) keeps
      // the editor open and shows the localized reload instruction instead of
      // the raw server detail. No client-side cancellation of the open editor.
      if (err?.response?.status === 409) {
        const message = t('browse.lock.stale_conflict', 'This value is externally managed. Reload the object.')
        const conflict = new Error(message)
        conflict.response = { data: { detail: message } }
        throw conflict
      }
      throw err
    }
  }

  if (!objectId) {
    return (
      <div className="rounded-lg border border-dashed p-12 text-center">
        <Boxes className="mx-auto h-12 w-12 text-muted-foreground" aria-hidden="true" />
        <h3 className="mt-4 text-lg font-semibold">{t('browse.detail.select_object', 'Select an Object')}</h3>
        <p className="mt-1 text-sm text-muted-foreground">{t('browse.detail.select_object_hint', 'Click an object in the list to view its details.')}</p>
      </div>
    )
  }

  if (objLoading) {
    return (
      <div className="rounded-lg border">
        <TableSkeleton rows={6} cols={1} />
      </div>
    )
  }

  if (!obj) {
    return <div className="rounded-lg border p-6 text-destructive">{t('browse.detail.object_not_found', 'Object not found')}</div>
  }

  const grouped = {}
  for (const r of refs) {
    const key = r.from_attribute_name || 'References'
    if (!grouped[key]) grouped[key] = []
    grouped[key].push(r)
  }

  const inboundByType = {}
  for (const r of inboundRefs) {
    const key = r.object_type_id
    if (!inboundByType[key]) inboundByType[key] = []
    inboundByType[key].push(r)
  }
  const inboundGrouped = Object.entries(inboundByType)
    .map(([typeId, refList]) => ({
      typeId,
      typeName: allObjectTypes.find(t => t.id === typeId)?.name || typeId,
      refs: [...refList].sort((a, b) => (a.from_object_name || '').localeCompare(b.from_object_name || '')),
    }))
    .sort((a, b) => a.typeName.localeCompare(b.typeName))

  // Deduplicate: one row per attribute (multi-select has multiple ObjectValues per attr)
  // Sort by attribute sort order first so the panel matches the configured order
  const valuesSorted = [...values].sort((a, b) => {
    const sa = attributes.find(x => x.id === a.object_type_attribute_id)?.sort ?? 0
    const sb = attributes.find(x => x.id === b.object_type_attribute_id)?.sort ?? 0
    return sa - sb
  })
  const seenAttrIds = new Set()
  const dedupedValues = []
  for (const v of valuesSorted) {
    if (!seenAttrIds.has(v.object_type_attribute_id)) {
      seenAttrIds.add(v.object_type_attribute_id)
      dedupedValues.push(v)
    }
  }

  // 290 D-02/D-08/UI05: an explicitly locked attribute without value or
  // reference still gets a read-only `—` row; the merged list interleaves
  // those rows with the existing value rows by the shared attribute sort.
  const { scalar: unsetScalar, reference: unsetReference } = collectUnsetLockedAttributes({ attributes, values, refs })
  const mergedRows = mergeValueAndUnsetRows(dedupedValues, unsetScalar, attributes)
  const renderedRowCount = mergedRows.length + Object.keys(grouped).length + unsetReference.length

  // Local loaded-list filtering of the inbound references surface (manifest
  // #09): the already-loaded rows are narrowed in memory by the shared
  // URL-synced q. Reference links, inline editing, badges, and panel
  // loading/error behavior are untouched — only rendered rows change.
  const filteredInboundGrouped = filterInboundGroups(inboundGrouped, panelQ)

  const toggleSection = (key) => setActiveSectionState((prev) => {
    const isSameOpen = prev.forObjectId === objectId && prev.section === key
    return isSameOpen ? { forObjectId: objectId, section: null } : { forObjectId: objectId, section: key }
  })
  const detailTabs = [
    { key: 'comments', label: t('browse.comments.title', 'Comments') },
    { key: 'files', label: t('browse.files.title', 'Files') },
    ...(showHistoryTab ? [{ key: 'history', label: t('browse.history.title', 'History') }] : []),
  ]

  return (
    <div className="space-y-4">
      <div className="rounded-lg border p-6 space-y-4">
        <div className="flex items-center justify-end gap-2">
          <Button variant="outline" size="icon" className="h-8 w-8" leftIcon="network"
            aria-label={t('browse.graph.view_object_graph', 'View Object Graph')} title={t('browse.graph.view_object_graph', 'View Object Graph')} data-testid="graph-button"
            onClick={() => setGraphOpen(true)} />
          <Can resource="roles" action="manage">
            <Button variant="outline" size="icon" className="h-8 w-8" leftIcon="shield"
              aria-label={t('common.permissions', 'Permissions')} title={t('common.permissions', 'Permissions')} data-testid="permissions-button"
              disabled={readOnly}
              onClick={() => setPermDialogOpen(true)} />
          </Can>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={readOnly}
            onClick={() => onEdit?.(obj)}
          >
            {t('common.edit', 'Edit')}
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={readOnly}
            onClick={async () => { if (await confirmDialog({ message: interpolateDefault(t('browse.objects.delete_named_confirm', 'Delete "{{name}}"?'), { name: obj.name }), danger: true })) onDelete?.(obj.id) }}
          >
            {t('common.delete', 'Delete')}
          </Button>
        </div>
        <h2 className="text-2xl font-bold tracking-tight">{obj.name}</h2>
      </div>

      <div className="rounded-lg border p-6 space-y-3">
        <h3 className="text-lg font-semibold" data-testid="attributes-section">{selectPluralKey('browse.detail.attributes_references', renderedRowCount) === 'browse.detail.attributes_references_one'
          ? interpolateDefault(t('browse.detail.attributes_references_one', 'Attributes & References ({{count}})'), { count: renderedRowCount })
          : interpolateDefault(t('browse.detail.attributes_references_other', 'Attributes & References ({{count}})'), { count: renderedRowCount })}</h3>
        {values.length === 0 && refs.length === 0 && unsetScalar.length === 0 && unsetReference.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('browse.detail.no_attributes_references', 'No attributes or references.')}</p>
        ) : (
          <Table>
            <thead>
              <tr className="border-b">
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium w-1/3">{t('common.attribute', 'Attribute')}</th>
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.value', 'Value')}</th>
              </tr>
            </thead>
              <tbody>
                {mergedRows.map((entry) => {
                  if (entry.kind === 'unset') {
                    const unsetAttr = entry.attribute
                    return (
                      <tr key={`unset-${unsetAttr.id}`} className="border-b last:border-0 text-sm" data-testid="attribute-row">
                        <td className="px-4 py-3 text-muted-foreground">
                          <span className="inline-flex items-center gap-1">
                            {unsetAttr.name}
                            <PluginItemBadges badges={attributeBadges.get(unsetAttr.id)} />
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className="inline-flex items-center gap-2">
                            <span data-testid="unset-locked-value" className="text-muted-foreground">—</span>
                            <LockIndicator managedBy={unsetAttr.managed_by} />
                          </span>
                        </td>
                      </tr>
                    )
                  }
                  const v = entry.value
                  const attr = attributes.find(a => a.id === v.object_type_attribute_id)
                  const effectiveType = getEffectiveDataType(attr)
                  const rawValue = extractTypedValue(v, effectiveType)
                  const allValuesForAttr = values.filter(v2 => v2.object_type_attribute_id === v.object_type_attribute_id)
                  const isEditing = editingValueId === v.id
                  const isSystem = attr ? SYSTEM_ATTR_NAMES.has(attr.name) : false
                  // 290 D-02/D-08: only the server-projected is_locked===false
                  // proves a row editable — a missing marker (pending/absent
                  // metadata) never infers an unlocked row (UI05).
                  const lockKnown = !!attr && typeof attr.is_locked === 'boolean'
                  const isLocked = !lockKnown || attr.is_locked === true
                  const canEdit = !readOnly && !isSystem && !isLocked
                  const canTranslateValue = MULTILINGUAL_ELIGIBLE_TYPES.has(effectiveType) && !!attr?.multilingual && !!v.id
                  return (
                    <tr key={v.object_type_attribute_id} className="border-b last:border-0 text-sm" data-testid="attribute-row">
                      <td className="px-4 py-3 text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          {v.attribute_name || v.object_type_attribute_id}
                          <PluginItemBadges badges={attributeBadges.get(v.object_type_attribute_id)} />
                        </span>
                      </td>
                      <td
                        className={isEditing ? 'px-2 py-1' : `px-4 py-3 ${canEdit ? 'cursor-pointer hover:bg-muted/50 transition-colors group/cell' : ''}`}
                        onClick={readOnly ? undefined : (isEditing ? (e) => e.stopPropagation() : canEdit ? () => setEditingValueId(v.id) : undefined)}
                      >
                        {isEditing ? (
                          <InlineCellEditor
                            value={rawValue}
                            attrType={effectiveType}
                            attribute={attr}
                            typeMap={typeMap}
                            attributeTypes={attributeTypes}
                            objectId={objectId}
                            valueId={v.id}
                            schemaId={obj?.schema_id}
                            lang={lang}
                            onSave={(newValue) => handlePanelSave(v, attr, newValue)}
                            onCancel={() => setEditingValueId(null)}
                            readOnly={readOnly}
                            showInlineTranslateButton={false}
                          />
                        ) : (
                          <span className="inline-flex items-center gap-1.5 min-w-0">
                            <InlineCellValue value={rawValue} attrType={effectiveType} attribute={attr} typeMap={typeMap} attributeTypes={attributeTypes} allValues={allValuesForAttr} />
                            {canTranslateValue && (isFallingBack(v, lang, defaultLanguageCode) ? (
                              <FallbackBadge
                                langCode={lang}
                                langName={getLanguageName(lang, languageList)}
                                onClick={(e) => { e.stopPropagation(); openTranslationPanel(v, effectiveType) }}
                              />
                            ) : (
                              <button
                                type="button"
                                data-testid="translate-value-button"
                                title={t('browse.values.translate', 'Translate value')}
                                aria-label={t('browse.values.translate', 'Translate value')}
                                onClick={(e) => { e.stopPropagation(); openTranslationPanel(v, effectiveType) }}
                                className="inline-flex items-center justify-center shrink-0 h-5 w-5 rounded-full text-muted-foreground/60 hover:text-foreground hover:bg-accent transition-colors"
                              >
                                <LucideIcon name="globe" size={12} />
                              </button>
                            ))}
                            {attr?.is_locked === true && <LockIndicator managedBy={attr.managed_by} />}
                            {!isLocked && <LucideIcon name="pencil" size={11} data-testid="edit-value-affordance" className="opacity-0 group-hover/cell:opacity-100 shrink-0 text-muted-foreground/40 transition-opacity" />}
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
                {Object.entries(grouped).map(([attrName, refList]) => {
                  const refAttr = attributes.find(a => a.name === attrName)
                  return (
                  <tr key={attrName} className="border-b last:border-0 text-sm bg-blue-50/50 dark:bg-blue-950/20" data-testid="attribute-row">
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs font-medium text-blue-700 dark:text-blue-300">
                         {t('browse.references.ref_badge', 'Ref')}
                      </span>
                      {refAttr?.reference_type && <> <ReferenceTypeBadge referenceType={refAttr.reference_type} /></>}
                      <span className="text-muted-foreground ml-1.5">{attrName}</span>
                      <PluginItemBadges badges={attributeBadges.get(refAttr?.id)} />
                    </td>
                    <td className="px-4 py-3 space-y-1">
                      {refList.map((r) => (
                        <div key={r.id}>
                          <Link to={`/browse/objects/${r.to_object_id}`} className="text-primary hover:underline">
                            {r.to_object_name || r.to_object_id}
                          </Link>
                        </div>
                      ))}
                      {refAttr?.is_locked === true && <LockIndicator managedBy={refAttr.managed_by} />}
                    </td>
                  </tr>
                  )
                })}
                {unsetReference.map((attr) => (
                  <tr key={`unset-ref-${attr.id}`} className="border-b last:border-0 text-sm bg-blue-50/50 dark:bg-blue-950/20" data-testid="attribute-row">
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs font-medium text-blue-700 dark:text-blue-300">
                        {t('browse.references.ref_badge', 'Ref')}
                      </span>
                      {attr.reference_type && <> <ReferenceTypeBadge referenceType={attr.reference_type} /></>}
                      <span className="text-muted-foreground ml-1.5">{attr.name}</span>
                      <PluginItemBadges badges={attributeBadges.get(attr.id)} />
                    </td>
                    <td className="px-4 py-3 space-y-1">
                      <span className="inline-flex items-center gap-2">
                        <span data-testid="unset-locked-value" className="text-muted-foreground">—</span>
                        <LockIndicator managedBy={attr.managed_by} />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
        )}
      </div>

      {inboundGrouped.length > 0 && (
        <div className="rounded-lg border p-6 space-y-3">
          <h3 className="text-lg font-semibold">{selectPluralKey('browse.detail.referenced_by', inboundRefs.length) === 'browse.detail.referenced_by_one'
          ? interpolateDefault(t('browse.detail.referenced_by_one', 'Referenced By ({{count}})'), { count: inboundRefs.length })
          : interpolateDefault(t('browse.detail.referenced_by_other', 'Referenced By ({{count}})'), { count: inboundRefs.length })}</h3>
          <p className="text-xs text-muted-foreground">{t('browse.detail.objects_reference_object', 'Objects that reference this object')}</p>
          <TableToolbar
            urlQ={panelQ}
            setQ={setPanelQ}
            placeholder={t('browse.detail.search_referenced_by_placeholder', 'Search referenced by...')}
            ariaLabel={t('browse.detail.search_referenced_by_label', 'Search referenced by')}
          />
          <Table>
            <thead>
              <tr className="border-b">
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium w-1/3">{t('common.object_type', 'Object Type')}</th>
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.source_object', 'Source Object')}</th>
              </tr>
            </thead>
              <tbody>
                {filteredInboundGrouped.length === 0 && (
                  <tr>
                    <td colSpan={2} className="px-4 py-8 text-center text-sm text-muted-foreground">
                      {t('browse.detail.no_matching_referenced_by', 'No matching references.')}
                    </td>
                  </tr>
                )}
                {filteredInboundGrouped.map(({ typeId, typeName, refs }) => (
                  <tr key={typeId} className="border-b last:border-0 text-sm bg-amber-50/50 dark:bg-amber-950/20">
                    <td className="px-4 py-3 align-top">
                      <span className="inline-flex items-center gap-1 rounded bg-amber-100 dark:bg-amber-900 px-1.5 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300">
                        {typeName}
                      </span>
                    </td>
                    <td className="px-4 py-3 space-y-1">
                      {refs.map((r) => (
                        <div key={r.id}>
                          <Link to={`/browse/object-types/${r.object_type_id}?select=${r.from_object_id}`} className="text-primary hover:underline">
                            {r.from_object_name || r.from_object_id}
                          </Link>
                          {r.from_attribute_name && (
                             <span className="ml-1.5 text-xs text-muted-foreground">{interpolateDefault(t('browse.references.via_attribute', 'via {{name}}'), { name: r.from_attribute_name })}</span>
                          )}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
        </div>
      )}

      {!inboundRefsPending && inboundGrouped.length === 0 && (
        <div className="rounded-lg border border-dashed p-6 text-center">
          <p className="text-sm text-muted-foreground">{t('browse.detail.no_inbound_references', 'No objects reference this object.')}</p>
        </div>
      )}

      <div className="rounded-lg border">
        <div className="flex border-b" data-testid="object-detail-tabs">
          {detailTabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => toggleSection(tab.key)}
              aria-expanded={activeSection === tab.key}
              data-testid={`${tab.key}-toggle`}
              className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeSection === tab.key
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <ObjectCommentsSection objectId={objectId} active={activeSection === 'comments'} />
        <ObjectFilesSection objectId={objectId} active={activeSection === 'files'} />
        <ObjectHistorySection objectId={objectId} active={activeSection === 'history'} />
      </div>

      <PluginSectionRegion
        family="objectDetailSections"
        headingLevel="h3"
        gapClass="space-y-4"
        componentProps={{ object: obj }}
      />

      <PermissionsDialog
        entityType="object"
        recordId={objectId}
        open={permDialogOpen}
        onClose={() => setPermDialogOpen(false)}
      />
      {graphOpen && (
        <Suspense fallback={<GraphModalShell onClose={() => setGraphOpen(false)} />}>
          <ObjectGraphModal objectId={objectId} open={graphOpen} onClose={() => setGraphOpen(false)} />
        </Suspense>
      )}
      <TranslationPanel
        entityType="object_value"
        entityId={translationPanel?.entityId}
        schemaId={obj?.schema_id}
        fields={[{ key: 'value' }]}
        fieldValues={translationPanel?.fieldValues || {}}
        open={!!translationPanel}
        onClose={() => setTranslationPanel(null)}
        focusLang={lang}
      />
    </div>
  )
})

export default ObjectDetailPanel
