import { useTranslation } from 'react-i18next'
import { cn } from '../../shared/lib/utils'

const STATE_STYLES = {
  active: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  loaded: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
  error: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  disabled: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300',
}

const STATE_TITLE_DEFAULTS = {
  active: 'Active — loaded and ready',
  loaded: 'Loaded — not yet activated',
  error: 'Error — loading or registration failed',
  disabled: 'Disabled (kill switch)',
}

export default function PluginStateBadge({ name, state }) {
  const { t } = useTranslation()
  if (!state) return null

  const style = STATE_STYLES[state] || STATE_STYLES.disabled
  const titleDefault = STATE_TITLE_DEFAULTS[state] || state

  return (
    <span
      className={cn('inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium', style)}
      title={t(`plugins.state.${state}_title`, titleDefault)}
      data-testid={'plugin-state-' + name}
    >
      {state}
    </span>
  )
}