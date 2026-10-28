import { useState } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { useCrossEntitySearch } from '../../shared/hooks/use-search'
import HighlightedText from '../../shared/utils/highlight-text'
import { Button } from '../../shared/components/Button'
import { PaginationBar } from '../../shared/components/PaginationBar'
import EmptyState from '../../shared/components/EmptyState'
import Search from 'lucide-react/dist/esm/icons/search'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { PHASE231_SEARCH_ENTITY_TYPES } from '../../shared/lib/phase231-dynamic-contract'

const TABS = ['objects', 'object_types', 'schemas']

function entityLabelKey(tab) {
  if (tab === 'objects') return PHASE231_SEARCH_ENTITY_TYPES.object
  if (tab === 'object_types') return PHASE231_SEARCH_ENTITY_TYPES.object_type
  return PHASE231_SEARCH_ENTITY_TYPES.schema
}

export default function CrossEntitySearchPage() {
  const { t } = useTranslation()
  const getEntityLabel = (tab) => {
    const label = entityLabelKey(tab)
    return t(label.key, label.defaultText)
  }
  const [searchParams, setSearchParams] = useSearchParams()
  const q = searchParams.get('q') || ''
  const [activeTab, setActiveTab] = useState('objects')
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState({ objects: false, object_types: false, schemas: false })

  const pageSize = expanded[activeTab] ? 20 : 5

  const { data, isLoading, error } = useCrossEntitySearch(q, page, pageSize)

  const [inputValue, setInputValue] = useState(q)

  function handleSearch(e) {
    e.preventDefault()
    if (inputValue.trim().length >= 2) {
      setSearchParams({ q: inputValue.trim() })
      setPage(1)
    }
  }

  if (!q) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <form onSubmit={handleSearch} className="mb-6">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              value={inputValue}
              onChange={e => setInputValue(e.target.value)}
              placeholder={t('search.cross_entity.placeholder', 'Search across Objects, ObjectTypes, and Schemas...')}
              className="w-full rounded-lg border border-input bg-background pl-10 pr-4 py-2 text-lg focus:outline-none focus:ring-2 focus:ring-primary"
              autoFocus
            />
          </div>
        </form>
        <EmptyState
          variant="empty"
          icon={Search}
          title={t('search.cross_entity.title', 'Cross-Entity Search')}
          description={t('search.cross_entity.description', 'Search across all Objects, ObjectTypes, and Schemas. Results are grouped by entity type.')}
        />
      </div>
    )
  }

  const currentGroup = data?.[activeTab]
  const totalResults = TABS.reduce((sum, tab) => sum + (data?.[tab]?.count || 0), 0)

  function toggleExpand(tab) {
    setExpanded(prev => ({ ...prev, [tab]: !prev[tab] }))
    setPage(1)
  }

  return (
    <div className="max-w-4xl mx-auto p-6">
      {/* Search input (always visible) */}
      <form onSubmit={handleSearch} className="mb-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            placeholder={t('search.cross_entity.all_placeholder', 'Search across all entities...')}
            className="w-full rounded-lg border border-input bg-background pl-10 pr-4 py-2 text-base focus:outline-none focus:ring-2 focus:ring-primary"
          />
        </div>
      </form>

      {isLoading && (
        <div className="text-center py-12 text-muted-foreground">{t('search.searching', 'Searching...')}</div>
      )}

      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 px-4 py-2 text-sm text-destructive mb-2">
          {t('search.error', 'Search failed. Please try again.')}
        </div>
      )}

      {data && (
        <>
          {/* Summary line */}
          <p className="text-sm text-muted-foreground mb-4">
            {selectPluralKey('search.cross_entity.summary', totalResults) === 'search.cross_entity.summary_one'
              ? interpolateDefault(t('search.cross_entity.summary_one', '{{count}} result for "{{query}}"'), { count: totalResults, query: q })
              : interpolateDefault(t('search.cross_entity.summary_other', '{{count}} results for "{{query}}"'), { count: totalResults, query: q })}
          </p>

          {/* Tabs */}
          <div className="flex gap-0 border-b mb-4">
            {TABS.map(tab => {
              const count = data[tab]?.count || 0
              return (
                <button
                  key={tab}
                  onClick={() => { setActiveTab(tab); setPage(1) }}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    activeTab === tab
                      ? 'border-primary text-primary'
                      : 'border-transparent text-muted-foreground hover:text-foreground'
                  }`}
                >
                   {getEntityLabel(tab)} ({count})
                </button>
              )
            })}
          </div>

          {/* Results for active tab */}
          {currentGroup && currentGroup.results.length > 0 ? (
            <>
              <div className="space-y-3">
                {currentGroup.results.map(result => {
                  const config = PHASE231_SEARCH_ENTITY_TYPES[result.type] || PHASE231_SEARCH_ENTITY_TYPES.object
                  // Extract first highlight snippet
                  const highlightSnippet = result.highlights
                    ? Object.values(result.highlights).flat().find(s => s && s.includes('<mark>'))
                    : null

                  return (
                    <div key={`${result.type}-${result.id}`} className="border rounded-lg p-4 hover:bg-muted/30 transition-colors">
                      <div className="flex items-start gap-2">
                        {/* Type badge */}
                        <span className={`shrink-0 text-xs px-2 py-1 rounded-full font-medium ${config.badgeClass}`}>
                           {t(config.key, config.defaultText)}
                        </span>
                        <div className="flex-1 min-w-0">
                          {/* Clickable name */}
                          <Link
                            to={result.url}
                            className="text-primary hover:underline font-medium"
                          >
                            <HighlightedText text={result.name} />
                          </Link>
                          {/* Description/snippet with highlight */}
                          {highlightSnippet && (
                            <div className="mt-1 text-sm text-muted-foreground line-clamp-2">
                              <HighlightedText text={highlightSnippet} />
                            </div>
                          )}
                          {!highlightSnippet && result.description && (
                            <div className="mt-1 text-sm text-muted-foreground line-clamp-2">
                              <HighlightedText text={result.description} />
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* Pagination for expanded view */}
              {expanded[activeTab] && currentGroup.count > pageSize && (
                <div className="mt-4">
                  <PaginationBar
                    page={page}
                    pageSize={pageSize}
                    total={currentGroup.count}
                    onPageChange={setPage}
                    onPageSizeChange={() => {}}
                    hideRange={false}
                  />
                </div>
              )}

              {/* "Show all (N)" link */}
              {!expanded[activeTab] && currentGroup.count > 5 && (
                <div className="mt-4 text-center">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => toggleExpand(activeTab)}
                  >
                     {selectPluralKey('search.cross_entity.show_all', currentGroup.count) === 'search.cross_entity.show_all_one'
                       ? interpolateDefault(t('search.cross_entity.show_all_one', 'Show all ({{count}})'), { count: currentGroup.count })
                       : interpolateDefault(t('search.cross_entity.show_all_other', 'Show all ({{count}})'), { count: currentGroup.count })}
                  </Button>
                </div>
              )}
            </>
          ) : (
            <EmptyState
              variant="no-results"
               title={interpolateDefault(t('search.cross_entity.no_results', 'No {{entity}} found.'), { entity: getEntityLabel(activeTab) })}
               description={t('search.cross_entity.no_results_description', 'Try a different search term or check another tab.')}
            />
          )}
        </>
      )}
    </div>
  )
}
