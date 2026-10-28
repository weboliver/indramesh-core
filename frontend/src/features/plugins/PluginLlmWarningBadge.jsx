import { useTranslation } from 'react-i18next'
import { LucideIcon } from '../../shared/lib/lucide-icons'

// Phase 318: informational amber badge for a plugin that declares `requires_llm`
// while the Core LLM client is not usable. Like the production warning it never
// disables the plugin or its kill switch; it only says why the plugin's AI
// actions are hidden.
export const LLM_WARNING_COPY = {
  not_configured: {
    badgeKey: 'plugins.llm.not_configured_badge',
    badge: 'LLM not configured',
    bodyKey: 'plugins.llm.not_configured_body',
    body: 'This plugin needs an LLM and hides its AI actions until one is configured. Set LLM_API_KEY and LLM_MODEL (see the LLM block in .env.example) and restart backend and worker.',
  },
  misconfigured: {
    badgeKey: 'plugins.llm.misconfigured_badge',
    badge: 'LLM configuration invalid',
    bodyKey: 'plugins.llm.misconfigured_body',
    body: 'This plugin needs an LLM, but the LLM configuration is invalid, so its AI actions stay hidden. Correct the LLM_* settings and restart backend and worker.',
  },
}

export default function PluginLlmWarningBadge({ name, kind }) {
  const { t } = useTranslation()
  const copy = LLM_WARNING_COPY[kind]
  if (!copy) return null
  const body = t(copy.bodyKey, copy.body)

  return (
    <span
      role="status"
      title={body}
      aria-label={body}
      data-testid={'plugin-llm-warning-' + name}
      className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
    >
      <LucideIcon name="triangle-alert" size={12} aria-hidden="true" />
      {t(copy.badgeKey, copy.badge)}
    </span>
  )
}
