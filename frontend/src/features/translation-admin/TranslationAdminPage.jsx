import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useCan } from '../../shared/components/Can'
import { useLanguages } from '../../shared/hooks/use-languages'
import useDebouncedValue from '../../shared/hooks/use-debounced-value'
import { cn } from '../../shared/lib/utils'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { usePluginVisibleContributions } from '../../shared/plugin-runtime/PluginProvider'
import { PluginContributionBoundary } from '../../shared/plugin-runtime/PluginSlotBoundary'
import { createTranslationReviewSession } from '../../shared/plugin-runtime/translation-review-session'
import { useTranslationCatalog } from './use-translation-catalog'
import { useTranslationExport } from './use-translation-export'
import TranslationCatalogTable from './TranslationCatalogTable'

// D-02 sentinel value for the server-provided nullable namespace option;
// distinct from '' (All) so the select can distinguish the two states.
const NULL_NAMESPACE_VALUE = '__uncategorized__'

// D-03/T-307-16: the admin page keeps the Core coverage/filter/export/manual
// pagination and consumes a neutral review projection published by the
// ai-translation plugin. It knows no suggestion lifecycle, no bulk controller
// and no apply API: the plugin publishes rows/drafts for the current scope and
// the page narrows the SAME table to them.
export default function TranslationAdminPage() {
  const { t } = useTranslation()
  const hasUpdate = useCan('translations', 'update')
  const hasDelete = useCan('translations', 'delete')

  // D-06: English is the initial selected editor language on every fresh
  // mount, independent of the user's runtime/profile UI language, and never
  // persisted as a preference.
  const [editorLanguage, setEditorLanguage] = useState('en')
  const [namespaceSelection, setNamespaceSelection] = useState('')
  const [rawSearch, setRawSearch] = useState('')
  const debouncedSearch = useDebouncedValue(rawSearch, 300)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [onlyMissingFilter, setOnlyMissingFilter] = useState(false)

  const [adminInspect, setAdminInspect] = useState(null)
  const [presenceRevision, setPresenceRevision] = useState(0)
  const scopeCounterRef = useRef(0)

  // Derived before the plugin hook: the presence context must carry the current
  // namespace form, and a `const` used above its declaration would throw.
  const namespaceIsNull = namespaceSelection === NULL_NAMESPACE_VALUE
  const namespace = namespaceIsNull || namespaceSelection === '' ? undefined : namespaceSelection

  // D-16: the admin action family may declare an optional async presence
  // predicate; `presenceRevision` lets an owner request a re-evaluation of its
  // own predicate without any host polling.
  const adminActions = usePluginVisibleContributions('translationAdminActions', {
    language: editorLanguage,
    namespace,
    namespaceIsNull,
    search: debouncedSearch,
    onlyMissing: onlyMissingFilter,
    scopeRevision: scopeCounterRef.current,
    presenceRevision,
  })
  const handlePresenceChanged = useCallback(() => setPresenceRevision((value) => value + 1), [])

  // D-06/T-307-16: a changed filter/language advances the review scope revision
  // and starts a fresh review session, so a late projection from a previous
  // scope is fenced out and a new scope always returns to browse.
  const [reviewSession, setReviewSession] = useState(() =>
    createTranslationReviewSession({ scopeRevision: 0 })
  )
  const [, setReviewVersion] = useState(0)

  useEffect(() => {
    setAdminInspect(null)
    scopeCounterRef.current += 1
    const next = createTranslationReviewSession({ scopeRevision: scopeCounterRef.current })
    const unsubscribe = next.subscribe(() => setReviewVersion((value) => value + 1))
    setReviewSession(next)
    return unsubscribe
  }, [editorLanguage, namespaceSelection, debouncedSearch, onlyMissingFilter])

  // D-02/D-04: changing any filter, the editor language, or the page size
  // returns to page 1.
  useEffect(() => {
    setPage(1)
  }, [editorLanguage, namespaceSelection, debouncedSearch, pageSize, onlyMissingFilter])

  const { data: languagesData } = useLanguages(0, 200)
  const languages = useMemo(() => (Array.isArray(languagesData) ? languagesData : []), [languagesData])
  const activeLanguages = useMemo(() => languages.filter((language) => language.is_active), [languages])
  const orderedLanguages = useMemo(() => {
    const english = activeLanguages.find((language) => language.code === 'en')
    const rest = activeLanguages.filter((language) => language.code !== 'en')
    return english ? [english, ...rest] : activeLanguages
  }, [activeLanguages])

  const editorLanguageName = orderedLanguages.find((language) => language.code === editorLanguage)?.name || editorLanguage

  const skip = (page - 1) * pageSize

  const { catalog, isLoading, isError, refetch, commitField, getFieldStatus } = useTranslationCatalog({
    editorLanguage,
    namespace,
    namespaceIsNull,
    q: debouncedSearch,
    skip,
    limit: pageSize,
    onlyMissing: onlyMissingFilter,
  })

  const review = reviewSession.getReview()
  const inReview = !!review
  const rows = catalog?.items || []
  const total = catalog?.total || 0
  const hasFilters = !!debouncedSearch || namespaceSelection !== '' || onlyMissingFilter
  const showActionsColumn = hasUpdate || hasDelete
  const skeletonCols = showActionsColumn ? 5 : 4

  const namedNamespaces = (catalog?.namespaces || []).filter((entry) => entry.value !== null)
  const hasUncategorized = (catalog?.namespaces || []).some((entry) => entry.value === null)

  const clearFilters = () => {
    setRawSearch('')
    setNamespaceSelection('')
    setOnlyMissingFilter(false)
    setPage(1)
  }

  // The reference admin action is a non-writing scope inspection: it reports the
  // normalized current scope in this row's context and never starts a bulk job.
  const handleAdminInspect = (entry) => {
    setAdminInspect((prev) =>
      prev?.owner === entry.owner && prev?.id === entry.id
        ? null
        : {
            owner: entry.owner,
            id: entry.id,
            label: entry.displayLabel,
            language: editorLanguage,
            namespaceLabel: namespaceIsNull
              ? t('translation.namespace_uncategorized', 'Uncategorized')
              : (namespace ?? t('translation_admin.namespace_all', 'All namespaces')),
            search: debouncedSearch,
            onlyMissing: onlyMissingFilter,
          }
    )
  }

  // Phase 235 / D-09/D-11/D-16: server-authoritative availability + fail-closed
  // permission is the complete visibility predicate.
  const translationExport = useTranslationExport()
  const exportAvailable = hasUpdate
    && catalog?.export_available === true
    && translationExport.result?.type !== 'unavailable'

  const buildAdminActionProps = (entry) => ({
    filters: {
      language: editorLanguage,
      namespace,
      namespaceIsNull,
      search: debouncedSearch,
      onlyMissing: onlyMissingFilter,
    },
    scopeRevision: reviewSession.scopeRevision,
    canWrite: hasUpdate,
    publishReview: (payload = {}) =>
      reviewSession.publishReview({
        owner: entry.owner,
        scopeRevision: payload.scopeRevision ?? reviewSession.scopeRevision,
        rows: payload.rows,
        drafts: payload.drafts,
        total: payload.total,
        onDraftChange: payload.onDraftChange,
        onDiscard: payload.onDiscard,
      }),
    leaveReview: () => reviewSession.leaveReview(),
    onReviewChange: (uiStringId, value) => reviewSession.setDraftValue(uiStringId, value),
    onReviewDiscard: (uiStringId) => reviewSession.removeRow(uiStringId),
    refreshCatalog: () => refetch(),
    onInspect: () => handleAdminInspect(entry),
    onPresenceChanged: handlePresenceChanged,
  })

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="translation-admin-page">
        <PageHeader
          title={t('translation_admin.title', 'Translation')}
          description={t('translation_admin.description', 'Manage interface text and translations.')}
          icon="languages"
        >
          {exportAvailable && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              leftIcon="save"
              loading={translationExport.isExporting}
              disabled={translationExport.isExporting}
              onClick={translationExport.runExport}
              data-testid="translation-export-action"
            >
              {t('translation_export.action.save', 'Save all translations to filesystem')}
            </Button>
          )}
        </PageHeader>

        {translationExport.result?.type === 'success' && (
          <div
            role="status"
            aria-live="polite"
            data-testid="translation-export-success"
            className="rounded-md border border-primary/30 bg-primary/5 p-4 space-y-2"
          >
            <div className="flex items-center gap-2 text-sm font-medium text-primary">
              <LucideIcon name="check-circle" size={18} className="shrink-0" />
              <span>{t('translation_export.success.heading', 'Translations saved to filesystem')}</span>
            </div>
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1">
                {t('translation_export.success.files_written_label', 'Files written')}
              </div>
              <ul className="space-y-1" data-testid="translation-export-file-list">
                {translationExport.result.paths.map((path) => (
                  <li key={path} className="text-xs font-mono text-foreground" style={{ overflowWrap: 'anywhere' }}>
                    {path}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {translationExport.result?.type === 'error' && (
          <div
            role="alert"
            data-testid="translation-export-error"
            className="rounded-md border border-destructive/30 bg-destructive/10 p-4 space-y-1"
          >
            <div className="flex items-center gap-2 text-sm font-medium text-destructive">
              <LucideIcon name="alert-circle" size={18} className="shrink-0" />
              <span>{t('translation_export.error.heading', "Couldn’t save translations to filesystem")}</span>
            </div>
            <p className="text-sm text-destructive">
              {t('translation_export.error.body', 'No files were changed. Try again.')}
            </p>
          </div>
        )}

        {translationExport.result?.type === 'conflict' && (
          <div
            role="status"
            aria-live="polite"
            data-testid="translation-export-conflict"
            className="rounded-md border border-border bg-muted p-4"
          >
            <div className="flex items-center gap-2 text-sm text-foreground">
              <LucideIcon name="clock" size={18} className="text-muted-foreground shrink-0" />
              <span>{t('translation_export.conflict.message', 'A translation export is already in progress. Try again in a moment.')}</span>
            </div>
          </div>
        )}

        {translationExport.result?.type === 'unavailable' && (
          <div
            role="status"
            aria-live="polite"
            data-testid="translation-export-unavailable"
            className="rounded-md border border-border bg-muted p-4"
          >
            <div className="flex items-center gap-2 text-sm text-foreground">
              <LucideIcon name="info" size={18} className="text-muted-foreground shrink-0" />
              <span>{t('translation_export.stale.message', 'Filesystem export is no longer available on this server.')}</span>
            </div>
          </div>
        )}

        <section className="space-y-3">
          <h2 className="text-lg font-medium">{t('translation_admin.coverage.heading', 'Translation coverage')}</h2>
          <div className="flex flex-wrap gap-3">
            {orderedLanguages.map((language) => {
              const coverage = catalog?.coverage?.find((entry) => entry.code === language.code)
              const translated = coverage?.translated ?? 0
              const coverageTotal = coverage?.total ?? 0
              const isSelected = language.code === editorLanguage
              return (
                <div
                  key={language.code}
                  data-testid={`coverage-card-${language.code}`}
                  className={cn(
                    'rounded-lg border p-4 min-w-[160px]',
                    isSelected ? 'border-primary' : 'border-border'
                  )}
                >
                  <div className="text-sm font-medium">{language.name} ({language.code})</div>
                  <div className="text-xs text-muted-foreground">
                    {interpolateDefault(
                      t('translation_admin.coverage.count', '{{translated}} / {{total}} translated'),
                      { translated, total: coverageTotal }
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        <div className="flex flex-wrap gap-3" data-testid="translation-filter-bar">
          <div className="flex-1 min-w-[200px]">
            <label htmlFor="translation-search-input" className="text-xs font-medium block mb-1">
              {t('translation_admin.filters.search_label', 'Search translations')}
            </label>
            <input
              id="translation-search-input"
              type="text"
              value={rawSearch}
              onChange={(e) => setRawSearch(e.target.value)}
              placeholder={t('translation_admin.filters.search_placeholder', 'Search keys and values')}
              className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
              data-testid="translation-search-input"
            />
          </div>
          <div className="min-w-[180px]">
            <label htmlFor="translation-namespace-select" className="text-xs font-medium block mb-1">
              {t('translation_admin.filters.namespace_label', 'Namespace')}
            </label>
            <select
              id="translation-namespace-select"
              value={namespaceSelection}
              onChange={(e) => setNamespaceSelection(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground"
              data-testid="translation-namespace-select"
            >
              <option value="">{t('translation_admin.namespace_all', 'All namespaces')}</option>
              {namedNamespaces.map((entry) => (
                <option key={entry.value} value={entry.value}>{entry.value}</option>
              ))}
              {hasUncategorized && (
                <option value={NULL_NAMESPACE_VALUE}>{t('translation.namespace_uncategorized', 'Uncategorized')}</option>
              )}
            </select>
          </div>
          <div className="min-w-[160px]">
            <label htmlFor="translation-editor-language-select" className="text-xs font-medium block mb-1">
              {t('translation_admin.filters.editor_language_label', 'Editor language')}
            </label>
            <select
              id="translation-editor-language-select"
              value={editorLanguage}
              onChange={(e) => setEditorLanguage(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground"
              data-testid="translation-editor-language-select"
            >
              {orderedLanguages.map((language) => (
                <option key={language.code} value={language.code}>{language.name} ({language.code})</option>
              ))}
            </select>
          </div>
          <div className="flex items-end pb-1.5">
            <label className="flex items-center gap-2 text-sm text-foreground select-none">
              <input
                type="checkbox"
                checked={onlyMissingFilter}
                onChange={(e) => setOnlyMissingFilter(e.target.checked)}
                className="rounded border-input"
                data-testid="translation-only-missing-checkbox"
              />
              {t('translation_admin.filters.only_missing_label', 'Only missing translations')}
            </label>
          </div>
        </div>

        {adminActions.length > 0 && (
          <div className="space-y-2" data-testid="translation-admin-actions">
            <div className="flex flex-wrap items-start gap-2">
              {adminActions.map((entry) => {
                const AdminAction = entry.component
                return (
                  <div key={`${entry.pluginName}:${entry.id}`} className="min-w-0">
                    <PluginContributionBoundary
                      slot={`translationAdminActions:${entry.id}`}
                      pluginName={entry.pluginName}
                      label={entry.displayLabel}
                      owner={entry.owner}
                    >
                      <AdminAction {...buildAdminActionProps(entry)} />
                    </PluginContributionBoundary>
                    <p className="mt-1 text-xs text-muted-foreground break-words">
                      {interpolateDefault(t('plugins.sections.provided_by', 'Provided by plugin "{{plugin}}"'), { plugin: entry.owner })}
                    </p>
                  </div>
                )
              })}
            </div>
            {adminInspect && (
              <div
                className="rounded-md border border-input bg-muted p-4 text-sm text-foreground break-words"
                data-testid="translation-admin-scope"
              >
                {`${t('translation_admin.filters.editor_language_label', 'Editor language')}: ${adminInspect.language} · `}
                {`${t('translation_admin.filters.namespace_label', 'Namespace')}: ${adminInspect.namespaceLabel} · `}
                {`${t('translation_admin.filters.search_label', 'Search translations')}: ${adminInspect.search || '—'} · `}
                {`${t('translation_admin.filters.only_missing_label', 'Only missing translations')}: ${adminInspect.onlyMissing ? t('common.yes', 'Yes') : t('common.no', 'No')}`}
              </div>
            )}
            {!isLoading && catalog?.total === 0 && (
              <p className="text-xs text-muted-foreground break-words">
                {t('plugins.seams.translation.empty_scope', 'No translations match this scope. Adjust the language or filters and try again.')}
              </p>
            )}
          </div>
        )}

        {isLoading && !inReview ? (
          <TableSkeleton rows={5} cols={skeletonCols} />
        ) : isError ? (
          <EmptyState
            variant="error"
            title={t('translation_admin.error.load_title', "Couldn't load translations")}
            description={t('translation_admin.error.load_description', 'Check your connection and try again.')}
            onButtonClick={() => refetch()}
          />
        ) : !inReview && rows.length === 0 ? (
          hasFilters ? (
            <EmptyState variant="no-results" onButtonClick={clearFilters} />
          ) : (
            <EmptyState
              variant="empty"
              title={t('translation_admin.empty.title', 'No UI strings available')}
              description={t('translation_admin.empty.description', 'The interface text catalog has not been seeded. Contact your administrator.')}
            />
          )
        ) : (
          <div className="space-y-3">
            <TranslationCatalogTable
              rows={rows}
              editorLanguage={editorLanguage}
              editorLanguageName={editorLanguageName}
              hasUpdate={hasUpdate}
              hasDelete={hasDelete}
              commitField={commitField}
              getFieldStatus={getFieldStatus}
              review={review}
              reviewReadonly={!hasUpdate}
              onReviewChange={(uiStringId, value) => reviewSession.editDraft(uiStringId, value)}
              onReviewDiscard={(uiStringId) => reviewSession.discardRow(uiStringId)}
            />
            {!inReview && (
              <PaginationBar
                page={page}
                pageSize={pageSize}
                total={total}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            )}
          </div>
        )}
      </div>
    </ErrorBoundary>
  )
}
