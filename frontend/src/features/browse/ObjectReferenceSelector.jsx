import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { Button } from '../../shared/components/Button'
import useDebouncedSearch from '../../shared/hooks/use-debounced-search'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export default function ObjectReferenceSelector({ referencedObjectTypeId, value, onChange, structure, isMulti = false }) {
  const { t } = useTranslation()
  const [mode, setMode] = useState('search')
  const [browseSearchQuery, setBrowseSearchQuery] = useState('')

  // D-04: structure.filter.objecttypeid takes priority over referencedObjectTypeId
  // D-05: Fall back to referencedObjectTypeId when structure.filter is absent or missing objecttypeid
  const rawFilterObjectTypeId = structure?.filter?.objecttypeid
  const effectiveObjectTypeId = (UUID_RE.test(rawFilterObjectTypeId) ? rawFilterObjectTypeId : null) || referencedObjectTypeId || null

  const selectedIds = isMulti ? (Array.isArray(value) ? value : []) : null

  // Single-value: fetch selected object name
  const { data: selectedObject } = useQuery({
    queryKey: ['object-ref-name', value],
    queryFn: () => api.get(`/objects/${value}`).then((r) => r.data),
    enabled: !isMulti && !!value,
  })

  // Multi-value: fetch names for all selected IDs
  const { data: selectedObjects = [] } = useQuery({
    queryKey: ['object-ref-names-multi', (selectedIds || []).join(',')],
    queryFn: async () => {
      if (!selectedIds?.length) return []
      const results = await Promise.all(
        selectedIds.map(id => api.get(`/objects/${id}`).then(r => r.data).catch(() => ({ id, name: id.slice(0, 8) + '...' })))
      )
      return results
    },
    enabled: isMulti && !!selectedIds?.length,
  })

  const { query, setQuery, results: objects, isLoading } = useDebouncedSearch({
    fetcher: (q, signal) => api.get('/objects/', {
      params: {
        skip: 0,
        limit: 10,
        sort_by: 'name',
        sort_order: 'asc',
        ...(effectiveObjectTypeId ? { object_type_id: effectiveObjectTypeId } : {}),
        ...(q ? { q } : {}),
      },
      signal,
    }).then(r => r.data),
    debounceMs: 300,
    minChars: isMulti ? 0 : 2,
    enabled: mode === 'search',
  })

  const addRef = (id) => {
    if (isMulti) {
      if (!selectedIds.includes(id)) onChange([...selectedIds, id])
    } else {
      onChange(id)
    }
    setQuery('')
  }

  const removeRef = (id) => {
    onChange(selectedIds.filter(sid => sid !== id))
  }

  if (mode === 'browse') {
    return (
      <div className="space-y-2">
        <input
          type="text"
          value={browseSearchQuery}
          onChange={e => setBrowseSearchQuery(e.target.value)}
          placeholder={t('browse.references.search_objects', 'Search objects...')}
          className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary mb-2"
        />
        <ObjectTreeBrowser
          effectiveObjectTypeId={effectiveObjectTypeId}
          onSelect={(id) => { addRef(id); setBrowseSearchQuery(''); if (!isMulti) setMode('search') }}
          searchQuery={browseSearchQuery}
        />
        <Button type="button" variant="ghost" size="sm" onClick={() => setMode('search')}>
          {t('browse.references.back_to_search', '← Back to search')}
        </Button>
      </div>
    )
  }

  if (isMulti) {
    const availableObjects = objects.filter(o => !selectedIds.includes(o.id))
    return (
      <div className="space-y-2">
        {selectedIds.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {selectedObjects.map(obj => (
              <span key={obj.id} className="inline-flex items-center gap-1 bg-muted rounded px-2 py-0.5 text-xs">
                {obj.name}
                <Button type="button" size="icon" variant="ghost" onClick={() => removeRef(obj.id)} className="h-5 w-5 ml-0.5 hover:text-destructive" leftIcon="x" aria-label={t('browse.references.remove', 'Remove')} />
              </span>
            ))}
          </div>
        )}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('browse.references.search_to_add', 'Search to add...')}
          className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <div className="rounded-md border max-h-40 overflow-y-auto">
          {isLoading ? (
              <p className="px-3 py-2 text-sm text-muted-foreground">{t('browse.references.loading', 'Loading...')}</p>
          ) : availableObjects.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">{query ? t('browse.references.no_results', 'No results') : t('browse.references.no_more_objects', 'No more objects to add')}</p>
          ) : (
            availableObjects.slice(0, 10).map((obj) => (
              <Button type="button" variant="ghost" key={obj.id} onClick={() => addRef(obj.id)} className="w-full justify-start font-normal h-auto px-3 py-2 text-sm">
                {obj.name}
              </Button>
            ))
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {value ? (
        <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2">
           <span className="text-sm flex-1 truncate text-primary">{interpolateDefault(t('browse.references.selected', 'Selected: {{value}}'), { value: selectedObject?.name || value.slice(0, 8) + '...' })}</span>
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange(null)} className="hover:text-destructive">
            {t('browse.references.clear', 'Clear')}
          </Button>
        </div>
      ) : (
        <>
          <div className="flex gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('browse.references.search_objects', 'Search objects...')}
              className="flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setMode('browse')}
            >
              {t('browse.references.browse', 'Browse')}
            </Button>
          </div>
          {query && (
            <div className="rounded-md border max-h-40 overflow-y-auto">
              {isLoading ? (
                  <p className="px-3 py-2 text-sm text-muted-foreground">{t('browse.references.searching', 'Searching...')}</p>
              ) : objects.length === 0 ? (
                  <p className="px-3 py-2 text-sm text-muted-foreground">{t('browse.references.no_results', 'No results')}</p>
              ) : (
                objects.slice(0, 10).map((obj) => (
                  <Button
                    type="button"
                    variant="ghost"
                    key={obj.id}
                    onClick={() => onChange(obj.id)}
                    className="w-full justify-start font-normal h-auto px-3 py-2 text-sm"
                  >
                    {obj.name}
                  </Button>
                ))
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function ObjectTreeBrowser({ effectiveObjectTypeId, onSelect, searchQuery = '' }) {
  const { t } = useTranslation()
  const [expandedSource, setExpandedSource] = useState(null)
  const [expandedSchema, setExpandedSchema] = useState(null)
  const [selectedOtId, setSelectedOtId] = useState(null)

  const isSearchingBrowse = searchQuery.length > 0

  const { data: allBrowseObjects = [], isLoading: isBrowseSearchLoading } = useQuery({
    queryKey: ['objects-browse-search', searchQuery],
    queryFn: () => api.get('/objects/', { params: { skip: 0, limit: 200, sort_by: 'name', sort_order: 'asc' } }).then(r => r.data),
    enabled: isSearchingBrowse && !effectiveObjectTypeId,
  })

  // When the attribute already constrains the type (filter or referenced_object_type_id),
  // skip the Source/Schema/ObjectType drill-down entirely and list matching objects directly —
  // picking an ObjectType again would be redundant since it's already fixed.
  const { data: filteredObjects = [], isLoading: isFilteredLoading } = useQuery({
    queryKey: ['objects-tree-filtered', effectiveObjectTypeId],
    queryFn: () => api.get('/objects/', { params: { skip: 0, limit: 50, object_type_id: effectiveObjectTypeId } }).then((r) => r.data),
    enabled: !!effectiveObjectTypeId,
  })

  const { data: sources = [] } = useQuery({
    queryKey: ['sources-tree'],
    queryFn: () => api.get('/sources/', { params: { skip: 0, limit: 100 } }).then((r) => r.data),
    enabled: !effectiveObjectTypeId,
  })

  const { data: schemas = [] } = useQuery({
    queryKey: ['schemas-tree', expandedSource],
    queryFn: () => api.get('/schemas/', { params: { source_id: expandedSource, skip: 0, limit: 100 } }).then((r) => r.data),
    enabled: !effectiveObjectTypeId && !!expandedSource,
  })

  const { data: objectTypes = [] } = useQuery({
    queryKey: ['object-types-tree', expandedSchema],
    queryFn: () => api.get('/object-types/', { params: { schema_id: expandedSchema, skip: 0, limit: 100 } }).then((r) => r.data),
    enabled: !effectiveObjectTypeId && !!expandedSchema,
  })

  const { data: objects = [] } = useQuery({
    queryKey: ['objects-tree', selectedOtId],
    queryFn: () => api.get('/objects/', { params: { skip: 0, limit: 50, object_type_id: selectedOtId } }).then((r) => r.data),
    enabled: !effectiveObjectTypeId && !!selectedOtId,
  })

  const browseFiltered = isSearchingBrowse
    ? (effectiveObjectTypeId
        ? filteredObjects.filter(o => o.name.toLowerCase().includes(searchQuery.toLowerCase()))
        : allBrowseObjects.filter(o => o.name.toLowerCase().includes(searchQuery.toLowerCase())))
    : null

  if (isSearchingBrowse) {
    return (
      <div className="rounded-md border text-sm max-h-48 overflow-y-auto p-2 space-y-1">
        {(effectiveObjectTypeId ? isFilteredLoading : isBrowseSearchLoading) ? (
          <p className="px-2 py-1 text-sm text-muted-foreground">{t('browse.references.searching', 'Searching...')}</p>
        ) : browseFiltered.length === 0 ? (
          <p className="px-2 py-1 text-sm text-muted-foreground">{interpolateDefault(t('browse.references.no_objects_match', 'No objects match "{{query}}"'), { query: searchQuery })}</p>
        ) : (
          browseFiltered.slice(0, 50).map((obj) => (
            <Button
              type="button"
              variant="ghost"
              key={obj.id}
              onClick={() => onSelect(obj.id)}
              className="w-full justify-start font-normal h-auto px-2 py-1 text-sm"
            >
              {obj.name}
            </Button>
          ))
        )}
      </div>
    )
  }

  if (effectiveObjectTypeId) {
    return (
      <div className="rounded-md border text-sm max-h-48 overflow-y-auto p-2 space-y-1">
        {isFilteredLoading ? (
          <p className="px-2 py-1 text-sm text-muted-foreground">{t('browse.references.loading', 'Loading...')}</p>
        ) : filteredObjects.length === 0 ? (
          <p className="px-2 py-1 text-sm text-muted-foreground">{t('browse.references.no_objects_found', 'No objects found')}</p>
        ) : (
          filteredObjects.map((obj) => (
            <Button
              type="button"
              variant="ghost"
              key={obj.id}
              onClick={() => onSelect(obj.id)}
              className="w-full justify-start font-normal h-auto px-2 py-1 text-sm"
            >
              {obj.name}
            </Button>
          ))
        )}
      </div>
    )
  }

  return (
    <div className="rounded-md border text-sm max-h-48 overflow-y-auto p-2 space-y-1">
      {sources.map((src) => (
        <div key={src.id}>
          <Button
            type="button"
            variant="ghost"
            onClick={() => setExpandedSource(expandedSource === src.id ? null : src.id)}
            className="w-full justify-start font-normal h-auto px-2 py-1 text-sm"
          >
            <span className="text-xs text-muted-foreground">{expandedSource === src.id ? '▼' : '▶'}</span>
            {src.name}
          </Button>
          {expandedSource === src.id && schemas.map((sch) => (
            <div key={sch.id} className="ml-3">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setExpandedSchema(expandedSchema === sch.id ? null : sch.id)}
                className="w-full justify-start font-normal h-auto px-2 py-1 text-sm"
              >
                <span className="text-xs text-muted-foreground">{expandedSchema === sch.id ? '▼' : '▶'}</span>
                {sch.name}
              </Button>
              {expandedSchema === sch.id && objectTypes.map((ot) => (
                <div key={ot.id} className="ml-3">
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => setSelectedOtId(ot.id)}
                    className={`w-full justify-start font-normal h-auto px-2 py-1 text-sm ${
                      selectedOtId === ot.id ? 'bg-accent' : ''
                    }`}
                  >
                    {ot.name}
                  </Button>
                  {selectedOtId === ot.id && objects.map((obj) => (
                    <Button
                      type="button"
                      variant="ghost"
                      key={obj.id}
                      onClick={() => onSelect(obj.id)}
                      className="ml-3 w-full justify-start font-normal h-auto px-2 py-1 text-sm text-muted-foreground"
                    >
                      {obj.name}
                    </Button>
                  ))}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
