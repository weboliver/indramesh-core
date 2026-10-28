import { useEffect, useMemo, useRef } from 'react'
import { useQueries } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { LucideIcon } from '../lib/lucide-icons'
import { normalizeBadgeMap, splitItemBadges } from './contribution-utils'
import { usePluginItemBadgeResolvers, usePluginRuntime, usePluginText } from './PluginProvider'

// S2/D-03: host-drawn neutral pill. Plugins return data, never colours/tones,
// and never React nodes — the host renders text and a named LucideIcon only
// (T-291-04-01).
const BADGE_PILL_CLASS =
  'inline-flex items-center gap-1 h-5 px-2 text-xs rounded-full bg-muted text-muted-foreground shrink-0'

// One batch entry per applicable, permitted plugin resolver per rendered list.
// The resolver is called once with every visible id — never once per row
// (FEEXT-01 batch resolution, T-291-04-02). Returns `Map<id, badge[]>` with the
// per-id arrays kept as stable as React allows so memoized host rows do not
// re-render when nothing resolved differently.
export function usePluginItemBadges(kind, ids, context) {
  const resolvers = usePluginItemBadgeResolvers(kind)
  const { reportFault } = usePluginRuntime()

  // Sorted, de-duplicated, truthy ids memoized on their joined string, so the
  // react-query cache key is stable across a re-render that re-creates the
  // caller's array literal.
  const idsKey = (ids || []).join('\u0000')
  const sortedIds = useMemo(() => {
    const seen = new Set()
    const out = []
    for (const id of idsKey ? idsKey.split('\u0000') : []) {
      if (!id || seen.has(id)) continue
      seen.add(id)
      out.push(id)
    }
    return out.sort()
  }, [idsKey])

  const contextKey = JSON.stringify(context ?? {})

  const results = useQueries({
    queries: resolvers.map((entry) => ({
      queryKey: ['plugin-item-badges', entry.pluginName, entry.id, kind, sortedIds, contextKey],
      queryFn: () =>
        Promise.resolve().then(() => entry.resolve({ kind, ids: sortedIds, context })),
      enabled: sortedIds.length > 0,
      staleTime: 30_000,
      retry: false,
    })),
  })

  // A rejected/throwing resolver or a malformed return reports one fault per
  // distinct failure, then contributes nothing; rows stay intact
  // (T-291-04-03). The ref dedupes across the report-triggered re-renders.
  const reportedRef = useRef(new Set())
  useEffect(() => {
    results.forEach((result, index) => {
      const entry = resolvers[index]
      if (!entry || !result) return
      let failureId = null
      let reason = null
      if (result.status === 'error') {
        failureId = `${entry.pluginName}:${entry.id}:error:${result.errorUpdatedAt ?? 0}`
        reason = result.error?.message || 'resolver failed'
      } else if (result.status === 'success' && !normalizeBadgeMap(result.data, sortedIds).ok) {
        failureId = `${entry.pluginName}:${entry.id}:malformed:${result.dataUpdatedAt ?? 0}`
        reason = 'malformed result'
      }
      if (!failureId || reportedRef.current.has(failureId)) return
      reportedRef.current.add(failureId)
      reportFault(entry.pluginName, {
        kind: 'slot_render',
        slot: 'itemBadges:' + entry.id,
        reason: 'itemBadges:' + kind + ': ' + reason,
      })
    })
  }, [results, resolvers, kind, sortedIds, reportFault])

  return useMemo(() => {
    const map = new Map()
    resolvers.forEach((entry, index) => {
      const result = results[index]
      if (!result || result.status !== 'success') return
      const { ok, byId } = normalizeBadgeMap(result.data, sortedIds)
      if (!ok) return
      byId.forEach((badges, id) => {
        const tagged = badges.map((badge) => ({ ...badge, pluginName: entry.pluginName }))
        map.set(id, [...(map.get(id) || []), ...tagged])
      })
    })
    return map
  }, [resolvers, sortedIds, results])
}

function resolvePluginText(pluginText, badge, keyField, field) {
  // WR-03: `title` is optional, so a `titleKey` without a `title` used to pass
  // `undefined` as i18next's `defaultValue`; i18next then echoed the raw key
  // (truthy), defeating the callers' `|| label` fallback. Default to the badge
  // label so the documented chain (active language → plugin English → default)
  // still ends at a human-readable string.
  const fallback = badge[field] ?? badge.label
  return badge[keyField] ? pluginText(badge.pluginName, badge[keyField], fallback) : fallback
}

// Renders the host-drawn badge pills for one item: at most two, then a single
// `+N` pill whose title lists the hidden labels. Non-interactive spans, so a
// click still selects the host row; the item name stays the primary anchor.
export function PluginItemBadges({ badges }) {
  const { t } = useTranslation()
  const pluginText = usePluginText()

  if (!badges || badges.length === 0) return null

  const { visible, hidden } = splitItemBadges(badges, 2)
  const hiddenLabels = hidden.map(
    (badge) => resolvePluginText(pluginText, badge, 'titleKey', 'title') || badge.label
  )

  return (
    <span className="ml-2 inline-flex items-center gap-1 shrink-0">
      {visible.map((badge, index) => {
        const label = resolvePluginText(pluginText, badge, 'labelKey', 'label')
        const title = resolvePluginText(pluginText, badge, 'titleKey', 'title') || label
        return (
          <span
            key={`${badge.pluginName}:${label}:${index}`}
            data-testid="plugin-item-badge"
            className={BADGE_PILL_CLASS}
            title={title}
            aria-label={title}
          >
            {badge.icon ? <LucideIcon name={badge.icon} size={12} /> : null}
            <span className="max-w-[160px] truncate">{label}</span>
          </span>
        )
      })}
      {hidden.length > 0 ? (
        <span
          data-testid="plugin-item-badge-more"
          className={BADGE_PILL_CLASS}
          title={interpolateDefault(
            t('plugins.item_badges.more_title', 'More badges: {{labels}}'),
            { labels: hiddenLabels.join(', ') }
          )}
          aria-label={interpolateDefault(
            t('plugins.item_badges.more_title', 'More badges: {{labels}}'),
            { labels: hiddenLabels.join(', ') }
          )}
        >
          {interpolateDefault(t('plugins.item_badges.more', '+{{count}}'), {
            count: hidden.length,
          })}
        </span>
      ) : null}
    </span>
  )
}