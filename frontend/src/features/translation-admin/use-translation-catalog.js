import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { useUiLanguage } from '../../shared/hooks/use-ui-language'
import { i18n } from '../../shared/lib/i18n'

const DICTIONARY_STALE_TIME = 5 * 60 * 1000

function fetchUiDictionary(code) {
  return api.get('ui-translations/dictionary', { params: { lang: code } }).then((response) => response.data)
}

// D-01/D-02/D-03/D-04 — server-paginated, filtered catalog query. `namespace`
// distinguishes three cases: All (both namespace/namespaceIsNull omitted),
// a named namespace (`namespace` sent as-is), or the server-provided nullable
// option (`namespaceIsNull: true` sends `namespace_is_null=true`, `namespace`
// omitted). Query key mirrors every param that changes the server response.
function buildQueryKey({ editorLanguage, namespace, namespaceIsNull, q, skip, limit, onlyMissing }) {
  return [
    'translation-catalog',
    {
      editorLanguage,
      namespace: namespaceIsNull ? null : (namespace || null),
      namespaceIsNull: !!namespaceIsNull,
      q: q || '',
      skip,
      limit,
      onlyMissing: !!onlyMissing,
    },
  ]
}

/**
 * Feature-local catalog query + per-field serialized autosave/reset state
 * machine for the Translation admin page (D-08..D-14, T-233-12, T-233-13).
 *
 * Consumers (TranslationAdminPage/TranslationCatalogTable) supply the
 * already-debounced/normalized query params; this hook owns:
 *  - the GET ui-translations/catalog query (never refetched after a mutation)
 *  - one in-flight PUT/DELETE per `${rowId}:${language}` key, with a queued
 *    latest draft sent immediately after the in-flight request settles
 *  - exact-row/coverage cache updates via queryClient.setQueryData
 *  - exact `['ui-strings', editorLanguage]` invalidation on every success,
 *    plus an active-runtime-bundle refresh (fetchQuery + addResourceBundle)
 *    only when the edited language is the active runtime UI language
 */
