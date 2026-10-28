import { useState, useEffect, useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

const BASE_COLUMNS = [
  { id: 'name', labelKey: 'common.name', defaultLabel: 'Name', isBase: true },
  { id: 'actions', labelKey: 'common.actions', defaultLabel: 'Actions', isBase: true },
]

function storageKey(objectTypeId) {
  return `indramesh-ot-columns-${objectTypeId}`
}

function persist(objectTypeId, visibleColumns, columnOrder) {
  try {
    localStorage.setItem(storageKey(objectTypeId), JSON.stringify({
      v: 2,
      visible: visibleColumns,
      order: columnOrder,
    }))
  } catch {
    // localStorage full or unavailable — silently degrade (config lost on refresh)
  }
}

const BASE_LABELS = new Set(BASE_COLUMNS.map(c => c.defaultLabel.toLowerCase()))

function buildAllColumns(attributes, t) {
  const translatedBaseLabels = {
    name: t('common.name', 'Name'),
    actions: t('common.actions', 'Actions'),
  }

  return [
    ...BASE_COLUMNS
      .filter(c => c.id !== 'actions')
      .map(column => ({ ...column, label: translatedBaseLabels[column.id] })),
    ...attributes
      .filter(attr => !BASE_LABELS.has(attr.name.toLowerCase()))
      .map(attr => ({
        id: `attr-${attr.id}`,
        label: attr.name,
        isBase: false,
        attributeId: attr.id,
        is_inherited: attr.is_inherited === true,
        definingObjectTypeName: attr.defining_object_type_name || '',
      })),
    {
      ...BASE_COLUMNS.find(c => c.id === 'actions'),
      label: translatedBaseLabels.actions,
    },
  ]
}

export function useTableColumns(objectTypeId, attributes, attributesLoading = false) {
  const { t } = useTranslation()
  const [columnOrder, setColumnOrder] = useState([])
  const [visibleColumns, setVisibleColumns] = useState([])
  const [initialized, setInitialized] = useState(false)

  useEffect(() => {
    if (!objectTypeId) return

    const allCols = buildAllColumns(attributes, t)
    const allColIds = allCols.map(c => c.id)

    let stored = null
    try {
      const raw = localStorage.getItem(storageKey(objectTypeId))
      stored = raw ? JSON.parse(raw) : null
    } catch {
      stored = null
    }

    let nextOrder
    let nextVisible

    if (stored) {
      if (attributesLoading) {
        // Attributes are still being fetched (attributes is empty on first
        // paint). Preserve the stored ids verbatim so attribute/reference columns
        // are not dropped before they can resolve. Ids that no longer exist are
        // harmless here — the columns memo (below) drops any id absent from
        // allColumns, so they simply stay hidden until attributes arrive.
        nextOrder = stored.order
        nextVisible = stored.visible
      } else {
        // Attributes are known — reconcile and drop ids for deleted attributes.
        const storedOrderFiltered = stored.order.filter(id => allColIds.includes(id))
        const newIds = allColIds.filter(id => !stored.order.includes(id))
        const actionsIdx = storedOrderFiltered.indexOf('actions')

        if (actionsIdx === -1) {
          // 'actions' missing from stored order (corrupt/old data) — append at end
          nextOrder = [...storedOrderFiltered, ...newIds, 'actions']
        } else {
          // Insert newly-added attribute columns right before 'actions', wherever
          // the user previously positioned it — don't force-relocate 'actions' itself.
          nextOrder = [
            ...storedOrderFiltered.slice(0, actionsIdx),
            ...newIds,
            ...storedOrderFiltered.slice(actionsIdx),
          ]
        }

        nextVisible = stored.visible.filter(id => allColIds.includes(id))
        if (!nextVisible.includes('name')) nextVisible = ['name', ...nextVisible]
        if (!nextVisible.includes('actions')) nextVisible = [...nextVisible, 'actions']

        // One-time migration of pre-141 stored prefs (payloads without v: 2 never
        // contained inherited ids in `visible` — the old alwaysVisible lock made
        // them impossible to add; they rendered only via forcedVisible). Union the
        // inherited ids in so they don't vanish for users with saved prefs, and
        // stamp the upgrade by re-persisting.
        if (stored.v !== 2) {
          const inheritedIds = allCols
            .filter(c => c.is_inherited)
            .map(c => c.id)
          const merged = [...nextVisible]
          for (const id of inheritedIds) {
            if (!merged.includes(id)) merged.push(id)
          }
          nextVisible = merged
          persist(objectTypeId, nextVisible, nextOrder)
        }
      }
    } else {
      nextOrder = allColIds
      const inheritedIds = allCols.filter(c => c.is_inherited).map(c => c.id)
      nextVisible = ['name', 'actions', ...inheritedIds]
    }

    setColumnOrder(nextOrder)
    setVisibleColumns(nextVisible)
    setInitialized(true)
  }, [objectTypeId, attributes, attributesLoading, t])

  const allColumns = useMemo(() => buildAllColumns(attributes, t), [attributes, t])

  const columns = useMemo(() => {
    // Base columns (name/actions) always render regardless of visibleColumns.
    const forcedVisible = new Set(
      allColumns.filter(c => c.isBase).map(c => c.id)
    )
    return columnOrder
      .filter(id => visibleColumns.includes(id) || forcedVisible.has(id))
      .map(id => allColumns.find(c => c.id === id))
      .filter(Boolean)
  }, [columnOrder, visibleColumns, allColumns])

  const toggleColumn = useCallback((columnId) => {
    const col = allColumns.find(c => c.id === columnId)
    if (!col || col.isBase) return

    setVisibleColumns(prev => {
      const next = prev.includes(columnId)
        ? prev.filter(id => id !== columnId)
        : [...prev, columnId]
      persist(objectTypeId, next, columnOrder)
      return next
    })
  }, [allColumns, objectTypeId, columnOrder])

  const moveColumn = useCallback((fromColId, toColId) => {
    setColumnOrder(prev => {
      const fromIndex = prev.indexOf(fromColId)
      const toIndex = prev.indexOf(toColId)
      if (fromIndex === -1 || toIndex === -1 || fromIndex === toIndex) return prev
      const next = [...prev]
      const [moved] = next.splice(fromIndex, 1)
      next.splice(toIndex, 0, moved)
      persist(objectTypeId, visibleColumns, next)
      return next
    })
  }, [objectTypeId, visibleColumns])

  const resetToDefaults = useCallback(() => {
    try {
      localStorage.removeItem(storageKey(objectTypeId))
    } catch {
      // ignore
    }
    const allCols = buildAllColumns(attributes, t)
    setColumnOrder(allCols.map(c => c.id))
    setVisibleColumns(['name', 'actions', ...allCols.filter(c => c.is_inherited).map(c => c.id)])
  }, [objectTypeId, attributes, t])

  return {
    columns,
    allColumns,
    toggleColumn,
    moveColumn,
    resetToDefaults,
    initialized,
  }
}
