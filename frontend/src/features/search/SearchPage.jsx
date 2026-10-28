import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useOpenSearchSearch } from '../../shared/hooks/use-opensearch-search'
import { useSources } from '../../shared/hooks/use-sources'
import { useSchemas } from '../../shared/hooks/use-schemas'
import { useObjectTypes } from '../../shared/hooks/use-object-types'
import SegmentedControl from '../../shared/components/SegmentedControl'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { useAqlSearch } from '../../shared/hooks/use-aql-search'
import useAqlSuggest from '../../shared/hooks/use-aql-suggest'
import { applySuggestion } from '../../shared/lib/aql-token-boundary'
import AqlSuggestDropdown from '../../shared/components/AqlSuggestDropdown'
import AqlSyntaxHelpModal from '../../shared/components/AqlSyntaxHelpModal'
import HighlightedText from '../../shared/utils/highlight-text'
import { unescapeHtmlEntities } from '../../shared/utils/html-entities'
import { BrowseSearchControl } from '../../features/browse/BrowseSearchInput'
import { useBrowseSearchController } from '../../features/browse/browse-search-controller'
import Search from 'lucide-react/dist/esm/icons/search'
import SearchX from 'lucide-react/dist/esm/icons/search-x'
import HelpCircle from 'lucide-react/dist/esm/icons/help-circle'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

const LAST_PLAIN_QUERY_KEY = 'indramesh-search-last-plain-query'
const LAST_AQL_QUERY_KEY = 'indramesh-search-last-aql-query'

function readLastQuery(key) {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    // localStorage unavailable (private browsing, etc.) — degrade to empty
    return ''
  }
}

function writeLastQuery(key, value) {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch {
    // localStorage full or unavailable — silently degrade (nothing to remember)
  }
}

function flattenHierarchy(objectTypes) {
  const byParent = new Map()
  for (const ot of objectTypes) {
    const key = ot.parent_id ?? null
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(ot)
  }
  for (const children of byParent.values()) {
    children.sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0))
  }
  const result = []
  function visit(parentId, level) {
    for (const ot of byParent.get(parentId) ?? []) {
      result.push({ ot, level })
      visit(ot.id, level + 1)
    }
  }
  visit(null, 0)
  return result
}

/**
 * AQL mode adapter (Phase 252, UX-06). Renders the existing autocomplete
 * textarea from the shared BrowseSearchInput controller: the controller owns
 * the 300ms debounce, two-character gate, clear action, and URL-sync commit
 * (setAqlQ), while immediate draft/caret updates flow to useAqlSuggest and
 * the Enter/Arrow/Tab/Escape/selection handlers stay attached to the textarea
 * so autocomplete behavior is retained.
 */
function AqlBrowseSearchInput({ controller, textareaRef, onCursor, onKeyUp, onKeyDown, onBlur, placeholder, dropdown, testId = 'search-input' }) {
  const { t } = useTranslation()

  return (
    <div className="relative w-full">
      <textarea
        ref={textareaRef}
        value={controller.searchInput}
        onChange={(e) => {
          controller.handleChange(e.target.value)
          onCursor(e)
        }}
        onKeyDown={onKeyDown}
        onSelect={onCursor}
        onClick={onCursor}
        // Defensive cursor-only fallback for caret-only key moves (Pitfall 1).
        // Deliberately does NOT clear suggestDismissed — the keyup of the
        // Escape key itself would otherwise instantly re-open the dropdown the
        // keydown just dismissed (D-13).
        onKeyUp={onKeyUp}
        onBlur={onBlur}
        placeholder={placeholder}
        rows={1}
        className="w-full resize-y rounded-md border border-input bg-background px-4 pr-8 py-3 text-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
        autoFocus
        data-testid={testId}
      />
      {controller.searchInput && (
        <button
          type="button"
          onClick={controller.handleClear}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
          aria-label={t('browse.search.clear', 'Clear search')}
          data-testid="browse-search-clear"
        >
          <LucideIcon name="x" size={14} />
        </button>
      )}
      {dropdown}
    </div>
  )
}

