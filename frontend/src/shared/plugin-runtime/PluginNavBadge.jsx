import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { selectPluralKey } from '../lib/i18n'
import { normalizeNavBadge } from './contribution-utils'
import { PluginSlotBoundary } from './PluginSlotBoundary'

// The plugin's `badge` hook runs inside this child component, wrapped by its own
// PluginSlotBoundary (fallback={null}): a throwing hook loses only the pill while
// the nav link stays usable (T-291-03-01, D-04/D-05). The hook is called at the
// top level unconditionally, so a plugin may use React/TanStack hooks inside it
// (or return a static number from a plain function).
function PluginNavBadgeValue({ useBadge, badgeLabel, itemId }) {
  const { t } = useTranslation()
  const value = useBadge()
  const display = normalizeNavBadge(value)
  if (display === null) return null

  // Accessible name: the plugin's badgeLabel(count) wins when it returns a
  // non-empty string, otherwise host copy (S3, D-07). badgeLabel is called inside
  // a try/catch — a throw falls back to the host text rather than losing the pill.
  let ariaLabel = null
  if (typeof badgeLabel === 'function') {
    try {
      const custom = badgeLabel(value)
      if (typeof custom === 'string' && custom !== '') ariaLabel = custom
    } catch {
      ariaLabel = null
    }
  }
  if (ariaLabel === null) {
    ariaLabel =
      display === '99+'
        ? t('plugins.nav.badge_overflow', 'More than 99 items')
        : interpolateDefault(
            selectPluralKey('plugins.nav.badge_count', value) === 'plugins.nav.badge_count_one'
              ? t('plugins.nav.badge_count_one', '{{count}} item')
              : t('plugins.nav.badge_count_other', '{{count}} items'),
            { count: value }
          )
  }

  return (
    <span className="ml-auto pl-2 shrink-0">
      <span
        className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-muted px-1.5 text-xs font-normal tabular-nums text-muted-foreground"
        data-testid={'plugin-nav-badge-' + itemId}
        aria-label={ariaLabel}
      >
        {display}
      </span>
    </span>
  )
}

// A neutral count pill for a plugin nav item (D-07). Renders nothing unless the
// item declares a `badge` hook. The host draws the pill in the muted token only
// — never an accent or warning colour, a dot, a count on the dropdown trigger,
// or animation — and the host adds no polling interval (D-04/D-05/D-06).
export function PluginNavBadge({ item }) {
  if (typeof item.badge !== 'function') return null
  return (
    <PluginSlotBoundary
      key={item.pluginName + ':' + item.id}
      slot={'navItems:' + item.id + ':badge'}
      pluginName={item.pluginName}
      fallback={null}
    >
      <PluginNavBadgeValue useBadge={item.badge} badgeLabel={item.badgeLabel} itemId={item.id} />
    </PluginSlotBoundary>
  )
}
