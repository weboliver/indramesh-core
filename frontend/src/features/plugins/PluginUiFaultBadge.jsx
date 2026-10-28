import { useTranslation } from 'react-i18next'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'

const VARIANTS = {
  load: {
    labelKey: 'plugins.ui_status.fault',
    labelDefault: 'UI fault',
    titleKey: 'plugins.ui_status.fault_title',
    titleDefault: 'The plugin UI failed in this session. Reloading the page will try again.',
    testIdPrefix: 'plugin-ui-fault-',
  },
  render: {
    labelKey: 'plugins.ui_fault.render_badge',
    labelDefault: 'Render fault',
    titleKey: 'plugins.ui_fault.render_title',
    titleDefault: 'A contribution of this plugin failed while rendering. The plugin stays active; the affected slot shows a fallback.',
    testIdPrefix: 'plugin-render-fault-',
  },
}

export default function PluginUiFaultBadge({ name, kind = 'load' }) {
  const { t } = useTranslation()
  const variant = VARIANTS[kind] || VARIANTS.load

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium',
        'bg-destructive/10 text-destructive'
      )}
      title={t(variant.titleKey, variant.titleDefault)}
      data-testid={variant.testIdPrefix + name}
    >
      <LucideIcon name="triangle-alert" size={12} />
      {t(variant.labelKey, variant.labelDefault)}
    </span>
  )
}