import { useSources } from '../../shared/hooks/use-sources'
import { useTranslation } from 'react-i18next'

export default function SourceSelect({ value, onChange, disabled }) {
  const { t } = useTranslation()
  const { data: sources, isLoading } = useSources(0, 1000)

  return (
    <div className="space-y-1.5">
      <label htmlFor="wizard-source-select" className="text-sm font-medium text-foreground">
        {t('wizard.source.label', 'Source')} <span className="text-destructive">*</span>
      </label>
      <select
        id="wizard-source-select"
        value={value || ''}
        onChange={e => onChange(e.target.value || null)}
        disabled={disabled}
        data-testid="wizard-source-select"
        className="w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {isLoading ? (
          <option value="">{t('wizard.source.loading', 'Loading sources...')}</option>
        ) : sources?.length === 0 ? (
          <option value="" disabled>{t('wizard.source.empty', 'No sources available. Create a source first.')}</option>
        ) : (
          <>
            {!value && <option value="" disabled>{t('wizard.source.select', 'Select a Source')}</option>}
            {sources?.map(s => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </>
        )}
      </select>
      <p className="text-xs text-muted-foreground">{t('wizard.source.help', 'The Source determines where this schema belongs.')}</p>
    </div>
  )
}
