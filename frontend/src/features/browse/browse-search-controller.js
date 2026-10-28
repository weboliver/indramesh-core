import { useState, useRef, useEffect, useCallback } from 'react'

/**
 * Shared search controller (Phase 252, UX-06).
 *
 * The single baseline implementation of the D-02 search contract: 300ms
 * debounce, a two-character minimum before a non-empty query is applied, a
 * URL-synced `setQ` callback (whose call sites also reset pagination to
 * page 1), and a clear action that restores the unfiltered state.
 *
 * `BrowseSearchInput` renders it as the standard input control; SearchPage's
 * `AqlBrowseSearchInput` renders it as the autocomplete textarea, so both
 * modes share the exact same debounce/gate/clear/URL-sync semantics.
 */
export function resolveQueryAction(value, urlQ) {
  if (value.length >= 2 || value === '') return { type: 'set', value }
  // Backspacing a 2+ char query down to exactly 1 char: treat like clearing
  // so the results list doesn't stay stale relative to the visible input.
  if (urlQ) return { type: 'set', value: '' }
  return { type: 'none' }
}

export function useBrowseSearchController({ urlQ, setQ, initialValue }) {
  const [searchInput, setSearchInput] = useState(initialValue ?? urlQ ?? '')
  const timerRef = useRef(null)
  const isInternalRef = useRef(false)

  // Sync from URL changes (browser back/forward, external writes) — but never
  // on mount: the effective initial value above (URL param OR restored last
  // query) must win over the possibly-empty raw URL param. The previous-URL
  // ref keeps the mount pass inert (identical on mount, different on real
  // navigation), matching SearchPage's established StrictMode-safe pattern.
  const prevUrlQ = useRef(urlQ)
  useEffect(() => {
    if (prevUrlQ.current === urlQ) return
    prevUrlQ.current = urlQ
    if (!isInternalRef.current && urlQ !== searchInput) {
      setSearchInput(urlQ || '')
    }
  }, [urlQ]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleChange = useCallback((value) => {
    setSearchInput(value)

    if (timerRef.current) clearTimeout(timerRef.current)

    timerRef.current = setTimeout(() => {
      isInternalRef.current = true
      const action = resolveQueryAction(value, urlQ)
      if (action.type === 'set') {
        setQ(action.value, { replace: true })
      }
      setTimeout(() => { isInternalRef.current = false }, 0)
    }, 300)
  }, [setQ, urlQ])

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [])

  const handleClear = useCallback(() => {
    setSearchInput('')
    if (timerRef.current) clearTimeout(timerRef.current)
    isInternalRef.current = true
    setQ('', { replace: true })
    setTimeout(() => { isInternalRef.current = false }, 0)
  }, [setQ])

  // Imperative setter for programmatic value changes (AQL suggestion accept)
  // that must land in the visible control synchronously, before the URL
  // round-trip would propagate it.
  const setValue = useCallback((value) => {
    setSearchInput(value)
  }, [])

  return { searchInput, handleChange, handleClear, setValue }
}