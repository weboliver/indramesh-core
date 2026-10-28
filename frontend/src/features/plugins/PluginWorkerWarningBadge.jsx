import { useTranslation } from 'react-i18next'
import { cn } from '../../shared/lib/utils'

export default function PluginWorkerWarningBadge({ name }) {
  const { t } = useTranslation()

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium',
        'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400'
      )}
      title={t('plugins.worker.warning_title', 'The worker process has not loaded this version yet. Restart the worker so the plugin takes effect there.')}
      data-testid={'plugin-worker-warning-' + name}
    >
      {t('plugins.worker.warning_badge', 'Worker restart required')}
    </span>
  )
}