export function useTranslationCatalog({ editorLanguage, namespace, namespaceIsNull, q, skip, limit, onlyMissing }) {
  const queryClient = useQueryClient()
  const uiLanguage = useUiLanguage()
  const runtimeLanguage = uiLanguage?.runtimeLanguage

  const queryKey = useMemo(
    () => buildQueryKey({ editorLanguage, namespace, namespaceIsNull, q, skip, limit, onlyMissing }),
    [editorLanguage, namespace, namespaceIsNull, q, skip, limit, onlyMissing]
  )

  const query = useQuery({
    queryKey,
    queryFn: () => api.get('ui-translations/catalog', {
      params: {
        editor_language: editorLanguage,
        namespace: namespaceIsNull ? undefined : (namespace || undefined),
        namespace_is_null: namespaceIsNull ? true : undefined,
        q: q || undefined,
        skip,
        limit,
        only_missing: onlyMissing ? true : undefined,
      },
    }).then((response) => response.data),
    enabled: !!editorLanguage,
    placeholderData: keepPreviousData,
  })

  // Per-`${rowId}:${language}` UI status only (draft text stays local to the
  // table's controlled inputs) — { status: 'idle'|'saving'|'saved'|'error', error }.
  const [fieldStatus, setFieldStatus] = useState({})
  const inFlightRef = useRef({})
  const queuedRef = useRef({})

  const patchStatus = useCallback((key, patch) => {
    setFieldStatus((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }))
  }, [])

  const clearSavedStatus = useCallback((key) => {
    setFieldStatus((prev) => {
      if (prev[key]?.status !== 'saved') return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])

  const applyRowSuccess = useCallback((rowId, language, rowPatch, coverageDelta) => {
    queryClient.setQueryData(queryKey, (old) => {
      if (!old) return old
      const items = (old.items || []).map((item) => (item.id === rowId ? { ...item, ...rowPatch } : item))
      const coverage = coverageDelta
        ? (old.coverage || []).map((entry) => (
          entry.code === language
            ? { ...entry, translated: Math.min(entry.total, Math.max(0, entry.translated + coverageDelta)) }
            : entry
        ))
        : old.coverage
      return { ...old, items, coverage }
    })
  }, [queryClient, queryKey])

  // T-233-13 — every success exact-invalidates the edited dictionary; only an
  // active-runtime-language edit also fetches and overwrites the singleton
  // systemui resource bundle, so an inactive-language edit never leaves a
  // later-activated language serving a stale cached dictionary.
  const syncDictionary = useCallback(async (language) => {
    const dictionaryKey = ['ui-strings', language]
    await queryClient.invalidateQueries({ queryKey: dictionaryKey, exact: true })
    if (!runtimeLanguage || language !== runtimeLanguage) return
    const dictionary = await queryClient.fetchQuery({
      queryKey: dictionaryKey,
      queryFn: () => fetchUiDictionary(language),
      staleTime: DICTIONARY_STALE_TIME,
    })
    i18n.addResourceBundle(language, 'systemui', dictionary.strings, true, true)
  }, [queryClient, runtimeLanguage])

  // Reads the row's current cached stored_value right before this specific
  // request is sent (not a value captured once at drainQueue's start) so a
  // coverage-delta computed for the Nth queued edit in one batch reflects the
  // (N-1)th edit's already-applied result, not stale pre-batch state (CR-02).
  const getCurrentStoredValue = useCallback((rowId, fallback) => {
    const cached = queryClient.getQueryData(queryKey)
    const cachedRow = cached?.items?.find((item) => item.id === rowId)
    return cachedRow ? cachedRow.stored_value : fallback
  }, [queryClient, queryKey])

  const sendMutation = useCallback(async (row, language, value) => {
    const trimmed = value.trim()
    const isDelete = trimmed === ''
    const priorStoredValue = getCurrentStoredValue(row.id, row.stored_value)
    const priorExplicit = !!(priorStoredValue && priorStoredValue.trim() !== '')
    let rowPatch

    if (isDelete) {
      await api.delete(`ui-translations/keys/${row.id}/translations/${language}`)
      // The post-delete effective value may fall back to an explicit English
      // row rather than the code default (three-tier chain) — re-resolve the
      // single row server-side rather than assuming code_default (CR-01).
      const refreshed = await api.get('ui-translations/catalog', {
        params: { editor_language: language, ui_string_id: row.id, skip: 0, limit: 1 },
      }).then((response) => response.data)
      const freshRow = refreshed.items?.[0]
      rowPatch = freshRow
        ? { stored_value: freshRow.stored_value, effective_value: freshRow.effective_value, fallback_source: freshRow.fallback_source }
        : { stored_value: null, effective_value: row.default_text, fallback_source: 'code_default' }
    } else {
      const response = await api.put('translations/', {
        translation_type: 'systemui',
        entity_id: row.id,
        field: 'value',
        language_code: language,
        value,
      })
      const savedValue = response.data?.value ?? value
      rowPatch = { stored_value: savedValue, effective_value: savedValue, fallback_source: 'explicit' }
    }

    const nowExplicit = !isDelete
    const coverageDelta = priorExplicit === nowExplicit ? 0 : (nowExplicit ? 1 : -1)
    applyRowSuccess(row.id, language, rowPatch, coverageDelta)
    await syncDictionary(language)
    return rowPatch
  }, [applyRowSuccess, syncDictionary, getCurrentStoredValue])

  // D-14/T-233-12 — one in-flight request per key; a draft that arrives while
  // a request is in flight replaces the queue slot (never sent concurrently)
  // and is sent immediately after the current request settles successfully.
  const drainQueue = useCallback(async (row, language, key) => {
    for (;;) {
      const pending = queuedRef.current[key]
      if (pending === undefined) break
      delete queuedRef.current[key]
      inFlightRef.current[key] = true
      patchStatus(key, { status: 'saving', error: null })
      try {
        await sendMutation(row, language, pending.value)
        patchStatus(key, { status: 'saved', error: null })
        setTimeout(() => clearSavedStatus(key), 1500)
      } catch (err) {
        inFlightRef.current[key] = false
        patchStatus(key, { status: 'error', error: err })
        return
      }
    }
    inFlightRef.current[key] = false
  }, [sendMutation, patchStatus, clearSavedStatus])

  const commitField = useCallback((row, language, value) => {
    const key = `${row.id}:${language}`
    queuedRef.current[key] = { value }
    if (inFlightRef.current[key]) return
    drainQueue(row, language, key)
  }, [drainQueue])

  const getFieldStatus = useCallback((rowId, language) => (
    fieldStatus[`${rowId}:${language}`] || { status: 'idle', error: null }
  ), [fieldStatus])

  return {
    catalog: query.data,
    isLoading: query.isLoading,
    isError: query.isError,
    isFetching: query.isFetching,
    refetch: query.refetch,
    commitField,
    getFieldStatus,
  }
}
