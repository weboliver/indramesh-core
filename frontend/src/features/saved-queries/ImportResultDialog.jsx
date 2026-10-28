import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import { selectPluralKey } from '../../shared/lib/i18n'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const IMPORT_RESULT_DEFAULTS = {
  graphql_saved_queries_import_result_one: '{{count}} query imported. {{renamed}} renamed to avoid duplicate names.',
  graphql_saved_queries_import_result_other: '{{count}} queries imported. {{renamed}} renamed to avoid duplicate names.',
}

// Labelled import-result modal (304 GQL-04/D-11): counts + renamed note, focus
// contained, Escape/Close dismissed, initial focus on the result heading.
export default function ImportResultDialog({ open, result, onClose }) {
  const { t } = useTranslation()
  const headingRef = useRef(null)

  useEffect(() => {
    if (!open) return
    headingRef.current?.focus()
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  if (!open) return null

  const imported = result?.imported ?? 0
  const renamed = result?.renamed ?? 0
  const pluralKey = selectPluralKey('graphql.saved_queries.import_result', imported)
  const pluralDefault = pluralKey.endsWith('_one')
    ? IMPORT_RESULT_DEFAULTS.graphql_saved_queries_import_result_one
    : IMPORT_RESULT_DEFAULTS.graphql_saved_queries_import_result_other
  const summary = interpolateDefault(t(pluralKey, pluralDefault), { count: imported, renamed })

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-1 sm:p-0"
      data-testid="import-result-dialog"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-result-heading"
        className="w-full max-w-lg rounded-lg border bg-card p-4 shadow-xl"
      >
        <h2
          id="import-result-heading"
          ref={headingRef}
          tabIndex={-1}
          className="text-lg font-semibold focus:outline-none"
        >
          {t('graphql.saved_queries.import_heading', 'Import saved queries')}
        </h2>
        <p className="mt-2 text-sm" data-testid="import-result-summary">{summary}</p>
        <div className="mt-4 flex justify-end">
          <Button type="button" variant="primary" size="md" onClick={onClose} data-testid="import-result-close">
            {t('common.close', 'Close')}
          </Button>
        </div>
      </div>
    </div>
  )
}