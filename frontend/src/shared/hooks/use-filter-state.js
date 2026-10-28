import { useSearchParams } from 'react-router-dom'
import { useCallback, useMemo, useEffect } from 'react'

function isConditionComplete(cond) {
  if (!cond || !cond.attr_id) return false
  if (cond.op === 'between') {
    return cond.val !== '' && cond.val !== null && cond.val !== undefined && cond.val2 !== '' && cond.val2 !== null && cond.val2 !== undefined
  }
  return cond.val !== '' && cond.val !== null && cond.val !== undefined
}

// Strips incomplete conditions (blank attribute/value, e.g. a freshly added
// row the user hasn't filled in yet) and now-empty groups before the tree is
// sent to the backend — the raw filterTree (with placeholder rows) stays in
// the URL untouched so the builder UI keeps rendering them for editing.
function sanitizeFilterTree(tree) {
  if (!tree) return null
  const conditions = (tree.conditions || []).filter(isConditionComplete)
  const groups = (tree.groups || []).map(sanitizeFilterTree).filter(Boolean)
  if (conditions.length === 0 && groups.length === 0) return null
  return { op: tree.op || 'AND', conditions, groups }
}

export function useFilterState(defaultPageSize = 25) {
  const [searchParams, setSearchParams] = useSearchParams()

  // --- Read values from URL search params ---
  const q = useMemo(() => searchParams.get('q') || '', [searchParams])

  const filters = useMemo(() => {
    const raw = searchParams.get('attr_filters')
    if (!raw) return {}
    try {
      return JSON.parse(raw)
    } catch {
      return {}
    }
  }, [searchParams])

  const filterTree = useMemo(() => {
    const raw = searchParams.get('filter_tree')
    if (!raw) return null
    try {
      return JSON.parse(raw)
    } catch {
      return null
    }
  }, [searchParams])

  const page = useMemo(() => {
    const p = parseInt(searchParams.get('page'), 10)
    return Number.isFinite(p) && p >= 1 ? p : 1
  }, [searchParams])

  const pageSize = useMemo(() => {
    const s = parseInt(searchParams.get('page_size'), 10)
    return [10, 25, 50, 100].includes(s) ? s : defaultPageSize
  }, [searchParams, defaultPageSize])

  const sortBy = useMemo(() => searchParams.get('sort_by') || 'name', [searchParams])

  const sortOrder = useMemo(() => {
    const v = searchParams.get('sort_order')
    return v === 'desc' ? 'desc' : 'asc'
  }, [searchParams])

  // --- Setters ---
  const setQ = useCallback((newQ, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (newQ) {
        next.set('q', String(newQ))
      } else {
        next.delete('q')
      }
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const setFilter = useCallback((attrId, value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      const current = {}
      const raw = prev.get('attr_filters')
      if (raw) {
        try {
          Object.assign(current, JSON.parse(raw))
        } catch {
          // corrupted attr_filters — start fresh
        }
      }
      current[attrId] = value
      next.set('attr_filters', JSON.stringify(current))
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const removeFilter = useCallback((attrId) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      const current = {}
      const raw = prev.get('attr_filters')
      if (raw) {
        try {
          Object.assign(current, JSON.parse(raw))
        } catch {
          // ignore parse failures
        }
      }
      delete current[attrId]
      if (Object.keys(current).length > 0) {
        next.set('attr_filters', JSON.stringify(current))
      } else {
        next.delete('attr_filters')
      }
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const clearFilters = useCallback(() => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('attr_filters')
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const setFilterTree = useCallback((newTree) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (newTree && ((newTree.conditions && newTree.conditions.length > 0) || (newTree.groups && newTree.groups.length > 0))) {
        next.set('filter_tree', JSON.stringify(newTree))
        next.delete('attr_filters')
      } else {
        next.delete('filter_tree')
      }
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const setPage = useCallback((newPage) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('page', String(newPage))
      return next
    })
  }, [setSearchParams])

  const setPageSize = useCallback((newSize) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('page_size', String(newSize))
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const setSort = useCallback((field, order = 'asc') => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('sort_by', field)
      next.set('sort_order', order === 'desc' ? 'desc' : 'asc')
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  // D-10: One-time migration from attr_filters to filter_tree
  useEffect(() => {
    const hasFilterTree = searchParams.has('filter_tree')
    const hasAttrFilters = searchParams.has('attr_filters')
    if (!hasFilterTree && hasAttrFilters) {
      const raw = searchParams.get('attr_filters')
      try {
        const flatFilters = JSON.parse(raw)
        if (flatFilters && typeof flatFilters === 'object' && Object.keys(flatFilters).length > 0) {
          const conditions = Object.entries(flatFilters).map(([attr_id, val]) => ({
            attr_id, op: 'eq', val: String(val)
          }))
          const migratedTree = { op: 'AND', conditions, groups: [] }
          setSearchParams(prev => {
            const next = new URLSearchParams(prev)
            next.set('filter_tree', JSON.stringify(migratedTree))
            next.delete('attr_filters')
            next.set('page', '1')
            return next
          }, { replace: true })
        }
      } catch {
        // Corrupted attr_filters — just clear it
        setSearchParams(prev => {
          const next = new URLSearchParams(prev)
          next.delete('attr_filters')
          return next
        }, { replace: true })
      }
    }
  }, [searchParams, setSearchParams])

  // --- Derived values ---
  const skip = useMemo(() => (page - 1) * pageSize, [page, pageSize])

  const queryParams = useMemo(() => {
    const sanitizedTree = sanitizeFilterTree(filterTree)
    return {
      q: q || undefined,
      filter_tree: sanitizedTree ? JSON.stringify(sanitizedTree) : null,
      attr_filters: (!sanitizedTree && Object.keys(filters).length > 0) ? { ...filters } : null,
      sort_by: sortBy,
      sort_order: sortOrder,
      skip,
      limit: pageSize,
    }
  }, [q, filters, filterTree, sortBy, sortOrder, skip, pageSize])

  return {
    q, setQ,
    filters, setFilter, removeFilter, clearFilters,
    filterTree, setFilterTree,
    page, pageSize, setPage, setPageSize,
    sortBy, sortOrder, setSort,
    queryParams,
    skip,
  }
}
