import { useCallback, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'

// Phase 235 / EXP-01: feature-local export mutation. Normalizes the server's
// paths-only POST /ui-translations/export outcome into exactly one of
// 'success' | 'error' | 'conflict' | 'unavailable' (D-15/D-16). Never polls,
// never stores state outside this hook, never infers environment client-side.
function classifyExportOutcome(err) {
  const responseStatus = err?.response?.status
  if (responseStatus === 409) return { type: 'conflict' }
  if (responseStatus === 403) return { type: 'unavailable' }
  return { type: 'error' }
}

/**
 * useTranslationExport() — POST ui-translations/export mutation plus the
 * durable result state the Translation admin header/panel render from.
 *
 * D-15: the prior result is preserved while a new request is in flight
 * (no reset on mutate) and replaced only once that request settles into
 * one of the four outcome types above.
 */
export function useTranslationExport() {
  const [result, setResult] = useState(null)

  const mutation = useMutation({
    mutationFn: () => api.post('ui-translations/export').then((response) => response.data),
    onSuccess: (data) => {
      const paths = Array.isArray(data?.paths) ? data.paths : []
      // A 200 with no paths is malformed for an active-language export
      // (UI-SPEC: "an empty file list is not a valid success presentation")
      // — treat it as the generic failure panel rather than claiming success.
      if (paths.length === 0) {
        setResult({ type: 'error' })
        return
      }
      setResult({ type: 'success', paths })
    },
    onError: (err) => {
      setResult(classifyExportOutcome(err))
    },
  })

  const runExport = useCallback(() => {
    mutation.mutate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    runExport,
    isExporting: mutation.isPending,
    result,
  }
}