export default function SearchPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  // Filters persist in URL params (source_id/schema_id/object_type_id) like
  // q/aql/mode — they survive reload and deep links and reset page to 1.
  const [filters, setFilters] = useState(() => ({
    source_id: searchParams.get('source_id') || undefined,
    schema_id: searchParams.get('schema_id') || undefined,
    object_type_id: searchParams.get('object_type_id') || undefined,
  }))
  const { data: sources = [] } = useSources(0, 1000)
  const [selectedSourceId, setSelectedSourceId] = useState(() => searchParams.get('source_id') || '')
  const [selectedSchemaId, setSelectedSchemaId] = useState(() => searchParams.get('schema_id') || '')
  const { data: schemas = [] } = useSchemas(selectedSourceId)
  const { data: objectTypes = [] } = useObjectTypes(selectedSchemaId, 0, 500)

  // Free Text and AQL keep independent query text in separate URL params (`q`
  // vs `aql`) so switching tabs restores each mode's own last query instead of
  // clobbering it with whatever the other tab's input held — and both survive
  // a page refresh regardless of which tab is currently active.
  const urlPlainQuery = useMemo(() => searchParams.get('q') || '', [searchParams])
  const urlAqlQuery = useMemo(() => searchParams.get('aql') || '', [searchParams])

  const modeFromParams = searchParams.get('mode') || 'plain'
  const [mode, setMode] = useState(modeFromParams)

  // AQL results can span ObjectTypes from any schema (objectType is named
  // inline in the query, e.g. `objectType IN (...)`/`objectTypeAndChildren(...)`)
  // — the schema-scoped `objectTypes` above is usually empty in AQL mode since
  // no schema filter is selected there. Fetch the full, unscoped list instead
  // so the results table can resolve every hit's object_type_id to a name.
  const { data: allObjectTypes = [] } = useObjectTypes(undefined, 0, 500, { enabled: mode === 'aql' })

  const handleModeChange = (newMode) => {
    setMode(newMode)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (newMode === 'aql') {
        next.set('mode', 'aql')
      } else {
        next.delete('mode')
      }
      return next
    })
  }

  // A URL param (deep link / refresh with `?q=`/`?aql=` present) always wins;
  // with no param at all, fall back to the last query the user ran, so
  // landing on a bare /search still shows it instead of an empty box.
  const [plainQuery, setPlainQuery] = useState(() => urlPlainQuery || readLastQuery(LAST_PLAIN_QUERY_KEY))
  const [aqlQuery, setAqlQuery] = useState(() => urlAqlQuery || readLastQuery(LAST_AQL_QUERY_KEY))
  // The input box always shows the ACTIVE tab's own text — switching tabs
  // restores whatever was last typed there instead of carrying over the
  // other tab's query.
  const currentQuery = mode === 'aql' ? aqlQuery : plainQuery

  // AQL autocomplete state (Plan 195-04): cursor tracking, dropdown open/
  // highlight/dismiss state, and the textarea DOM ref for caret-anchored
  // positioning + programmatic selection after accept (D-12).
  const [cursorPos, setCursorPos] = useState(0)
  const [activeIndex, setActiveIndex] = useState(0)
  const [suggestDismissed, setSuggestDismissed] = useState(false)
  const [showSyntaxHelp, setShowSyntaxHelp] = useState(false)
  const textareaRef = useRef(null)

  // Keep state in sync with EXTERNAL URL changes (browser back/forward) — but
  // not on mount, where the effective initial value above (URL param OR
  // localStorage fallback) must win over the possibly-empty raw URL param.
  // A boolean first-run guard is NOT safe here: React StrictMode (dev mode)
  // double-invokes effects on mount, so the second pass would wipe the
  // restored initial value with the empty URL param. Compare against the
  // previous URL value instead — identical on mount, different on real
  // back/forward navigation.
  const prevUrlPlainQuery = useRef(urlPlainQuery)
  useEffect(() => {
    if (prevUrlPlainQuery.current === urlPlainQuery) return
    prevUrlPlainQuery.current = urlPlainQuery
    setPlainQuery(urlPlainQuery)
    writeLastQuery(LAST_PLAIN_QUERY_KEY, urlPlainQuery)
  }, [urlPlainQuery])

  const prevUrlAqlQuery = useRef(urlAqlQuery)
  useEffect(() => {
    if (prevUrlAqlQuery.current === urlAqlQuery) return
    prevUrlAqlQuery.current = urlAqlQuery
    setAqlQuery(urlAqlQuery)
    writeLastQuery(LAST_AQL_QUERY_KEY, urlAqlQuery)
  }, [urlAqlQuery])

  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const aql = useAqlSearch(aqlQuery)
  const { setPage: setAqlPage } = aql
  const { setQuery: setEsQuery, results: esResults, total: esTotal, isLoading: esLoading, setFilters: updateSearchFilters } = useOpenSearchSearch(filters, plainQuery)

  // Sync filter state from URL params (deep link / back-forward / explicit
  // reset). Same first-run guard pattern as the query syncs above — the
  // initial state already reflects the URL, so a compare against the previous
  // URL value (identical on mount) keeps this effect inert until the URL
  // actually changes.
  const urlSourceId = searchParams.get('source_id') || ''
  const urlSchemaId = searchParams.get('schema_id') || ''
  const urlObjectTypeId = searchParams.get('object_type_id') || ''
  const prevFilterUrl = useRef(`${urlSourceId}|${urlSchemaId}|${urlObjectTypeId}`)
  useEffect(() => {
    const current = `${urlSourceId}|${urlSchemaId}|${urlObjectTypeId}`
    if (prevFilterUrl.current === current) return
    prevFilterUrl.current = current
    setSelectedSourceId(urlSourceId)
    setSelectedSchemaId(urlSchemaId)
    setFilters({
      source_id: urlSourceId || undefined,
      schema_id: urlSchemaId || undefined,
      object_type_id: urlObjectTypeId || undefined,
    })
    updateSearchFilters({
      source_id: urlSourceId || undefined,
      schema_id: urlSchemaId || undefined,
      object_type_id: urlObjectTypeId || undefined,
    })
  }, [urlSourceId, urlSchemaId, urlObjectTypeId, updateSearchFilters])

  // D-02 query adapters (surface 22, distinct plain q / AQL aql): URL-synced,
  // preserve every unrelated search parameter (incl. mode), reset pagination.
  // The BrowseSearchInput controller calls these AFTER its 300ms debounce and
  // two-character gate; explicit AQL execution calls setAqlQ directly.
  const setPlainQ = useCallback((value, { replace = false } = {}) => {
    setPlainQuery(value)
    setEsQuery(value)
    setPage(1)
    writeLastQuery(LAST_PLAIN_QUERY_KEY, value)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      return next
    }, { replace })
  }, [setEsQuery, setPage, setSearchParams])

  const setAqlQ = useCallback((value, { replace = false } = {}) => {
    setAqlQuery(value)
    setPage(1)
    setAqlPage(1)
    writeLastQuery(LAST_AQL_QUERY_KEY, value)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('aql', value)
      else next.delete('aql')
      return next
    }, { replace })
  }, [setAqlPage, setPage, setSearchParams])

  // Shared controller instances — the single baseline debounce/gate/clear/
  // URL-sync implementation feeding the plain-mode BrowseSearchControl and
  // the AQL-mode AqlBrowseSearchInput adapter.
  const plainCtrl = useBrowseSearchController({
    urlQ: urlPlainQuery,
    setQ: setPlainQ,
    initialValue: urlPlainQuery || readLastQuery(LAST_PLAIN_QUERY_KEY),
  })
  const aqlCtrl = useBrowseSearchController({
    urlQ: urlAqlQuery,
    setQ: setAqlQ,
    initialValue: urlAqlQuery || readLastQuery(LAST_AQL_QUERY_KEY),
  })

  // Debounced/aborted/LRU-cached suggestion fetch (Plan 02). Called at top
  // level (Rules of Hooks); only meaningful in AQL mode — in plain mode the
  // hook's empty-pre-cursor early return keeps it inert, and the dropdown
  // render below is gated on `mode === 'aql'` regardless.
  const rawSuggestions = useAqlSuggest(aqlQuery, cursorPos)
  // D-13: Escape sets suggestDismissed so the dropdown hides without clearing
  // the query; the next caret move/keystroke (updateCursor) re-opens it.
  // Memoized so the [] fallback keeps a stable identity (the effect below
  // depends on `items` and must not fire on every render).
  const items = useMemo(() => (suggestDismissed ? [] : rawSuggestions), [suggestDismissed, rawSuggestions])

  // D-07: first row auto-highlighted on open/refresh — reset activeIndex
  // whenever the suggestion list identity changes.
  useEffect(() => {
    setActiveIndex(0)
  }, [items])

  useEffect(() => {
    updateSearchFilters({ page, page_size: pageSize })
  }, [page, pageSize, updateSearchFilters])

  const queryRef = useRef(aqlQuery)
  queryRef.current = aqlQuery

  // aql.setQuery/aql.execute come from a custom hook whose returned object is
  // recreated every render — keep them behind refs so the callbacks/effects
  // below can stay stable (same latest-ref pattern as queryRef).
  const setAqlQueryRef = useRef(aql.setQuery)
  setAqlQueryRef.current = aql.setQuery
  const executeAqlRef = useRef(aql.execute)
  executeAqlRef.current = aql.execute

  const [executeTrigger, setExecuteTrigger] = useState(0)

  const handleExecuteAql = useCallback(() => {
    if (queryRef.current.trim().length === 0) return
    setAqlQueryRef.current(queryRef.current)
    setExecuteTrigger(n => n + 1)
  }, [])

  useEffect(() => {
    if (executeTrigger > 0) {
      executeAqlRef.current()
    }
  }, [executeTrigger])

  // Run the restored AQL query (from the URL or, failing that, the last one
  // remembered in localStorage) once on mount — Free Text search already
  // auto-runs on mount via useOpenSearchSearch's debounce effect, so AQL
  // gets the same "don't have to re-enter it" treatment.
  useEffect(() => {
    if (queryRef.current.trim().length > 0) {
      handleExecuteAql()
    }
  }, [handleExecuteAql])

  const aqlObjectTypeMap = useMemo(() => {
    const map = new Map()
    allObjectTypes.forEach(ot => map.set(ot.id, ot.name))
    return map
  }, [allObjectTypes])

  // AQL explicit execution: Enter commits the LIVE textarea draft (the shared
  // controller only debounces typing — an explicit execute must never run a
  // stale committed query), writes aql via setAqlQ, then executes.
  const handleAqlEnter = useCallback((draftValue) => {
    if (!draftValue || draftValue.trim().length === 0) return
    setAqlQ(draftValue, { replace: true })
    setAqlQueryRef.current(draftValue)
    setExecuteTrigger(n => n + 1)
  }, [setAqlQ])

  // Single caret-tracking handler (Pitfall 1): reads the real selectionStart
  // into state (so the suggest hook's effect deps stay fresh) and clears the
  // Escape dismissal (D-13 — next caret move/keystroke re-opens the dropdown).
  // The dismissal is cleared ONLY when the caret position actually CHANGED:
  // incidental select events (e.g. the delayed caret-reset select a
  // controlled-textarea re-render fires after a programmatic value change)
  // must not undo an Escape dismissal mid-race (D-13 stickiness).
  const prevCursorPosRef = useRef(0)
  const updateCursor = (e) => {
    const pos = e.target.selectionStart ?? 0
    const moved = pos !== prevCursorPosRef.current
    prevCursorPosRef.current = pos
    setCursorPos(pos)
    if (moved) setSuggestDismissed(false)
  }

  // Defensive cursor-only fallback for caret-only key moves (Pitfall 1).
  // Deliberately does NOT clear suggestDismissed — the keyup of the Escape key
  // itself would otherwise instantly re-open the dropdown the keydown just
  // dismissed (D-13).
  const handleAqlKeyUp = useCallback((e) => {
    setCursorPos(e.target.selectionStart ?? 0)
  }, [])

  // Accept a suggestion: splice it into the LIVE draft at the token boundary
  // (AC-09), land it synchronously in the shared controller + URL + localStorage
  // (D-12/T-195-06), then reposition the caret and re-open the dropdown (D-12).
  // Declared BEFORE handleAqlKeyDown so its dependency array can reference it
  // without a temporal-dead-zone error (const ordering).
  const acceptSuggestion = useCallback((item) => {
    const { value, cursor } = applySuggestion(aqlCtrl.searchInput, cursorPos, item)
    aqlCtrl.setValue(value)
    setAqlQ(value, { replace: true })
    writeLastQuery(LAST_AQL_QUERY_KEY, value)
    requestAnimationFrame(() => {
      const ta = textareaRef.current
      if (ta) {
        ta.focus()
        ta.setSelectionRange(cursor, cursor)
      }
      setCursorPos(cursor)
      setSuggestDismissed(false)
    })
  }, [aqlCtrl, cursorPos, setAqlQ])

  // AQL textarea keydown (unchanged AQL autocomplete contract): Escape always
  // dismisses; with the dropdown open Arrow/Enter/Tab navigate/accept the
  // highlighted row (synchronous Enter-gating off committed state, Pitfall 3);
  // with it closed Enter executes the live draft. Shift+Enter inserts a
  // newline (default textarea behavior).
  const handleAqlKeyDown = useCallback((e) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      setSuggestDismissed(true)
      return
    }
    const open = items.length > 0
    if (open) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIndex((activeIndex + 1) % items.length) // wrap, D-11
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIndex((activeIndex - 1 + items.length) % items.length) // wrap, D-11
        return
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        acceptSuggestion(items[activeIndex]) // NEVER execute (AC-08)
        return
      }
      if (e.key === 'Tab') {
        e.preventDefault()
        acceptSuggestion(items[activeIndex]) // AC-07
        return
      }
    }
    // Enter submits; Shift+Enter inserts a newline (default textarea behavior,
    // so just let it through instead of preventDefault).
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleAqlEnter(e.target.value)
    }
  }, [items, activeIndex, acceptSuggestion, handleAqlEnter])

  const writeFilterParams = useCallback((key, value) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    })
  }, [setSearchParams])

  const handleFilterChange = (key, value) => {
    setFilters((f) => ({ ...f, [key]: value || undefined }))
    updateSearchFilters({ [key]: value || undefined })
    writeFilterParams(key, value || '')
  }

  const handleSourceChange = (value) => {
    setSelectedSourceId(value)
    setSelectedSchemaId('')
    handleFilterChange('source_id', value)
    handleFilterChange('schema_id', undefined)
    handleFilterChange('object_type_id', undefined)
  }

  const handleSchemaChange = (value) => {
    setSelectedSchemaId(value)
    handleFilterChange('schema_id', value)
    handleFilterChange('object_type_id', undefined)
  }

  const handleObjectTypeChange = (value) => {
    handleFilterChange('object_type_id', value)
  }

  const hasActiveFilters = Boolean(filters.source_id || filters.schema_id || filters.object_type_id)

  const handleResetFilters = () => {
    setSelectedSourceId('')
    setSelectedSchemaId('')
    setFilters({})
    updateSearchFilters({ source_id: undefined, schema_id: undefined, object_type_id: undefined })
    setPage(1)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('source_id')
      next.delete('schema_id')
      next.delete('object_type_id')
      return next
    })
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      <PageHeader title={t('search.title', 'Search')} description={t('search.description', 'Search across all objects')} />

      <div data-testid="aql-mode-toggle">
        <SegmentedControl
          options={[
            { value: 'plain', label: t('search.mode.free_text', 'Free Text Search') },
            { value: 'aql', label: t('search.mode.aql', 'AQL Search') },
          ]}
          active={mode}
          onChange={handleModeChange}
          name="search-mode"
        />
      </div>

      <div className="space-y-4">
        <TableToolbar
          urlQ={mode === 'aql' ? urlAqlQuery : urlPlainQuery}
          setQ={mode === 'aql' ? setAqlQ : setPlainQ}
          placeholder={t('search.query.placeholder', 'Search objects...')}
          ariaLabel={t('search.query.accessible_label', 'Search objects')}
          searchControl={mode === 'aql' ? (
            <AqlBrowseSearchInput
              controller={aqlCtrl}
              textareaRef={textareaRef}
              onCursor={updateCursor}
              onKeyUp={handleAqlKeyUp}
              onKeyDown={handleAqlKeyDown}
              onBlur={() => setSuggestDismissed(true)}
              placeholder={t('search.query.placeholder', 'Search objects...')}
              dropdown={items.length > 0 ? (
                <AqlSuggestDropdown
                  textareaRef={textareaRef}
                  cursorPos={cursorPos}
                  items={items}
                  activeIndex={activeIndex}
                  onAccept={acceptSuggestion}
                  onHover={setActiveIndex}
                  onOpenHelp={() => {
                    setShowSyntaxHelp(true)
                    setSuggestDismissed(true) // dismiss dropdown, same as an accept would (D-07)
                  }}
                />
              ) : null}
            />
          ) : (
            <BrowseSearchControl
              controller={plainCtrl}
              placeholder={t('search.query.placeholder', 'Search objects...')}
              ariaLabel={t('search.query.accessible_label', 'Search objects')}
              testId="search-input"
              className="flex-1 min-w-[200px]"
              autoFocus
            />
          )}
          actions={mode === 'aql' ? (
            <button
              type="button"
              onClick={() => setShowSyntaxHelp(true)}
              className="text-primary hover:underline inline-flex items-center gap-1"
              data-testid="aql-syntax-help-link"
            >
              <HelpCircle size={14} />
              {t('search.aql.syntax_help', 'AQL syntax help')}
            </button>
          ) : undefined}
        />

        {mode !== 'aql' && (
          <div className="flex flex-wrap gap-3" data-testid="search-filters">
            <select
              value={filters.source_id || ''}
              onChange={(e) => handleSourceChange(e.target.value)}
              className="rounded-md border px-3 py-1.5 text-sm bg-background disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <option value="">{t('search.filters.all_sources', 'All Sources')}</option>
              {sources.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <select
              value={filters.schema_id || ''}
              onChange={(e) => handleSchemaChange(e.target.value)}
              className="rounded-md border px-3 py-1.5 text-sm bg-background disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={!selectedSourceId}
            >
              <option value="">{t('search.filters.all_schemas', 'All Schemas')}</option>
              {schemas.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <select
              value={filters.object_type_id || ''}
              onChange={(e) => handleObjectTypeChange(e.target.value)}
              className="rounded-md border px-3 py-1.5 text-sm bg-background disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={!selectedSchemaId}
            >
              <option value="">{t('search.filters.all_object_types', 'All Object Types')}</option>
              {flattenHierarchy(objectTypes).map(({ ot, level }) => (
                <option key={ot.id} value={ot.id}>
                  {'    '.repeat(level)}{level > 0 ? '↳ ' : ''}{ot.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={handleResetFilters}
              disabled={!hasActiveFilters}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="search-filters-reset"
            >
              <LucideIcon name="x" size={14} />
              {t('search.filters.reset', 'Reset filters')}
            </button>
          </div>
        )}
      </div>

      {!currentQuery && (
        <div data-testid="search-empty-query">
          <EmptyState
            variant="empty"
            icon={Search}
            title={t('search.empty.title', 'Search objects')}
            description={t('search.empty.description', 'Enter a search query to find objects across all sources and schemas.')}
          />
        </div>
      )}

      {currentQuery && mode === 'plain' && esLoading && (
        <TableSkeleton rows={5} cols={2} />
      )}

      {currentQuery && mode === 'plain' && !esLoading && esResults.length === 0 && (
        <div data-testid="search-no-results">
          <EmptyState
            variant="no-results"
            icon={SearchX}
            title={t('search.no_results.title', 'No results')}
            description={t('search.no_results.description', 'No objects match your search. Try adjusting your query or filters.')}
          />
        </div>
      )}

      {mode === 'aql' && currentQuery && !aql.isLoading && aql.error && (
        <div data-testid="aql-error">
          <EmptyState
            variant="error"
            title={aql.error.message || t('search.aql.parse_error', 'Parse error')}
            description={aql.error.line !== null && aql.error.line !== undefined ? interpolateDefault(t('search.aql.parse_position', 'Line {{line}}, Column {{column}}'), { line: aql.error.line, column: aql.error.column }) : undefined}
          />
        </div>
      )}

      {currentQuery && mode === 'aql' && aql.isLoading && (
        <TableSkeleton rows={5} cols={2} />
      )}

      {currentQuery && mode === 'aql' && !aql.isLoading && !aql.error && aql.results.length === 0 && (
        <div data-testid="aql-no-results">
          <EmptyState
            variant="no-results"
            icon={SearchX}
            title={t('search.no_results.title', 'No results')}
            description={t('search.aql.no_results_description', 'No objects match your AQL query. Try adjusting your query.')}
          />
        </div>
      )}

      {mode === 'plain' && esResults.length > 0 && (
        <div className="space-y-3" data-testid="search-results">
          <p className="text-sm text-muted-foreground">{selectPluralKey('search.results.count', esTotal) === 'search.results.count_one'
            ? interpolateDefault(t('search.results.count_one', '{{count}} result'), { count: esTotal })
            : interpolateDefault(t('search.results.count_other', '{{count}} results'), { count: esTotal })}</p>
          <Table>
            <thead>
              <tr className="border-b">
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.name', 'Name')}</th>
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('search.results.score', 'Score')}</th>
              </tr>
            </thead>
            <tbody>
              {esResults.map((hit) => {
                const highlightEntries = hit.highlights
                  ? Object.entries(hit.highlights).flatMap(([field, snippets]) =>
                      (snippets || []).filter(Boolean).map((text) => ({ field, text }))
                    )
                  : []
                const firstHighlight = highlightEntries[0]

                return (
                  <tr key={hit.id} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-3">
                      <Link to={`/browse/objects/${hit.id}`} className="text-primary hover:underline">
                        {unescapeHtmlEntities(hit.name)}
                      </Link>
                      {firstHighlight && (
                        <div className="mt-0.5 text-sm text-muted-foreground line-clamp-2">
                          <HighlightedText text={firstHighlight.text} escaped={firstHighlight.field === 'name'} />
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground align-top">{hit.score?.toFixed(2)}</td>
                  </tr>
                )
              })}
            </tbody>
          </Table>
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={esTotal}
            onPageChange={setPage}
            onPageSizeChange={setPageSize}
          />
        </div>
      )}

      {mode === 'aql' && aql.results.length > 0 && (
        <div className="space-y-3" data-testid="aql-results">
          <p className="text-sm text-muted-foreground">{selectPluralKey('search.results.count', aql.total) === 'search.results.count_one'
            ? interpolateDefault(t('search.results.count_one', '{{count}} result'), { count: aql.total })
            : interpolateDefault(t('search.results.count_other', '{{count}} results'), { count: aql.total })}</p>
          <Table>
            <thead>
              <tr className="border-b">
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('common.name', 'Name')}</th>
                <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('search.results.object_type', 'ObjectType')}</th>
              </tr>
            </thead>
            <tbody>
              {aql.results.map((hit) => (
                <tr key={hit.id} className="border-b last:border-0 hover:bg-muted/30">
                  <td className="px-4 py-3">
                    <Link to={`/browse/objects/${hit.id}`} className="text-primary hover:underline">
                      {hit.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-foreground">
                    {aqlObjectTypeMap.get(hit.object_type_id) || hit.object_type_id}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
          <PaginationBar
            page={page}
            pageSize={pageSize}
            total={aql.total}
            onPageChange={(p) => {
              aql.setPage(p)
              aql.execute()
            }}
            onPageSizeChange={(s) => {
              aql.setPageSize(s)
              aql.setPage(1)
              aql.execute()
            }}
          />
        </div>
      )}
      </div>
      {showSyntaxHelp && (
        <AqlSyntaxHelpModal onClose={() => setShowSyntaxHelp(false)} />
      )}
    </ErrorBoundary>
  )
}
