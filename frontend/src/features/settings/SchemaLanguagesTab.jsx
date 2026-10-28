import { useLanguages, useSchemaLanguages, useSetSchemaLanguages } from '../../shared/hooks/use-languages'
import { useUpdateSchema } from '../../shared/hooks/use-schemas'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { showToast } from '../../shared/components/Toast'
import { useTranslation } from 'react-i18next'

export default function SchemaLanguagesTab({ schemaId, schema }) {
  const { t } = useTranslation()
  const { data: allLanguages = [], isLoading: languagesLoading, isError: languagesError } = useLanguages()
  const { data: enabledLanguages = [], isLoading: enabledLoading, isError: enabledError } = useSchemaLanguages(schemaId)
  const setSchemaLanguages = useSetSchemaLanguages(schemaId)
  const updateSchema = useUpdateSchema()

  if (languagesLoading || enabledLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (languagesError || enabledError) {
    return <EmptyState variant="error" />
  }

  const enabledCodes = enabledLanguages.map((l) => l.code)
  // Penultimate-language warning: leaving exactly one language enabled is allowed
  // (translations stop serving but stay stored) — derived from live enabled-language
  // state so it stays correct across remounts and concurrent edits, not just the last toggle.
  const showPenultimateHint = enabledCodes.length === 1

  // Active system languages, plus any already-enabled-but-now-deactivated languages
  // so the row stays visible (flagged) until reactivated.
  const activeLanguages = allLanguages.filter((l) => l.is_active)
  const activeCodes = new Set(activeLanguages.map((l) => l.code))
  const deactivatedButEnabled = enabledLanguages.filter((l) => !activeCodes.has(l.code))
  const rows = [...activeLanguages, ...deactivatedButEnabled]

  const handleToggle = (code, checked) => {
    let nextCodes
    if (checked) {
      nextCodes = [...new Set([...enabledCodes, code])]
    } else {
      if (enabledCodes.length <= 1) return
      nextCodes = enabledCodes.filter((c) => c !== code)
    }
    setSchemaLanguages.mutate(nextCodes, {
       onSuccess: () => showToast(t('toasts.schema_languages.saved', 'Schema languages saved'), { type: 'success' }),
       onError: (err) => showToast(err?.response?.data?.detail || t('toasts.schema_languages.save_failed', 'Failed to save schema languages.'), { type: 'error' }),
    })
  }

  const handleDefaultChange = (e) => {
    const default_language_id = e.target.value
    updateSchema.mutate({ id: schemaId, default_language_id }, {
       onSuccess: () => showToast(t('toasts.schema_languages.default_saved', 'Default language saved'), { type: 'success' }),
       onError: (err) => showToast(err?.response?.data?.detail || t('toasts.schema_languages.default_save_failed', 'Failed to save default language.'), { type: 'error' }),
    })
  }

  const enabledLanguageOptions = enabledLanguages

  return (
    <div className="space-y-4">
      <div>
         <h3 className="text-lg font-semibold">{t('settings.schema_languages.enabled_title', 'Enabled Languages')}</h3>
        <p className="text-sm text-muted-foreground">
           {t('settings.schema_languages.description', 'Choose which languages are available for this schema. The default language is the one content is authored in.')}
        </p>
      </div>

      <div className="space-y-2">
        {rows.map((language) => {
          const checked = enabledCodes.includes(language.code)
          const isDeactivated = !activeCodes.has(language.code)
          const isLastEnabled = checked && enabledCodes.length <= 1
          const disabled = isDeactivated || isLastEnabled

          return (
            <div key={language.code} className={`flex items-center gap-2 py-1 ${isDeactivated ? 'opacity-50 cursor-not-allowed' : ''}`}>
              <input
                type="checkbox"
                id={`schema-lang-${language.code}`}
                data-testid={`schema-lang-check-${language.code}`}
                checked={checked}
                disabled={disabled}
                onChange={(e) => handleToggle(language.code, e.target.checked)}
                className="h-4 w-4 rounded border-input"
              />
              <label htmlFor={`schema-lang-${language.code}`} className="text-sm">
                {language.name} ({language.code})
              </label>
              {isDeactivated && (
                 <span className="text-xs text-muted-foreground">{t('settings.schema_languages.reactivated', 'Reactivated system-wide language')}</span>
              )}
              {!isDeactivated && isLastEnabled && (
                  <span className="text-xs text-muted-foreground">{t('settings.schema_languages.minimum_enabled', 'At least one language must remain enabled')}</span>
              )}
            </div>
          )
        })}
      </div>

      {showPenultimateHint && (
        <p
          data-testid="penultimate-language-hint"
          className="text-xs text-amber-700 dark:text-amber-300 bg-amber-100 dark:bg-amber-900/40 rounded px-2 py-1.5"
        >
           {t('settings.schema_languages.penultimate_hint', 'Removing this language leaves exactly one enabled — translations stop serving (rows stay stored) and multilingual ObjectTypes stop applying until a second language is re-enabled.')}
        </p>
      )}

      <div className="space-y-1.5 pt-2">
         <label htmlFor="schema-default-lang" className="text-sm font-medium">{t('settings.schema_languages.default_label', 'Default Language')}</label>
        <select
          id="schema-default-lang"
          data-testid="schema-default-lang"
          value={schema?.default_language_id || ''}
          onChange={handleDefaultChange}
          className="w-full max-w-xs rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
        >
          {enabledLanguageOptions.map((language) => (
            <option key={language.id} value={language.id}>
              {language.name} ({language.code})
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
