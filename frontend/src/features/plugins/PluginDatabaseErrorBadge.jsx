import { useTranslation } from 'react-i18next'
import { cn } from '../../shared/lib/utils'

export default function PluginDatabaseErrorBadge({ name }) {
  const { t } = useTranslation()

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium',
        'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400'
      )}
      title={t('plugins.database.error_title', 'The database migrations of this plugin failed. The plugin remains inactive; full technical details are in the server logs.')}
      data-testid={'plugin-database-error-' + name}
    >
      {t('plugins.database.error_badge', 'Migration failed')}
    </span>
  )
}
