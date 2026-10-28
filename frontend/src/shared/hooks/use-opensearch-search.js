import { useRef, useCallback, useState } from 'react'
import useDebouncedSearch from './use-debounced-search'
import api from './use-api'

export function useOpenSearchSearch(initialFilters = {}, initialQuery = '') {
  const filtersRef = useRef(initialFilters)
  // Bumping this key re-runs the debounced effect even when `query` is
  // unchanged — filters live in filtersRef, so a filter-only change must
  // still fire a new search. (setQuery(sameValue) would bail out: React
  // skips re-renders when the state value is identical.)
  const [refreshKey, setRefreshKey] = useState(0)

  const fetcher = useCallback(async (q, signal) => {
    const res = await api.get('/search', {
      signal,
      params: { q, ...filtersRef.current },
    })
    return res.data
  }, []) // stable ref — filters read from ref, not closure

  const { query, setQuery, results, isLoading, error } = useDebouncedSearch({
    fetcher,
    debounceMs: 300,
    minChars: 2,
    enabled: true,
    initialQuery,
    refreshKey,
  })

  const setFilters = useCallback((newFilters) => {
    filtersRef.current = { ...filtersRef.current, ...newFilters }
    // Re-trigger search with new filters if query is active
    if (query.length >= 2) {
      setRefreshKey((k) => k + 1)
    }
  }, [query])

  // Flatten: results?.results is the actual array from the OpenSearch response
  const items = results?.results || []
  const total = results?.total || 0

  return { query, setQuery, results: items, total, isLoading, error, setFilters }
}
