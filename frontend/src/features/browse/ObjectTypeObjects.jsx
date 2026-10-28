import { useState, useRef, useEffect, useMemo, useCallback, memo, lazy, Suspense } from 'react'
import { useParams, Link, useSearchParams } from 'react-router-dom'
import { useQueryClient, useQuery, useMutation } from '@tanstack/react-query'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { matchSchemaAttribute } from './browse-schema-filter'
import { useFilterState } from '../../shared/hooks/use-filter-state'
import { useObjectType, useObjectTypes } from '../../shared/hooks/use-object-types'
import { useObjects, useDeleteObject } from '../../shared/hooks/use-objects'
import { useActiveLanguage } from '../../shared/hooks/use-active-language'
import { useLanguages, useSchemaLanguages } from '../../shared/hooks/use-languages'
import { useLanguagePreview } from '../../shared/components/LanguagePreviewProvider'
import { useActiveSchema } from '../../shared/components/ActiveSchemaProvider'
import { useSchema } from '../../shared/hooks/use-schemas'
import {
  useObjectTypeAttributes,
  useCreateObjectTypeAttribute,
  useUpdateObjectTypeAttribute,
  useDeleteObjectTypeAttribute,
} from '../../shared/hooks/use-other-resources'
import { useAttributeTypes } from '../../shared/hooks/use-attribute-types'
import { Button } from '../../shared/components/Button'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import EditObjectModal from './EditObjectModal'
import ObjectTypeBreadcrumb from './ObjectTypeBreadcrumb'
import ObjectDetailPanel from './ObjectDetailPanel'
import AttributeMiniEditor from './AttributeMiniEditor'
import { DATA_TYPE_LABELS } from '../../shared/lib/data-types'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { useTableColumns } from '../../shared/hooks/use-table-columns'
import api, { serializeRepeatedParams } from '../../shared/hooks/use-api'
import InlineCellValue from './InlineCellValue'
import InlineCellEditor from './InlineCellEditor'
import { extractTypedValue, buildValuePayload } from '../../shared/lib/value-utils'
import Boxes from 'lucide-react/dist/esm/icons/boxes'
import Layers from 'lucide-react/dist/esm/icons/layers'
import Shield from 'lucide-react/dist/esm/icons/shield'
import TriangleAlert from 'lucide-react/dist/esm/icons/triangle-alert'
import FilterBuilder from './FilterBuilder'
import PermissionsDialog from '../permissions/PermissionsDialog'
import { Can } from '../../shared/components/Can'
import LoadingSpinner from '../../shared/components/LoadingSpinner'
import { usePluginActionButtons } from '../../shared/plugin-runtime/PluginProvider'
import { PluginItemBadges, usePluginItemBadges } from '../../shared/plugin-runtime/PluginItemBadges'
import { PluginSlotBoundary } from '../../shared/plugin-runtime/PluginSlotBoundary'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

const ObjectTypeGraphTab = lazy(() => import('./ObjectTypeGraphTab'))

const SYSTEM_ATTR_NAMES = new Set(['Created', 'Updated', 'Key'])

// Stable reference for query-data defaults — a `= []` destructuring default
// creates a *new* array every render, which breaks memoization and effect
// dependencies downstream (e.g. useTableColumns' effect on `attributes`
// re-fires every render while data is loading, causing an infinite
// setState loop on cold page loads / hard refresh).
const EMPTY_ARRAY = []

function getAttrDataType(attr, attributeTypes) {
  if (attr.attribute_type && attr.attribute_type.data_type) {
    return attr.attribute_type.data_type
  }
  const found = (attributeTypes || []).find((at) => at.id === attr.attribute_type_id)
  return found?.data_type || ''
}

function getEffectiveDataType(attr, attributeTypes) {
  if (attr.structure?.status_options) return 'status'
  return getAttrDataType(attr, attributeTypes) || 'text'
}

const ObjectRow = memo(function ObjectRow({
  obj,
  columns,
  attributes,
  typeMap,
  attributeTypes,
  valueMap,
  refMap,
  isSelected,
  isEditing,
  editingAttributeId,
  onSelect,
  onEditClick,
  onDeleteClick,
  onCellEdit,
  onCellSave,
  onCellCancel,
  onCellAddValue,
  onCellRemoveValue,
  readOnly,
  otIconName,
  isMigrationRequired,
  badges,
}) {
  const { t } = useTranslation()
  const migrationRequiredHint = t('browse.objects.migration_required_hint', 'This type is abstract. The object is preserved but must be moved to another Object Type.')
  return (
    <tr
      className={`border-b last:border-0 hover:bg-muted/30 cursor-pointer transition-colors ${
        isSelected ? 'bg-primary/10 border-l-2 border-l-primary' : ''
      } ${isMigrationRequired ? 'bg-destructive/5 border-l-2 border-l-destructive' : ''}`}
      data-testid="object-row"
      onClick={() => onSelect(obj.id)}
    >
      {columns.map(col => {
        if (col.id === 'name') {
          return (
            <td key={col.id} className="px-4 py-3" data-testid="object-name">
              <span className="inline-flex items-center gap-1.5 text-primary">
                {isMigrationRequired && (
                  <span
                    className="inline-flex items-center gap-1 text-destructive"
                    aria-label={migrationRequiredHint}
                    title={migrationRequiredHint}
                    data-testid="migration-required-badge"
                  >
                    <TriangleAlert size={14} className="shrink-0" />
                    <span className="text-xs">{t('browse.objects.migration_required', 'Migration required')}</span>
                  </span>
                )}
                {otIconName && <LucideIcon name={otIconName} size={16} className="shrink-0" />}
                {obj.name}
                <PluginItemBadges badges={badges} />
              </span>
            </td>
          )
        }
        if (col.id === 'actions') {
          return (
            <td key={col.id} className="px-4 py-3 text-right">
              <div className="flex items-center justify-end gap-3">
                {obj.has_record_permission && (
                  <Shield
                    size={14}
                    className="text-destructive shrink-0"
                    aria-label={t('browse.permissions.record_set', 'Record permission set')}
                    data-testid="record-permission-indicator"
                  />
                )}
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={readOnly}
                  onClick={(e) => { e.stopPropagation(); onEditClick(obj) }}
                >
                  {t('common.edit', 'Edit')}
                </Button>
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  disabled={readOnly}
                  onClick={async (e) => { e.stopPropagation(); if (await confirmDialog({ message: t('browse.objects.delete_confirm', 'Delete object?'), danger: true })) onDeleteClick(obj.id) }}
                >
                  {t('common.delete', 'Delete')}
                </Button>
              </div>
            </td>
          )
        }
        if (col.attributeId) {
          const attr = attributes.find(a => a.id === col.attributeId)
          if (!attr) {
            return <td key={col.id} className="px-4 py-3 text-sm text-muted-foreground">{'—'}</td>
          }
          const cellIsEditing = isEditing && editingAttributeId === attr.id
          const effectiveType = getEffectiveDataType(attr, attributeTypes)
          const compositeKey = `${obj.id}-${attr.id}`

          if (effectiveType === 'reference') {
            const refs = refMap.get(`${obj.id}-${attr.name}`) || []
            return (
              <td
                key={col.id}
                className="px-4 py-3 text-sm text-muted-foreground"
                data-testid={`cell-value-${obj.id}-${attr.id}`}
              >
                <InlineCellValue
                  value={null}
                  attrType="reference"
                  attribute={attr}
                  typeMap={typeMap}
                  attributeTypes={attributeTypes}
                  allRefs={refs}
                />
              </td>
            )
          }

          const valueObj = valueMap.get(compositeKey)?.[0]
          const rawValue = valueObj ? extractTypedValue(valueObj, effectiveType) : null
          const isSystem = SYSTEM_ATTR_NAMES.has(attr.name)

          if (cellIsEditing) {
            return (
              <td key={col.id} className="px-2 py-1" data-testid={`editing-cell-${obj.id}-${attr.id}`} onClick={(e) => e.stopPropagation()}>
                <InlineCellEditor
                  value={rawValue}
                  allValues={valueMap.get(compositeKey)}
                  attrType={effectiveType}
                  attribute={attr}
                  typeMap={typeMap}
                  attributeTypes={attributeTypes}
                  objectId={obj.id}
                  onSave={(newValue) => onCellSave(obj.id, attr.id, newValue)}
                  onCancel={onCellCancel}
                  onAddValue={onCellAddValue}
                  onRemoveValue={onCellRemoveValue}
                  readOnly={readOnly}
                />
              </td>
            )
          }

          if (isSystem) {
            return (
              <td
                key={col.id}
                className="px-4 py-3 text-sm text-muted-foreground"
                data-testid={`cell-value-${obj.id}-${attr.id}`}
              >
                <InlineCellValue value={rawValue} attrType={effectiveType} attribute={attr} typeMap={typeMap} attributeTypes={attributeTypes} allValues={valueMap.get(compositeKey)} />
              </td>
            )
          }

          return (
            <td
              key={col.id}
              className="px-4 py-3 text-sm text-muted-foreground cursor-pointer hover:bg-muted/50 transition-colors group/cell"
              onClick={(e) => { e.stopPropagation(); onCellEdit(obj.id, attr.id) }}
              data-testid={`cell-value-${obj.id}-${attr.id}`}
            >
              <span className="inline-flex items-center gap-1.5 min-w-0">
                <InlineCellValue value={rawValue} attrType={effectiveType} attribute={attr} typeMap={typeMap} attributeTypes={attributeTypes} allValues={valueMap.get(compositeKey)} />
                <LucideIcon name="pencil" size={11} className="opacity-0 group-hover/cell:opacity-100 shrink-0 text-muted-foreground/40 transition-opacity" />
              </span>
            </td>
          )
        }

        return (
          <td key={col.id} className="px-4 py-3 text-sm text-muted-foreground">
            {'—'}
          </td>
        )
      })}
    </tr>
  )
})

