import { useTranslation } from 'react-i18next'
import { LucideIcon } from '../../shared/lib/lucide-icons'

// Core-rendered production-only warning affordance (304 GQL-06/D-18). It is an
// informational amber badge — never an error/fault badge — and it never disables
// the plugin or its kill switch. Rendered only when IS_PRODUCTION and the plugin
// declares the projected `enables_http_graphql` flag.
export default function PluginProductionWarningBadge({ name }) {
  const { t } = useTranslation()
  const body = t(
    'plugins.production_warning.body',
    'Not intended for production. While active in production this plugin enables the HTTP GraphQL endpoint /api/v1/graphql.'
  )

  return (
    <span
      role="status"
      title={body}
      aria-label={body}
      data-testid={'plugin-production-warning-' + name}
      className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
    >
      <LucideIcon name="triangle-alert" size={12} aria-hidden="true" />
      {t('plugins.production_warning.badge', 'Not for production')}
    </span>
  )
}