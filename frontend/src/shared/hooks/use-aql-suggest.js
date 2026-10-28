import { useState, useEffect, useRef, useMemo } from 'react'
import Fuse from 'fuse.js'

import api from './use-api'
import { getPartialToken, isInsideQuote } from '../lib/aql-token-boundary'

const CACHE_LIMIT = 50
const DEFAULT_DEBOUNCE_MS = 400
const FUSE_THRESHOLD = 0.3
const FUSE_DISTANCE = 100
const FUSE_MIN_MATCH_CHAR_LENGTH = 1

// Debounced, aborted, LRU-cached suggest-fetch hook (AC-06/AC-11, D-05/D-08/
// D-09/D-10). Modeled on use-debounced-search.js's debounce + AbortController
// + unmount-cleanup skeleton, extended with an LRU cache, in-flight-promise
// dedupe and an unchanged-key skip. The backend re-slices qlQuery[:cursorPos]
// itself, so we send the FULL textarea value + the real cursorPos (RESEARCH A2).
//
// Fail-silent contract (UI-SPEC "Error state"): any non-abort error resolves
// to [] — never surface an error row.
export default function useAqlSuggest(qlQuery, cursorPos, { debounceMs = DEFAULT_DEBOUNCE_MS } = {}) {
  const [suggestions, setSuggestions] = useState([])

  const cacheRef = useRef(new Map()) // key (`${qlQuery}|${cursorPos}`) -> suggestions[]
  const inFlightRef = useRef(new Map()) // key -> Promise (dedupe concurrent requests, D-09)
  const lastKeyRef = useRef(null) // last key actually fetched — skip repeats (D-10)
  const controllerRef = useRef(null)

  useEffect(() => {
    const key = `${qlQuery}|${cursorPos}`

    // D-05: no pre-cursor text — nothing meaningful to suggest.
    if (!qlQuery.slice(0, cursorPos).trim()) {
      setSuggestions([])
      lastKeyRef.current = null // invalidate — a retype of an old key must refetch (CR-01)
      return
    }

    // D-09: LRU cache hit — serve synchronously, no request.
    const cached = cacheRef.current.get(key)
    if (cached) {
      setSuggestions(cached)
      lastKeyRef.current = key
      return
    }

    // D-10: (query, cursorPos) unchanged since the last fetch — nothing to do.
    if (key === lastKeyRef.current) return

    const timer = setTimeout(async () => {
      // Debounce window elapsed — cancel any stale in-flight request.
      if (controllerRef.current) {
        controllerRef.current.abort()
      }
      const controller = new AbortController()
      controllerRef.current = controller

      // D-09: in-flight dedupe — a request for this key is already pending.
      let promise = inFlightRef.current.get(key)
      if (!promise) {
        promise = api
          .post('/aql/suggest', { qlQuery, cursorPos }, { signal: controller.signal })
          .then((res) => res.data.suggestions || [])
        inFlightRef.current.set(key, promise)
      }

      try {
        const result = await promise
        if (!controller.signal.aborted) {
          if (cacheRef.current.size >= CACHE_LIMIT) {
            const oldestKey = cacheRef.current.keys().next().value
            cacheRef.current.delete(oldestKey)
          }
          cacheRef.current.set(key, result)
          setSuggestions(result)
          lastKeyRef.current = key
        }
      } catch (err) {
        const isAbort = err?.name === 'AbortError' || err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED'
        if (!isAbort && !controller.signal.aborted) {
          setSuggestions([]) // fail silently per UI-SPEC — never surface an error
          lastKeyRef.current = null // invalidate — a retype of an old key must refetch (CR-01)
        }
      } finally {
        inFlightRef.current.delete(key)
      }
    }, debounceMs)

    return () => clearTimeout(timer)
  }, [qlQuery, cursorPos, debounceMs])

  useEffect(() => {
    return () => {
      if (controllerRef.current) {
        controllerRef.current.abort()
      }
    }
  }, [])

  // Client-side fuse filter: the backend only prefix-filters objectType-value
  // rows; field/keyword/operator/ref-fn lists come back unfiltered (RESEARCH).
  // Narrow them by the partial token for UX only (T-195-02 — this never adds
  // or relaxes permission logic). Empty partial token -> raw, unfiltered.
  // The partial token includes a leading `"` when the caret sits inside an
  // unterminated STRING (findTokenStart's replacement range must include it
  // for applySuggestion) — strip it for the fuse pattern, mirroring the
  // backend's own name_prefix quote-strip for objectType-value rows, so a
  // quoted partial like `"Na` still matches the unquoted field value `Name`.
  const partialToken = getPartialToken(qlQuery, cursorPos)
  const fusePattern = partialToken.startsWith('"') ? partialToken.slice(1) : partialToken

  // Whitespace-boundary display gate: outside a quoted string, the dropdown
  // should only appear once the user has just finished a token (space, an
  // opening paren, or an operator character) — not while still typing the
  // letters of a bare keyword/field/value word. A just-closed `"` or `)`
  // is deliberately NOT treated as a boundary by itself — both close off a
  // complete sub-unit (a value / a nested expression) rather than opening a
  // new one, so a real whitespace must follow before suggestions reappear
  // (quote-parity is handled separately below via isInsideQuote). This keeps
  // Enter free to submit the query instead of accepting a stale/incidental
  // highlighted suggestion (D-13's Enter-gating relies on
  // `items.length === 0` meaning "nothing to accept"). objectType-value rows
  // are exempt: they legitimately appear right after `=`/`(` with no space
  // (e.g. `objectType=` or `objectTypeAndChildren(`) and are always shown as
  // soon as the caret enters the quoted-value position, mirroring the
  // pre-quote behavior.
  const lastChar = qlQuery.charAt(cursorPos - 1)
  const atTokenBoundary = /[\s(=<>!~]/.test(lastChar)
  const onlyValueSuggestions = suggestions.length > 0 && suggestions.every((s) => s.kind === 'objectType-value')
  const suppressForBoundary = !isInsideQuote(qlQuery, cursorPos) && !atTokenBoundary && !onlyValueSuggestions

  const filteredSuggestions = useMemo(() => {
    if (suppressForBoundary) return []
    if (!fusePattern || suggestions.length === 0) return suggestions
    const fuse = new Fuse(suggestions, {
      keys: ['value'],
      threshold: FUSE_THRESHOLD,
      distance: FUSE_DISTANCE,
      minMatchCharLength: FUSE_MIN_MATCH_CHAR_LENGTH,
    })
    return fuse.search(fusePattern).map((r) => r.item)
  }, [fusePattern, suggestions, suppressForBoundary])

  return filteredSuggestions
}