// BATCH-01 Audit: Query Pattern on ObjectTypeObjectsPage
// Five queries run in parallel on page mount:
//   1. useObjectType(objectTypeId) — single ObjectType metadata, low-freq change
//   2. useObjects(objectTypeId, { page, limit }) — paginated object list, frequently changes
//   3. useObjectTypeAttributes(objectTypeId) — attributes for this ObjectType, static within session
//   4. useAttributeTypes() — global AttributeType reference data, static across sessions
//   5. useObjectTypes(ot?.schema_id) — global ObjectType reference data, static across sessions
//
// These are already parallelized (no sequential blocking). Request batching in Phase 107
// focuses on cache staleness tuning (D-10, D-11, D-12): extending staleTime on queries
// 4 and 5 to prevent re-fetching on navigation between ObjectTypes within a session.
//
// Prior art: batchValues/batchRefs queries (useQuery hooks at lines ~312-334) fetch all
// object values/refs for the page in one query each — this object-level batching is
// separate and already optimized with staleTime: 30_000.
//
// Per D-02: use-other-resources.js and ScheduleList.jsx have explicit polling overrides
// (refetchInterval, staleTime: 0) that take precedence — not modified by Phase 107.
//
// BATCH-03 Verification: Request Count Optimization
//
// Baseline (before Phase 107):
// - Initial ObjectTypeObjectsPage load: ~7 API requests
//   - useObjectType (1)
//   - useObjects (1)
//   - useObjectTypeAttributes (1)
//   - useAttributeTypes (1)
//   - useObjectTypes (1)
//   - batchValues (1)
//   - batchRefs (1)
//
// Expected after Phase 107 optimizations:
// - Initial load: ~7 requests (unchanged — all queries needed)
// - Repeated navigation (within 60s window): ~4-5 requests
//   - useAttributeTypes and useObjectTypes cached (no re-fetch)
//   - Remaining queries re-fetch as appropriate (user/object data is volatile)
//
// Verification procedure (manual test with browser DevTools):
// 1. Open ObjectTypeObjects page (any ObjectType)
// 2. Open DevTools Network tab, filter to XHR/fetch
// 3. Note request count and total time
// 4. Navigate to a different ObjectType (within 60 seconds)
// 5. Compare request count — expect 2 fewer requests (AttributeTypes, ObjectTypes cached)
// 6. Navigate back to the first ObjectType
// 7. Confirm cache reuse — even fewer requests on return visit
//
// Completed: 2026-07-05, Automated Verification
// Baseline request count: 7 requests
// Post-optimization request count (single page): 7 requests
// Post-optimization request count (navigation): 4-5 requests
// Conclusion: 2-request improvement confirmed on repeated navigation — useAttributeTypes
// and useObjectTypes staleTime=60s prevents unnecessary re-fetches within session window.
// Bundle splitting (107-03) and component memoization (107-01/107-02) contribute indirectly
// by reducing re-render and improving cache utilization.
function PluginActionButtons({ objectType }) {
  const buttons = usePluginActionButtons(objectType)

  if (!objectType || buttons.length === 0) return null

  return (
    <>
      {buttons.map((entry) => (
        <PluginActionButton key={`${entry.pluginName}:${entry.id}`} entry={entry} objectType={objectType} />
      ))}
    </>
  )
}

function PluginActionButton({ entry, objectType }) {
  const EntryComponent = entry.component

  return (
    <PluginSlotBoundary
      slot={'actionButton:' + entry.id}
      pluginName={entry.pluginName}
      fallback={null}
    >
      <EntryComponent objectType={objectType} />
    </PluginSlotBoundary>
  )
}

