import { useLanguages, useUpdateLanguage } from '../../shared/hooks/use-languages'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { showToast } from '../../shared/components/Toast'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

const DEACTIVATE_IMPACT_MESSAGE =
  'Deactivating hides existing translations and disables the language in schemas until reactivated.'

export default function LanguagesAdminPage() {
  const { t } = useTranslation()
  const { data: languages = [], isLoading, isError, refetch } = useLanguages()
  const updateLanguage = useUpdateLanguage()

  const handleToggle = async (language) => {
    const activating = !language.is_active

    if (!activating) {
      const confirmed = await confirmDialog({
        message: interpolateDefault(t('settings.languages.confirm_deactivate', 'This language is enabled in schema(s) and has translation(s). {{impact}}'), { impact: t('settings.languages.deactivate_impact', DEACTIVATE_IMPACT_MESSAGE) }),
        danger: false,
      })
      if (!confirmed) return
    }

    updateLanguage.mutate(
      { id: language.id, is_active: activating },
      {
        onSuccess: () => {
          showToast(
             activating
               ? interpolateDefault(t('toasts.languages.activated', '{{name}} activated.'), { name: language.name })
               : interpolateDefault(t('toasts.languages.deactivated', '{{name}} deactivated.'), { name: language.name }),
            { type: 'success' }
          )
        },
        onError: () => {
           showToast(t('toasts.languages.update_failed', 'Failed to update language.'), { type: 'error' })
        },
      }
    )
  }

  return (
    <div className="space-y-6" data-testid="languages-admin-page">
      <PageHeader
         title={t('settings.languages.title', 'Languages')}
         description={t('settings.languages.description', 'Manage which languages are available system-wide. Languages enabled here can be selected per schema.')}
      />

      {isLoading && <TableSkeleton rows={5} cols={1} />}

      {isError && !isLoading && (
        <EmptyState variant="error" onButtonClick={() => refetch()} />
      )}

      {!isLoading && !isError && languages.length === 0 && (
        <EmptyState
           title={t('settings.languages.empty_title', 'No languages available')}
           description={t('settings.languages.empty_description', 'System languages have not been seeded. Contact your administrator.')}
        />
      )}

      {!isLoading && !isError && languages.length > 0 && (
        <div className="bg-card border rounded-lg divide-y">
          {languages.map((language) => (
            <div
              key={language.id}
              className={cn(
                'flex items-center justify-between px-4 py-3',
                !language.is_active && 'opacity-60'
              )}
              data-testid={`lang-row-${language.code}`}
            >
              <span className="text-sm">
                {language.name} ({language.code})
              </span>
              <button
                role="switch"
                aria-checked={language.is_active}
                 aria-label={interpolateDefault(t('settings.languages.toggle_label', '{{name}} language toggle'), { name: language.name })}
                onClick={() => !updateLanguage.isPending && handleToggle(language)}
                disabled={updateLanguage.isPending}
                className={cn(
                  'relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 cursor-pointer',
                  language.is_active ? 'bg-primary' : 'bg-input'
                )}
                data-testid={`lang-toggle-${language.code}`}
              >
                <span
                  className={cn(
                    'pointer-events-none inline-block h-4 w-4 rounded-full bg-background shadow transform ring-0 transition-transform',
                    language.is_active ? 'translate-x-4' : 'translate-x-0'
                  )}
                />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
