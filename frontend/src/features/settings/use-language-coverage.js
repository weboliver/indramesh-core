import { useQuery } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'

/**
 * Coverage per active language (D-08).
 *
 * One `GET /ui-translations/catalog` read fills every row: the response's
 * `coverage[]` already lists `{ code, translated, total }` for all active
 * languages and `total` is the live `ui_strings` count. There is no second
 * counting path and no additional endpoint.
 *
 * The query never blocks the language list: retries are disabled and an
 * `enabled` guard means a 400/403 simply leaves the cells without a value.
 */
export function useLanguageCoverage(editorLanguage, enabled) {
  const query = useQuery({
    queryKey: ['language-coverage', editorLanguage],
    queryFn: () => api.get('ui-translations/catalog', {
      params: { editor_language: editorLanguage, skip: 0, limit: 1 },
    }).then((response) => response.data),
    enabled: enabled && Boolean(editorLanguage),
    retry: false,
  })

  const coverage = {}
  for (const entry of query.data?.coverage ?? []) {
    coverage[entry.code] = { translated: entry.translated, total: entry.total }
  }

  return { coverage, isLoading: query.isLoading, isError: query.isError }
}