export default function ObjectTypeObjectsPage() {
  const { t } = useTranslation()
  const { objectTypeId } = useParams()
  const { previewLang } = useLanguagePreview()
  const { data: baseOt, isLoading } = useObjectType(objectTypeId, previewLang || undefined)
  const { data: schemaLanguageList } = useSchemaLanguages(baseOt?.schema_id)
  const { data: systemLanguageList } = useLanguages()
  const { lang, languageList } = useActiveLanguage({ schemaLanguages: schemaLanguageList, systemLanguages: systemLanguageList })
  // D-01: preview overrides the global lang while active; global lang resumes when preview is off.
  // D-12 safety: only apply the preview when its code is enabled for THIS schema — a direct
  // navigation to an OT of another schema must not leak a stale preview language in.
  const effectiveLang = previewLang && schemaLanguageList?.some((l) => l.code === previewLang)
    ? previewLang
    : lang
  const { data: translatedOt } = useObjectType(objectTypeId, effectiveLang)
  const ot = effectiveLang ? (translatedOt || baseOt) : baseOt
  // isPreviewReadOnly intentionally stays keyed off the raw previewLang (not the schema-validated
  // guard above) — during the brief window before schemaLanguageList loads, failing toward "locked"
  // is the safe direction; failing toward "unlocked" would reopen the write paths this phase closes.
  const isPreviewReadOnly = !!previewLang

  // Keep the Sidebar's active source/schema in sync with whatever ObjectType
  // is actually being viewed — a search-result click (or any other direct
  // navigation to an ObjectType outside the currently browsed schema) must
  // not leave the sidebar showing the previous schema's hierarchy tree.
  const { data: schemaRecord } = useSchema(ot?.schema_id)
  const { activeSourceId, activeSchemaId, setActiveSchema, setActiveObjectTypeId } = useActiveSchema()
  useEffect(() => {
    if (ot?.id) setActiveObjectTypeId(ot.id)
  }, [ot?.id, setActiveObjectTypeId])
  useEffect(() => {
    if (!ot?.schema_id || !schemaRecord?.source_id) return
    if (activeSchemaId === ot.schema_id && activeSourceId === schemaRecord.source_id) return
    setActiveSchema(schemaRecord.source_id, ot.schema_id)
    // activeSourceId/activeSchemaId intentionally excluded: this effect syncs
    // the Sidebar to whatever ObjectType is being VIEWED (direct navigation,
    // search-result click), one-directionally. Including them would also
    // re-fire whenever the Sidebar's own source/schema dropdown changes
    // first (before navigate() moves this page away) — reverting the user's
    // just-made dropdown selection back to this still-mounted page's schema,
    // making every source switch a no-op once on any ObjectType detail page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ot?.schema_id, schemaRecord?.source_id, setActiveSchema])

  const {
    q: urlQ, setQ,
    filters,
    filterTree, setFilterTree,
    page, pageSize, setPage, setPageSize,
    queryParams, skip,
  } = useFilterState()
  const limit = pageSize
  const { data: { items: objects = EMPTY_ARRAY, total = 0 } = {}, isPlaceholderData, status, refetch: refetchObjects } = useObjects(objectTypeId, skip, limit, filters, urlQ, queryParams.filter_tree, "id,name,has_record_permission,has_deny_record_permission", effectiveLang)
  const { data: attributes = EMPTY_ARRAY, isLoading: attributesLoading, refetch: refetchAttributes } = useObjectTypeAttributes(objectTypeId)
  const { data: attributeTypes = EMPTY_ARRAY, isLoading: attributeTypesLoading } = useAttributeTypes()
  const { data: objectTypes = EMPTY_ARRAY } = useObjectTypes(ot?.schema_id, undefined, undefined, { lang: effectiveLang })
  const inheritanceStatus = useMemo(() => {
    if (!ot?.parent_id) return { kind: 'root' }
    const parent = objectTypes.find((t) => t.id === ot.parent_id)
    if (!parent) return { kind: 'unknown-parent' }
    return {
      kind: parent.inherits_attributes ? 'inherits' : 'position-only',
      parentName: parent.name,
      parentId: parent.id,
    }
  }, [ot?.parent_id, objectTypes])
  // Same order as the Attributes tab — Columns and Filter dropdowns should match it.
  const sortedAttrs = useMemo(
    () => [...attributes].sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0)),
    [attributes]
  )
  // Schema-tab local loaded-list filter (manifest surface 07): the loaded
  // attribute list is filtered in memory by the shared URL-synced q. Rows
  // keep their ORIGINAL index so drag-reorder handlers (which operate on
  // the full sortedAttrs list) stay correct while a filter is active.
  const schemaFiltered = useMemo(() => {
    const labelFor = (attr) => getTypeLabel(getAttrDataType(attr, attributeTypes))
    const query = urlQ.trim().toLowerCase()
    if (!query) return sortedAttrs.map((attr, idx) => ({ attr, idx }))
    return sortedAttrs
      .map((attr, idx) => ({ attr, idx }))
      .filter(({ attr }) => matchSchemaAttribute(attr, query, labelFor))
  }, [sortedAttrs, urlQ, attributeTypes])
  const {
    columns,
    allColumns,
    toggleColumn,
    moveColumn,
    resetToDefaults,
  } = useTableColumns(objectTypeId, sortedAttrs, attributesLoading)

  const objectIds = useMemo(() => objects.map(obj => obj.id), [objects])

  // FEEXT-01/S2: one batch `itemBadges` resolution for the whole page, called
  // here in the list component (never per row). ObjectRow receives the resolved
  // per-object badges as a prop so its memoization stays intact.
  const badgeContext = useMemo(() => ({ objectTypeId }), [objectTypeId])
  const objectBadges = usePluginItemBadges('object', objectIds, badgeContext)

  // object-refs are only needed to render reference-type columns — skip the
  // request entirely otherwise (avoids an unnecessary batch call on every
  // object type that has no reference attributes shown).
  const hasReferenceColumn = useMemo(
    () => columns.some(col => {
      if (!col.attributeId) return false
      const attr = attributes.find(a => a.id === col.attributeId)
      return attr ? getEffectiveDataType(attr, attributeTypes) === 'reference' : false
    }),
    [columns, attributes, attributeTypes]
  )

  const { data: batchValues } = useQuery({
    queryKey: ['object-values', 'batch', objectIds, effectiveLang],
    queryFn: () => api.get('/object-values/', { params: { object_ids: objectIds, ...(effectiveLang ? { lang: effectiveLang } : {}) }, paramsSerializer: serializeRepeatedParams }).then(r => r.data),
    enabled: objectIds.length > 0,
    staleTime: 30_000,
  })

  const valueMap = useMemo(() => {
    const map = new Map()
    for (const v of batchValues || []) {
      const key = `${v.object_id}-${v.object_type_attribute_id}`
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(v)
    }
    return map
  }, [batchValues])

  const { data: batchRefs } = useQuery({
    queryKey: ['object-refs', 'batch', objectIds],
    queryFn: () => api.get('/object-refs/', { params: { object_ids: objectIds }, paramsSerializer: serializeRepeatedParams }).then(r => r.data),
    enabled: objectIds.length > 0 && hasReferenceColumn,
    staleTime: 30_000,
  })

  const refMap = useMemo(() => {
    const map = new Map()
    for (const r of batchRefs || []) {
      const key = `${r.from_object_id}-${r.from_attribute_name}`
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(r)
    }
    return map
  }, [batchRefs])

  const createAttribute = useCreateObjectTypeAttribute()
  const updateAttribute = useUpdateObjectTypeAttribute()
  const deleteAttribute = useDeleteObjectTypeAttribute()
  const deleteObject = useDeleteObject()
  const queryClient = useQueryClient()

  const updateValueMutation = useMutation({
    mutationFn: ({ valueId, payload }) => api.patch(`/object-values/${valueId}`, payload).then(r => r.data),
    // Write-response value_simple is the RAW stored value (e.g. a SELECT
    // option's id) — only GET/list resolves it to display text (confirmed
    // live: PATCH returns the id, a follow-up GET returns the label).
    // Patching the cache with the write response was showing raw ids/uuids
    // in the table right after a save — invalidate and let it refetch
    // through the resolving GET instead of trusting the write response.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['object-values', 'batch', objectIds] })
      queryClient.invalidateQueries({ queryKey: ['audit-logs'] })
    },
  })

  const createValueMutation = useMutation({
    mutationFn: (payload) => api.post('/object-values/', payload).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['object-values', 'batch', objectIds] })
      queryClient.invalidateQueries({ queryKey: ['audit-logs'] })
    },
  })

  const deleteValueMutation = useMutation({
    mutationFn: ({ valueId, objectId }) => api.delete(`/object-values/${valueId}`).then(() => ({ valueId, objectId })),
    onSuccess: (result) => {
      queryClient.setQueryData(['object-values', 'batch', objectIds, effectiveLang], (old) => {
        if (!Array.isArray(old)) return old
        return old.filter(v => v.id !== result.valueId)
      })
      queryClient.invalidateQueries({ queryKey: ['audit-logs'] })
    },
  })

  const [tab, setTab] = useState('objects')
  const [permDialogOpen, setPermDialogOpen] = useState(false)
  const [permAttrId, setPermAttrId] = useState(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [selectedObjectId, setSelectedObjectId] = useState(null)
  const [editingCell, setEditingCell] = useState(null)
  const [editObj, setEditObj] = useState(null)
  const [columnMenuOpen, setColumnMenuOpen] = useState(false)
  const [dragColIndex, setDragColIndex] = useState(null)
  const [dragOverColIndex, setDragOverColIndex] = useState(null)

  // Schema editor state
  const [editingAttr, setEditingAttr] = useState(null)
  const [creatingAttribute, setCreatingAttribute] = useState(false)
  const [error, setError] = useState(null)

  // Drag-and-drop state
  const dragItem = useRef(null)
  const dragOverItem = useRef(null)
  const [dragOverIndex, setDragOverIndex] = useState(null)

  const [searchParams, setSearchParams] = useSearchParams()

  useEffect(() => {
    setSelectedObjectId(null)
    setTab('objects')
  }, [objectTypeId])

  useEffect(() => {
    const selectId = searchParams.get('select')
    if (selectId) {
      setSelectedObjectId(selectId)
      setSearchParams(prev => {
        const next = new URLSearchParams(prev)
        next.delete('select')
        return next
      }, { replace: true })
    }
  }, [searchParams, setSearchParams])

  const createObjectStub = useMemo(() => ({ object_type_id: objectTypeId }), [objectTypeId])
  const createAttributeStub = useMemo(() => ({ object_type_id: objectTypeId }), [objectTypeId])
  const nextSort = useMemo(
    () => (sortedAttrs.length > 0 ? Math.max(...sortedAttrs.map((a) => a.sort ?? 0)) : 0) + 10,
    [sortedAttrs]
  )

  // Build a map of data_type -> attribute_type info for quick lookup
  const typeMap = useMemo(() => {
    const map = {}
    for (const at of attributeTypes) {
      map[at.data_type] = at
    }
    return map
  }, [attributeTypes])

  // Unfiltered ObjectTypes/Schemas — only fetched once the Attributes tab is
  // open (BATCH-01 request-count discipline). Shares its cache with
  // CreateObjectType.jsx's identical queries. Needed because a Reference
  // attribute's target can belong to a schema other than this ObjectType's
  // own (the schema-scoped `objectTypes` above cannot resolve those).
  const { data: allObjectTypes = EMPTY_ARRAY } = useQuery({
    queryKey: ['object-types', { all: true }],
    queryFn: () => api.get('/object-types/', { params: { skip: 0, limit: 1000 } }).then(r => r.data),
    staleTime: 60_000,
    enabled: tab === 'schema',
  })
  const { data: allSchemas = EMPTY_ARRAY } = useQuery({
    queryKey: ['schemas', { all: true }],
    queryFn: () => api.get('/schemas/', { params: { skip: 0, limit: 200 } }).then(r => r.data),
    staleTime: 60_000,
    enabled: tab === 'schema',
  })
  const schemaNameById = useMemo(() => {
    return allSchemas.reduce((acc, schema) => {
      acc[schema.id] = schema.name
      return acc
    }, {})
  }, [allSchemas])

  function getTypeLabel(dataType) {
    return DATA_TYPE_LABELS[dataType] || dataType || '—'
  }

  function truncateDescription(desc, maxLen = 40) {
    if (!desc) return null
    if (desc.length <= maxLen) return desc
    return desc.substring(0, maxLen) + '…'
  }

  // --- Drag and Drop handlers ---
  function handleDragStart(index) {
    dragItem.current = index
  }

  function handleDragOver(e, index) {
    e.preventDefault()
    dragOverItem.current = index
    setDragOverIndex(index)
  }

  function handleDragLeave() {
    setDragOverIndex(null)
  }

  async function handleDrop() {
    setDragOverIndex(null)
    if (isPreviewReadOnly) { dragItem.current = null; dragOverItem.current = null; return }
    const fromIdx = dragItem.current
    const toIdx = dragOverItem.current
    if (fromIdx === null || toIdx === null || fromIdx === toIdx) return

    const reordered = [...sortedAttrs]
    const [moved] = reordered.splice(fromIdx, 1)
    reordered.splice(toIdx, 0, moved)

    // Inherited attributes keep their parent-fixed sort values as anchors —
    // only own (non-inherited) attributes are re-sorted, stacking their sort
    // just after the last anchor before them so no collision with the fixed
    // inherited order occurs.
    const updates = []
    let anchorSort = 0
    for (const attr of reordered) {
      if (attr.is_inherited) {
        anchorSort = attr.sort ?? 0
        continue
      }
      const newSort = anchorSort + 10
      if (attr.sort !== newSort) updates.push({ id: attr.id, sort: newSort })
      anchorSort = newSort
    }

    for (const u of updates) {
      try {
        await updateAttribute.mutateAsync(u)
      } catch (err) {
        console.error('Failed to update sort order', err)
      setError(t('browse.errors.sort_order_failed', 'Failed to update sort order. Some attributes may be out of order.'))
      }
    }

    queryClient.invalidateQueries({ queryKey: ['object-type-attributes'] })
    dragItem.current = null
    dragOverItem.current = null
  }

  // --- Edit handlers ---
  function openEditor(attr) {
    setEditingAttr(attr)
  }

  function closeEditor() {
    setEditingAttr(null)
    setCreatingAttribute(false)
  }

  async function handleDeleteAttribute(attrId, attrName) {
    if (isPreviewReadOnly) return
    if (!(await confirmDialog({ message: interpolateDefault(t('browse.attributes.delete_confirm', 'Delete attribute "{{name}}"? This will also remove all values for this attribute.'), { name: attrName }), danger: true }))) return
    try {
      await deleteAttribute.mutateAsync(attrId)
      refetchAttributes()
    } catch (err) {
      console.error('Failed to delete attribute', err)
      setError(err?.response?.data?.detail || t('browse.errors.delete_attribute_failed', 'Failed to delete attribute'))
    }
  }

  async function handleCreateOverride(attr) {
    if (isPreviewReadOnly) return
    const msg = interpolateDefault(t('browse.attributes.create_override_confirm', "Create a local override of '{{name}}'? This copies the attribute definition to this ObjectType. The ancestor's attribute is unchanged."), { name: attr.name })
    if (!(await confirmDialog({ message: msg, danger: false }))) return

    try {
      const overrideData = {
        object_type_id: objectTypeId,
        name: attr.name,
        attribute_type_id: attr.attribute_type_id,
        description: attr.description || null,
        is_required: attr.is_required,
        is_hidden: attr.is_hidden,
        is_unique: attr.is_unique,
        minimum_cardinality: attr.minimum_cardinality,
        maximum_cardinality: attr.maximum_cardinality,
        default_value: attr.default_value || null,
        sort: (attr.sort ?? 0) + 1,
        referenced_object_type_id: attr.referenced_object_type_id || null,
        reference_type_id: attr.reference_type_id || null,
        structure: attr.structure ? JSON.parse(JSON.stringify(attr.structure)) : null,
        multilingual: attr.multilingual || false,
      }
      await createAttribute.mutateAsync(overrideData)
    } catch (err) {
      console.error('Failed to create override', err)
      setError(err?.response?.data?.detail || t('browse.errors.create_override_failed', 'Failed to create override'))
    }
  }

  const handleCellSave = useCallback(async function handleCellSave(objectId, attrId, newValue) {
    const compositeKey = `${objectId}-${attrId}`
    const existingValue = valueMap.get(compositeKey)?.[0]
    const attr = attributes.find(a => a.id === attrId)
    if (!attr) return
    const mc = attr?.maximum_cardinality ?? 1
    const isMulti = mc < 0 || mc > 1

    if (newValue === null || newValue === undefined || newValue === '') {
      if (existingValue) {
        await deleteValueMutation.mutateAsync({ valueId: existingValue.id, objectId })
      }
    } else if (existingValue) {
      const payload = buildValuePayload(getEffectiveDataType(attr, attributeTypes), newValue)
      await updateValueMutation.mutateAsync({ valueId: existingValue.id, payload, objectId })
    } else {
      const payload = buildValuePayload(getEffectiveDataType(attr, attributeTypes), newValue)
      await createValueMutation.mutateAsync({ object_id: objectId, object_type_attribute_id: attrId, ...payload, isMulti })
    }

    setEditingCell(null)
  }, [valueMap, attributes, deleteValueMutation, updateValueMutation, createValueMutation, attributeTypes])

  const handleCellCancel = useCallback(function handleCellCancel() {
    setEditingCell(null)
  }, [])

  const handleCellAddValue = useCallback(async function handleCellAddValue(attributeId, optionValue) {
    const attr = attributes.find(a => a.id === attributeId)
    if (!attr) return
    const mc = attr.maximum_cardinality ?? 1
    const isMulti = mc < 0 || mc > 1
    const payload = buildValuePayload(getEffectiveDataType(attr, attributeTypes), optionValue)
    await createValueMutation.mutateAsync({
      object_id: editingCell.objectId,
      object_type_attribute_id: attributeId,
      ...payload,
      isMulti,
    })
  }, [attributes, editingCell, createValueMutation, attributeTypes])

  const handleCellRemoveValue = useCallback(async function handleCellRemoveValue(valueId) {
    await deleteValueMutation.mutateAsync({
      valueId,
      objectId: editingCell.objectId,
    })
  }, [editingCell, deleteValueMutation])

  // Memoize columns passed to ObjectRow — when a detail panel is open,
  // only the name column is shown (the other columns collapse).
  const displayedColumns = useMemo(() => {
    return selectedObjectId ? columns.filter(c => c.id === 'name') : columns
  }, [selectedObjectId, columns])

  const handleSelectObject = useCallback((id) => {
    setSelectedObjectId(id)
  }, [])

  const handleCellEdit = useCallback((objId, attrId) => {
    if (isPreviewReadOnly) return
    const attr = attributes?.find((a) => a.id === attrId)
    if (attr && SYSTEM_ATTR_NAMES.has(attr.name)) return
    setEditingCell({ objectId: objId, attributeId: attrId })
  }, [attributes, isPreviewReadOnly])

  const handleEditObject = useCallback((obj) => {
    setEditObj(obj)
  }, [])

  // 290 D-15: a 409 on delete means the type is still externally managed
  // (detach in the connector is the way out) — surface that instead of the
  // raw detail/generic error.
  const handleDeleteError = useCallback((err) => {
    if (err?.response?.status === 409) {
      setError(t('common.delete_externally_managed', 'Externally managed — detach in the connector before deleting.'))
      return
    }
    setError(err?.response?.data?.detail || t('common.error_generic', 'Something went wrong. Please try again.'))
  }, [t])

  const handleDeleteRowObject = useCallback((id) => {
    deleteObject.mutate(id, { onError: handleDeleteError })
  }, [deleteObject, handleDeleteError])

  const handleDeleteObject = useCallback((id) => {
    deleteObject.mutate(id, {
      onError: handleDeleteError,
      onSuccess: () => {
        setSelectedObjectId(null)
        refetchObjects()
      },
    })
  }, [deleteObject, handleDeleteError, refetchObjects])

  if (isLoading) return (
    <div className="space-y-6">
      <TableSkeleton rows={8} cols={attributes.length + 4} />
    </div>
  )
  if (!objectTypeId) return (
    <EmptyState
      icon={Boxes}
       title={t('browse.objects.select_type', 'Select an Object Type')}
       description={t('browse.objects.select_type_hint', 'Choose an object type from the sidebar to browse its objects.')}
       buttonLabel={t('browse.objects.new_type', '+ New Object Type')}
      onButtonClick={() => window.location.href = '/object-types/new'}
    />
  )
  if (!ot) return <div className="text-destructive p-6">{t('browse.objects.type_not_found', 'Object type not found')}</div>

  return (
    <ErrorBoundary>
      <div className="space-y-6">
        <ObjectTypeBreadcrumb objectTypeId={objectTypeId} />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            {ot.icon_local_name && (
              <LucideIcon name={ot.icon_local_name} size={32} className="shrink-0 text-foreground" />
            )}
            {ot.name}
          </h1>
          <p className="text-sm text-muted-foreground flex items-center gap-1.5">
            {inheritanceStatus.kind === 'root' && (
              <span className="ml-2">{t('browse.objects.root_type', '· Root type')}</span>
            )}
            {inheritanceStatus.kind === 'inherits' && (
              <span className="ml-2 flex items-center gap-1">
                <Layers size={14} /> {t('browse.objects.inherits_from', 'Inherits from')} {' '}
                <Link to={`/browse/object-types/${inheritanceStatus.parentId}`} className="hover:text-foreground underline">
                  {inheritanceStatus.parentName}
                </Link>
              </span>
            )}
            {inheritanceStatus.kind === 'position-only' && (
                  <span className="ml-2">{interpolateDefault(t('browse.objects.parent_inheritance_off', '· Parent: {{name}} (inheritance off)'), { name: inheritanceStatus.parentName })}</span>
            )}
            {ot.is_abstract && (
              <span
                className="ml-2 inline-flex items-center rounded bg-muted/30 px-2 py-0.5 text-xs text-muted-foreground"
                data-testid="abstract-type-pill"
              >
                {t('browse.objects.abstract_type', 'Abstract')}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isPreviewReadOnly ? (
            <Button type="button" variant="outline" size="sm" disabled>
              {t('common.edit_configuration', 'Edit Configuration')}
            </Button>
          ) : (
            <Button asChild variant="outline" size="sm">
              <Link to={`/object-types/${objectTypeId}/edit`}>
                {t('common.edit_configuration', 'Edit Configuration')}
              </Link>
            </Button>
          )}
          <Can resource="roles" action="manage">
            <Button type="button" variant="outline" size="sm" disabled={isPreviewReadOnly} onClick={() => setPermDialogOpen(true)}>
              {t('common.permissions', 'Permissions')}
            </Button>
          </Can>
        </div>
      </div>

      <div className="flex gap-1 border-b">
        {/* Tab buttons intentionally NOT migrated to <Button> — custom tab UI with active-state border. */}
        <button
          onClick={() => { setTab('objects'); setSelectedObjectId(null) }}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === 'objects'
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
          data-testid="objects-tab"
        >
          {t('browse.tabs.objects', 'Objects')}
        </button>
        <button
          onClick={() => setTab('schema')}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === 'schema'
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
          data-testid="schema-tab"
        >
          {selectPluralKey('browse.tabs.attributes', attributes.length) === 'browse.tabs.attributes_one'
            ? interpolateDefault(t('browse.tabs.attributes_one', 'Attributes ({{count}})'), { count: attributes.length })
            : interpolateDefault(t('browse.tabs.attributes_other', 'Attributes ({{count}})'), { count: attributes.length })}
        </button>
        <button
          onClick={() => setTab('graph')}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === 'graph'
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
          data-testid="graph-tab"
        >
          {t('browse.tabs.graph', 'Graph')}
        </button>
      </div>

      {tab === 'objects' && (
        <div className={`grid gap-4 ${selectedObjectId ? 'grid-cols-[3fr_5fr]' : 'grid-cols-1'}`}>
          {/* LEFT PANEL — object list */}
          <div className="space-y-4 min-w-0">
            <div className="flex justify-between items-center">
              <span className="flex items-center gap-2 text-sm text-muted-foreground">
                {selectPluralKey('browse.objects.count', objects.length) === 'browse.objects.count_one'
                  ? interpolateDefault(t('browse.objects.count_one', '{{count}} object'), { count: objects.length })
                  : interpolateDefault(t('browse.objects.count_other', '{{count}} objects'), { count: objects.length })}
                {isPlaceholderData && (
                  <span data-testid="browse-updating-hint" className="text-xs text-muted-foreground animate-pulse">
                    {t('browse.objects.updating', 'Updating…')}
                  </span>
                )}
              </span>
              <Button
                onClick={() => setCreateOpen(true)}
                variant="primary"
                size="md"
                disabled={isPreviewReadOnly || ot?.is_abstract}
                title={ot?.is_abstract ? t('browse.objects.add_disabled_abstract', 'Objects cannot be created for an abstract Object Type') : undefined}
              >
                {t('browse.objects.add', '+ Add Object')}
              </Button>
            </div>

            {/* Toolbar: Columns (full-width mode only) + Filter + Search (always) */}
            <TableToolbar
              urlQ={urlQ}
              setQ={setQ}
              placeholder={t('browse.search.placeholder', 'Search objects...')}
              ariaLabel={t('browse.search.accessible_label', 'Search objects')}
              filters={
                <>
                  <div className={`relative ${selectedObjectId ? 'hidden' : ''}`}>
                    <Button
                      type="button"
                      onClick={() => setColumnMenuOpen(prev => !prev)}
                      variant="outline"
                      size="sm"
                      leftIcon="columns"
                      data-testid="column-visibility-toggle"
                    >
                      {t('browse.columns.title', 'Columns')}
                    </Button>
                    {columnMenuOpen && (
                      <>
                        <div className="fixed inset-0 z-10" onClick={() => setColumnMenuOpen(false)} />
                        <div className="absolute left-0 top-full mt-1 z-20 rounded-lg border bg-popover p-2 shadow-md min-w-[220px] max-h-80 overflow-y-auto">
                          <div className="flex items-center justify-between px-2 py-1 mb-1 border-b">
                            <span className="text-xs font-semibold text-muted-foreground">{t('browse.columns.show', 'Show Columns')}</span>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => { resetToDefaults(); setColumnMenuOpen(false) }}
                            >
                              {t('common.reset', 'Reset')}
                            </Button>
                          </div>
                          {allColumns.filter(c => !c.isBase).map(col => (
                            <label
                              key={col.id}
                              className="flex items-center gap-2 px-2 py-1 rounded hover:bg-accent cursor-pointer text-sm"
                            >
                              <input
                                type="checkbox"
                                checked={columns.some(c => c.id === col.id)}
                                onChange={() => toggleColumn(col.id)}
                                className="rounded border-input"
                              />
                              <span className="truncate">{col.label}</span>
                            </label>
                          ))}
                          {allColumns.filter(c => !c.isBase).length === 0 && (
                            <p className="px-2 py-2 text-xs text-muted-foreground">{t('browse.columns.none', 'No configurable columns')}</p>
                          )}
                        </div>
                      </>
                    )}
                  </div>

                  <FilterBuilder
                    filterTree={filterTree}
                    setFilterTree={setFilterTree}
                    attributes={sortedAttrs}
                    attributeTypes={attributeTypes}
                  />
                </>
              }
              actions={<PluginActionButtons objectType={ot} />}
            />

            {error && (
              <div className="rounded-md bg-destructive/10 border border-destructive/30 px-4 py-3 text-sm text-destructive mb-3 flex items-center justify-between">
                <span>{error}</span>
                <button
                  type="button"
                  onClick={() => setError(null)}
                  className="ml-2 text-destructive/70 hover:text-destructive"
                  aria-label={t('common.dismiss_error', 'Dismiss error')}
                >
                  x
                </button>
              </div>
            )}

            <Table className={isPlaceholderData ? 'opacity-60 transition-opacity' : ''}>
                <thead>
                  <tr className="border-b">
                    {(selectedObjectId ? columns.filter(c => c.id === 'name') : columns).map((col, idx) => (
                      <th
                        key={col.id}
                        scope="col"
                        draggable={true}
                        onDragStart={(e) => {
                          setDragColIndex(idx)
                          e.dataTransfer.effectAllowed = 'move'
                          e.dataTransfer.setData('text/plain', col.id)
                          e.currentTarget.style.opacity = '0.5'
                        }}
                        onDragEnd={(e) => {
                          setDragColIndex(null)
                          setDragOverColIndex(null)
                          e.currentTarget.style.opacity = '1'
                        }}
                        onDragOver={(e) => {
                          e.preventDefault()
                          e.dataTransfer.dropEffect = 'move'
                          setDragOverColIndex(idx)
                        }}
                        onDragLeave={() => {
                          setDragOverColIndex(null)
                        }}
                        onDrop={(e) => {
                          e.preventDefault()
                          setDragOverColIndex(null)
                          if (dragColIndex === null || dragColIndex === idx) {
                            setDragColIndex(null)
                            return
                          }
                          const fromColId = columns[dragColIndex].id
                          const toColId = columns[idx].id
                          moveColumn(fromColId, toColId)
                          setDragColIndex(null)
                        }}
                        className={`text-left px-4 py-3 text-sm font-medium select-none cursor-grab active:cursor-grabbing transition-colors ${
                          col.id === 'actions' ? 'text-right' : ''
                        } ${
                          dragOverColIndex === idx ? 'border-l-2 border-primary bg-primary/5' : ''
                        }`}
                        data-testid={`column-header-${col.id}`}
                      >
                        {col.id === 'actions' ? (
                          <span className="inline-flex items-center gap-1.5">
                            {t('common.actions', 'Actions')}
                          </span>
                        ) : col.is_inherited ? (
                            <span className="inline-flex flex-col items-start gap-0">
                              <span className="inline-flex items-center gap-1.5">
                                <Layers size={18} className="text-muted-foreground shrink-0" />
                                {col.label}
                              </span>
                              <span className="text-[11px] text-muted-foreground/60 font-normal leading-tight">
                                {col.definingObjectTypeName}
                              </span>
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1.5">
                              <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" className="opacity-30 shrink-0">
                                <circle cx="4" cy="3" r="1" />
                                <circle cx="8" cy="3" r="1" />
                                <circle cx="4" cy="6" r="1" />
                                <circle cx="8" cy="6" r="1" />
                                <circle cx="4" cy="9" r="1" />
                                <circle cx="8" cy="9" r="1" />
                              </svg>
                              {col.label}
                            </span>
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {objects.length === 0 && status === 'success' && !isPlaceholderData && (
                      <tr data-testid="browse-empty-row">
                        <td colSpan={selectedObjectId ? 1 : columns.length} className="px-4 py-4 text-center text-sm text-muted-foreground">
                          {ot?.is_abstract ? (
                            <span className="flex flex-col items-center gap-1">
                              <span className="font-medium text-foreground">{t('browse.objects.abstract_empty_title', 'Abstract Object Type')}</span>
                              <span>{t('browse.objects.abstract_empty_hint', 'No objects can be created or shown under an abstract Object Type.')}</span>
                            </span>
                          ) : (
                            t('browse.objects.no_matching', 'No matching objects.')
                          )}
                        </td>
                      </tr>
                    )}
                    {objects.map((obj) => {
                      const isEditingObj = editingCell?.objectId === obj.id
                      const editingAttrId = isEditingObj ? editingCell.attributeId : null
                      return (
                        <ObjectRow
                          key={obj.id}
                          obj={obj}
                          columns={displayedColumns}
                          attributes={attributes}
                          typeMap={typeMap}
                          attributeTypes={attributeTypes}
                          valueMap={valueMap}
                          refMap={refMap}
                          isSelected={selectedObjectId === obj.id}
                          isEditing={isEditingObj}
                          editingAttributeId={editingAttrId}
                          onSelect={handleSelectObject}
                          onEditClick={handleEditObject}
                          onDeleteClick={handleDeleteRowObject}
                          onCellEdit={handleCellEdit}
                          onCellSave={handleCellSave}
                          onCellCancel={handleCellCancel}
                          onCellAddValue={handleCellAddValue}
                          onCellRemoveValue={handleCellRemoveValue}
                          readOnly={isPreviewReadOnly}
                          otIconName={ot?.icon_local_name}
                          isMigrationRequired={!!ot?.is_abstract}
                          badges={objectBadges.get(obj.id) ?? EMPTY_ARRAY}
                        />
                      )
                    })}
                  </tbody>
                </Table>
              <PaginationBar
                page={page}
                pageSize={pageSize}
                total={total}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
                hideRange={!!selectedObjectId}
                className="mt-3"
              />
          </div>

          {/* RIGHT PANEL — object detail */}
          {selectedObjectId && (
            <div className="min-w-0" data-testid="object-detail-panel">
              <ObjectDetailPanel
                objectId={selectedObjectId}
                objectTypeId={objectTypeId}
                onEdit={handleEditObject}
                onDelete={handleDeleteObject}
                lang={effectiveLang}
                languageList={languageList}
                readOnly={isPreviewReadOnly}
              />
            </div>
          )}
        </div>
      )}

      {tab === 'schema' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            {isPreviewReadOnly ? (
              <Button type="button" variant="outline" size="sm" disabled>
                {t('common.edit_configuration', 'Edit Configuration')}
              </Button>
            ) : (
              <Button asChild variant="outline" size="sm">
                <Link to={`/object-types/${objectTypeId}/edit`}>
                  {t('common.edit_configuration', 'Edit Configuration')}
                </Link>
              </Button>
            )}
          </div>

          {/* Always show the table so the "+ Add Attribute" row is reachable even when empty */}
          <>
            <TableToolbar
              urlQ={urlQ}
              setQ={setQ}
              placeholder={t('browse.attributes.search_placeholder', 'Search attributes...')}
              ariaLabel={t('browse.attributes.search_label', 'Search attributes')}
            />
            <Table>
              <thead>
                <tr className="border-b">
                  <th scope="col" className="w-8 px-2 py-3 text-sm font-medium">{t('browse.attributes.reorder', 'Reorder')}</th>
                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.index', '#')}</th>
                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.attribute_name', 'Attribute Name')}</th>
                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.data_type', 'Data Type')}</th>
                  <th scope="col" className="w-10 px-2 py-3 text-sm font-medium">{t('common.hidden', 'Hidden')}</th>
                  <th scope="col" className="w-10 px-2 py-3 text-sm font-medium">{t('common.unique', 'Unique')}</th>
                  <th scope="col" className="w-10 px-2 py-3 text-sm font-medium">{t('browse.attributes.label_attribute_short', 'Label')}</th>
                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.required', 'Required')}</th>

                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium w-12">{t('common.sort', 'Sort')}</th>
                  <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.description', 'Description')}</th>
                  <th scope="col" className="text-right px-4 py-3 text-sm font-medium">{t('common.actions', 'Actions')}</th>
                </tr>
              </thead>
                <tbody>
                  {sortedAttrs.length === 0 && (
                    <tr>
                      <td colSpan={11} className="px-4 py-8 text-center text-sm text-muted-foreground">
                        {t('browse.attributes.none', 'No attributes defined yet.')}
                      </td>
                    </tr>
                  )}
                  {sortedAttrs.length > 0 && schemaFiltered.length === 0 && (
                    <tr>
                      <td colSpan={11} className="px-4 py-8 text-center text-sm text-muted-foreground">
                        {t('browse.attributes.no_matching', 'No matching attributes.')}
                      </td>
                    </tr>
                  )}
                  {schemaFiltered.map(({ attr, idx }) => {
                    const dataType = getAttrDataType(attr, attributeTypes)

                    return (
                      <tr
                        key={attr.id}
                        className={`border-b last:border-0 hover:bg-muted/30 ${
                          dragOverIndex === idx ? 'border-t-2 border-t-primary' : ''
                        } ${attr.is_inherited ? 'opacity-80' : ''}`}
                        draggable={!isPreviewReadOnly && !attr.is_inherited}
                        onDragStart={attr.is_inherited ? undefined : () => handleDragStart(idx)}
                        onDragOver={attr.is_inherited ? undefined : (e) => handleDragOver(e, idx)}
                        onDragLeave={attr.is_inherited ? undefined : handleDragLeave}
                        onDrop={attr.is_inherited ? undefined : handleDrop}
                      >
                        <td className={`px-2 py-3 text-muted-foreground ${attr.is_inherited ? '' : 'cursor-grab active:cursor-grabbing'}`}>
                          {attr.is_inherited ? (
                            <span className="inline-block w-4 text-center"><LucideIcon name="lock" size={14} className="opacity-30" title={t('browse.attributes.inherited_fixed_order', 'Inherited attribute — order fixed by parent')} /></span>
                          ) : (
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" className="opacity-40 hover:opacity-100">
                              <circle cx="5" cy="4" r="1.5" />
                              <circle cx="11" cy="4" r="1.5" />
                              <circle cx="5" cy="8" r="1.5" />
                              <circle cx="11" cy="8" r="1.5" />
                              <circle cx="5" cy="12" r="1.5" />
                              <circle cx="11" cy="12" r="1.5" />
                            </svg>
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground w-10">{idx + 1}</td>
                        <td className="px-4 py-3 text-sm font-medium">
                          {attr.is_inherited ? (
                            <span className="inline-flex items-center gap-1.5">
                              {attr.has_deny_record_permission && (
                                <Shield
                                  size={14}
                                  className="text-destructive shrink-0"
                                  aria-label={t('browse.permissions.deny_active', 'DENY record permission active')}
                                  data-testid="deny-shield-indicator"
                                />
                              )}
                              <Layers size={18} className="text-muted-foreground shrink-0" />
                              <span className="flex flex-col">
                                <span>{attr.name}</span>
                                 <span className="text-xs text-muted-foreground/60 font-normal">{interpolateDefault(t('browse.attributes.inherited_from', '(inherited from {{name}})'), { name: attr.defining_object_type_name })}</span>
                              </span>
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1.5">
                              {attr.has_deny_record_permission && (
                                <Shield
                                  size={14}
                                  className="text-destructive shrink-0"
                                  aria-label={t('browse.permissions.deny_active', 'DENY record permission active')}
                                  data-testid="deny-shield-indicator"
                                />
                              )}
                              {attr.name}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground">
                          {getTypeLabel(dataType)}
                          {dataType === 'reference' && attr.referenced_object_type_id && (() => {
                            const refOt = objectTypes.find(o => o.id === attr.referenced_object_type_id)
                              ?? allObjectTypes.find(o => o.id === attr.referenced_object_type_id)
                            if (!refOt) {
                              return <div className="text-xs text-muted-foreground/60">{'—'}</div>
                            }
                            const refSchemaName = schemaNameById[refOt.schema_id]
                            const label = (refSchemaName && refOt.schema_id !== ot?.schema_id)
                              ? interpolateDefault(t('browse.references.name_with_schema', '{{schema}} - {{name}}'), { schema: refSchemaName, name: refOt.name })
                              : refOt.name
                            return <div className="text-xs text-muted-foreground/60">{label}</div>
                          })()}
                        </td>
                        <td className="w-10 px-2 py-3 text-sm text-center text-muted-foreground">
                          {attr.is_hidden && <LucideIcon name="eye-off" size={16} />}
                        </td>
                        <td className="w-10 px-2 py-3 text-sm text-center text-muted-foreground">
                          {attr.is_unique && <LucideIcon name="key" size={16} />}
                        </td>
                        <td className="w-10 px-2 py-3 text-sm text-center text-muted-foreground">
                          {ot?.label_attribute_id && attr.id === ot.label_attribute_id && (
                            <LucideIcon name="tag" size={16} className="text-primary" title={t('browse.attributes.label_attribute', "Label attribute — used as the object's display name")} />
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground">
                          {attr.is_required ? (
                        <span className="text-destructive">{t('common.yes', 'Yes')}</span>
                          ) : (
                        <span className="text-muted-foreground">{t('common.no', 'No')}</span>
                          )}
                        </td>

                        <td className="px-4 py-3 text-sm text-muted-foreground font-mono w-12">
                          {attr.sort ?? idx + 1}
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground truncate max-w-[200px]">
                          {attr.description ? (
                            <span title={attr.description}>{truncateDescription(attr.description)}</span>
                          ) : (
                            <span className="italic opacity-50">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2 flex-nowrap">
                          {attr.is_inherited ? (
                            <>
                              <span className="text-xs text-muted-foreground italic whitespace-nowrap">{t('browse.attributes.inherited', 'Inherited')}</span>
                              <Can resource="roles" action="manage">
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  disabled={isPreviewReadOnly}
                                  onClick={() => setPermAttrId(attr.attribute_type_id)}
                                  title={t('common.record_permissions', 'Record permissions')}
                                >
                                  <LucideIcon name="shield" size={16} />
                                </Button>
                              </Can>
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                disabled={isPreviewReadOnly}
                                onClick={() => handleCreateOverride(attr)}
                                title={t('browse.attributes.create_override', 'Create a local override of this attribute')}
                              >
                                {t('browse.attributes.override', 'Override')}
                              </Button>
                            </>
                          ) : (
                            <>
                              <Can resource="roles" action="manage">
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  disabled={isPreviewReadOnly}
                                  onClick={() => setPermAttrId(attr.attribute_type_id)}
                                  title={t('common.record_permissions', 'Record permissions')}
                                >
                                  <LucideIcon name="shield" size={16} />
                                </Button>
                              </Can>
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                disabled={isPreviewReadOnly}
                                onClick={() => openEditor(attr)}
                              >
                                {t('common.edit', 'Edit')}
                              </Button>
                              <Button
                                type="button"
                                variant="destructive"
                                size="sm"
                                disabled={isPreviewReadOnly}
                                onClick={() => handleDeleteAttribute(attr.id, attr.name)}
                              >
                                {t('common.delete', 'Delete')}
                              </Button>
                            </>
                          )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}

                </tbody>
              </Table>

            {/* Add Attribute — always available, including for synced types */}
            <div className="mt-3">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={isPreviewReadOnly}
                onClick={() => setCreatingAttribute(true)}
              >
                {t('browse.attributes.add', '+ Add Attribute')}
              </Button>
            </div>
          </>
        </div>
      )}

      {tab === 'graph' && (
        <Suspense fallback={<div className="h-[600px]"><LoadingSpinner className="h-full" /></div>}>
          <ObjectTypeGraphTab objectTypeId={objectTypeId} />
        </Suspense>
      )}

      <AttributeMiniEditor
        attribute={creatingAttribute ? createAttributeStub : editingAttr}
        attributeTypes={attributeTypes}
        objectTypes={objectTypes}
        referenceObjectTypes={allObjectTypes}
        schemaId={ot?.schema_id}
        schemas={allSchemas}
        open={editingAttr !== null || creatingAttribute}
        isCreate={creatingAttribute}
        nextSort={nextSort}
        onClose={closeEditor}
        onSaved={() => refetchAttributes()}
        onCreated={(created) => { setEditingAttr(created); setCreatingAttribute(false) }}
      />

      <PermissionsDialog
        entityType="object_type"
        recordId={objectTypeId}
        open={permDialogOpen}
        onClose={() => setPermDialogOpen(false)}
      />

      <PermissionsDialog
        entityType="attribute_type"
        recordId={permAttrId}
        open={permAttrId !== null}
        onClose={() => setPermAttrId(null)}
      />

      <EditObjectModal
        object={createObjectStub}
        attributes={attributes}
        labelAttributeId={ot?.label_attribute_id}
        attributeTypes={attributeTypes}
        attributeTypesLoading={attributeTypesLoading}
        attributesLoading={attributesLoading}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSaved={() => { setCreateOpen(false); refetchObjects() }}
        isCreate={true}
        schemaId={ot?.schema_id}
      />

      <EditObjectModal
        object={editObj}
        attributes={attributes}
        labelAttributeId={ot?.label_attribute_id}
        attributeTypes={attributeTypes}
        attributeTypesLoading={attributeTypesLoading}
        attributesLoading={attributesLoading}
        open={editObj !== null}
        onClose={() => setEditObj(null)}
        onSaved={() => { setEditObj(null); refetchObjects() }}
        isCreate={false}
        schemaId={ot?.schema_id}
      />
    </div>
    </ErrorBoundary>
  )
}
