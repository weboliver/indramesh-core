import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import { showToast } from '../../shared/components/Toast'

// JSON result panel (304 GQL-02/D-06/D-14). Renders the HTTP-200 {data, errors}
// envelope with a red-text GraphQL-errors block above the data JSON. Request-level
// 4xx responses render as surface-level error text, never as a JSON result.
export default function SavedQueryResultPanel({ result, error, isLoading }) {
  const { t } = useTranslation()

  const formatted = result ? JSON.stringify(result, null, 2) : ''
  const errors = Array.isArray(result?.errors) ? result.errors : []

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(formatted)
      showToast(t('graphql.saved_queries.copy_success', 'JSON copied.'))
    } catch {
      showToast(t('graphql.saved_queries.copy_failed', 'Copy failed. Select the JSON and copy it manually.'), { type: 'error' })
    }
  }

  return (
    <section
      className="space-y-3 rounded-lg border bg-card p-4"
      data-testid="saved-query-result"
      aria-label={t('graphql.saved_queries.result_heading', 'Result')}
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t('graphql.saved_queries.result_heading', 'Result')}</h2>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleCopy}
          disabled={!result}
          data-testid="saved-query-copy"
        >
          {t('graphql.saved_queries.copy_json', 'Copy JSON')}
        </Button>
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground" role="status" aria-busy="true" data-testid="saved-query-result-loading">
          {t('graphql.saved_queries.running', 'Running…')}
        </p>
      ) : error ? (
        <p className="whitespace-pre-wrap break-words text-sm text-red-700 dark:text-red-400" role="alert" data-testid="saved-query-result-error">
          {error.message}
        </p>
      ) : !result ? (
        <p className="text-sm text-muted-foreground" data-testid="saved-query-result-empty">
          {t('graphql.saved_queries.result_empty', 'Run the query to see its JSON result here.')}
        </p>
      ) : (
        <div className="space-y-3">
          {errors.length > 0 && (
            <div data-testid="saved-query-graphql-errors">
              <h3 className="text-sm font-semibold text-red-700 dark:text-red-400">
                {t('graphql.saved_queries.result_errors_heading', 'GraphQL errors')}
              </h3>
              <ul className="mt-1 space-y-1">
                {errors.map((entry, index) => (
                  <li key={index} className="whitespace-pre-wrap break-words text-sm text-red-700 dark:text-red-400">
                    {entry?.message ?? String(entry)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <pre
            className="max-h-[50vh] min-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted p-3 font-mono text-sm"
            data-testid="saved-query-result-json"
          >
            {formatted}
          </pre>
        </div>
      )}
    </section>
  )
}