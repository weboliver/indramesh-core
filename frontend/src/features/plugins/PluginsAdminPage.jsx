import { Fragment, useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableSkeleton from '../../shared/components/TableSkeleton'
import TableToolbar from '../../shared/components/TableToolbar'
import { Button } from '../../shared/components/Button'
import { useCan } from '../../shared/components/Can'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { showToast } from '../../shared/components/Toast'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { IS_PRODUCTION } from '../../shared/lib/env'
import { usePluginFaults, usePluginRuntime } from '../../shared/plugin-runtime/PluginProvider'
import { usePluginHealth, usePlugins, useUpdatePluginState } from './use-plugins'
import {
  isKillSwitchInteractive,
  llmWarningKind,
  nextKillSwitchTarget,
  shouldShowDatabaseError,
  shouldShowWorkerWarning,
} from './plugin-admin-helpers'
import PluginConfigForm from './PluginConfigForm'
import PluginDatabaseErrorBadge from './PluginDatabaseErrorBadge'
import PluginLlmWarningBadge, { LLM_WARNING_COPY } from './PluginLlmWarningBadge'
import PluginProductionWarningBadge from './PluginProductionWarningBadge'
import PluginStateBadge from './PluginStateBadge'
import PluginUiFaultBadge from './PluginUiFaultBadge'
import PluginWorkerWarningBadge from './PluginWorkerWarningBadge'

const EM_DASH = '—'

const UI_STATUS_LABELS = {
  pending: ['plugins.ui_status.pending', 'Pending'],
  loading: ['plugins.ui_status.loading', 'Loading'],
  ready: ['plugins.ui_status.ready', 'Ready'],
  ui_fault: ['plugins.ui_status.fault', 'UI fault'],
}

function faultReasonText(entry, t) {
  const reason = entry.reason || ''
  if (entry.kind === 'slot_render') {
    return t('plugins.ui_fault.slot_render', 'A contribution failed while rendering ({{slot}}).', {
      slot: entry.slot ?? '',
    })
  }
  if (entry.kind === 'slot_conflict') {
    return t('plugins.ui_fault.slot_conflict', 'A slot was already claimed ({{slot}}).', {
      slot: entry.slot ?? '',
    })
  }
  if (entry.kind === 'entry_invalid') {
    return interpolateDefault(
      t(
        'plugins.ui_fault.entry_invalid',
        'A contribution was rejected because it is malformed ({{slot}}).'
      ),
      { slot: entry.slot ?? '' }
    )
  }
  if (reason.startsWith('undeclared:')) {
    return t('plugins.ui_fault.undeclared', 'The bundle registers undeclared extension points ({{points}}).', {
      points: reason.slice('undeclared:'.length).trim(),
    })
  }
  return t('plugins.ui_fault.bundle', 'The UI bundle could not be loaded.')
}

function HealthResultPill({ healthy }) {
  const { t } = useTranslation()
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        healthy
          ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400'
          : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400'
      )}
    >
      {healthy ? t('plugins.health.ok', 'Healthy') : t('plugins.health.failed', 'Failed')}
    </span>
  )
}

function HealthResultBlock({ health }) {
  const { t } = useTranslation()

  if (health.isFetching) {
    return <span className="text-sm text-muted-foreground">{t('plugins.health.running', 'Health check running...')}</span>
  }

  if (health.isError) {
    const detail = health.error?.response?.data?.detail
    return (
      <span className="flex flex-wrap items-center gap-2">
        <HealthResultPill healthy={false} />
        {detail && <span className="text-xs text-muted-foreground whitespace-pre-wrap break-words">{String(detail)}</span>}
      </span>
    )
  }

  if (!health.data) {
    return <span className="text-sm text-muted-foreground">{t('plugins.health.not_checked', 'Not checked yet')}</span>
  }

  return (
    <span className="flex flex-wrap items-center gap-2">
      <HealthResultPill healthy={health.data.healthy === true} />
      {health.data.healthy !== true && health.data.error && (
        <span className="text-xs text-muted-foreground whitespace-pre-wrap break-words">{health.data.error}</span>
      )}
    </span>
  )
}

function PluginRow({ plugin, expanded, onToggle, onKeyDown, formatDate, runtimeState, faultEntries, canManage }) {
  const { t } = useTranslation()
  const health = usePluginHealth(plugin.name)
  const updateState = useUpdatePluginState()
  const [killSwitchFailed, setKillSwitchFailed] = useState(false)

  const showLoadBadge = runtimeState === 'ui_fault'
  const hasRenderFaults = faultEntries.some(
    (entry) =>
      entry.kind === 'slot_render' ||
      entry.kind === 'slot_conflict' ||
      entry.kind === 'entry_invalid'
  )
  const showRenderBadge = !showLoadBadge && runtimeState === 'ready' && hasRenderFaults

  // 304 GQL-06/D-18: production-only, flag-driven warning. The mechanism is the
  // manifest-declared boolean projected onto PluginResponse — Core never names a
  // plugin. Amber informational affordance, never a fault badge, and it never
  // disables the plugin or the kill switch.
  const showProductionWarning = IS_PRODUCTION && plugin.enables_http_graphql === true
  const llmWarning = llmWarningKind(plugin)
  const llmCopy = llmWarning ? LLM_WARNING_COPY[llmWarning] : null

  const interactive = isKillSwitchInteractive(plugin.state)
  const killSwitchEnabled = interactive && canManage && !updateState.isPending
  const killSwitchActive = plugin.state === 'active'
  const killSwitchLabelId = `plugin-kill-switch-label-${plugin.name}`
  const killSwitchLabel = killSwitchActive
    ? t('plugins.kill_switch.label', 'Active')
    : t('plugins.kill_switch.enable_label', 'Activate plugin')

  const handleKillSwitch = async () => {
    if (!killSwitchEnabled) return
    const target = nextKillSwitchTarget(plugin.state)
    if (!target) return

    if (target === 'disabled') {
      const confirmed = await confirmDialog({
        message: t(
          'plugins.kill_switch.confirm',
          'Deactivate plugin: deactivate "{{name}}"? Running operations of this plugin will be stopped and its routes will no longer be reachable. The deactivation can be undone at any time.',
          { name: plugin.name }
        ),
        danger: true,
      })
      if (!confirmed) return
    }

    setKillSwitchFailed(false)
    updateState.mutate(
      { name: plugin.name, state: target },
      {
        onSuccess: () =>
          showToast(
            target === 'disabled'
              ? t('plugins.kill_switch.disabled_success', 'Plugin "{{name}}" has been deactivated.', { name: plugin.name })
              : t('plugins.kill_switch.enabled_success', 'Plugin "{{name}}" has been activated.', { name: plugin.name })
          ),
        onError: () => setKillSwitchFailed(true),
      }
    )
  }

  const [uiStatusKey, uiStatusDefault] = UI_STATUS_LABELS[runtimeState] || UI_STATUS_LABELS.pending
  const rejectedEntries = faultEntries.filter((entry) => entry.kind === 'slot_conflict')

  const killSwitchHint = !canManage
    ? t('plugins.kill_switch.hint_no_permission', 'You lack the permission for this action (plugins:manage).')
    : !interactive
      ? t('plugins.kill_switch.hint_unavailable', 'The status cannot be changed in this state.')
      : t('plugins.kill_switch.hint_on', 'Deactivates the plugin in backend and worker. Tables and data are kept.')

  const capabilities = Array.isArray(plugin.capabilities) ? plugin.capabilities : []
  const extensionPoints = Array.isArray(plugin.frontend?.extension_points) ? plugin.frontend.extension_points : []

  return (
    <Fragment>
      <tr
        onClick={onToggle}
        onKeyDown={onKeyDown}
        className={cn('cursor-pointer border-b transition-colors hover:bg-accent/50', expanded && 'bg-muted/30')}
        data-expanded={expanded}
        data-testid={'plugin-row-' + plugin.name}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
      >
        <td className="px-4 py-3" aria-hidden="true">
          <LucideIcon name={expanded ? 'chevron-down' : 'chevron-right'} size={16} className="text-muted-foreground" />
        </td>
        <td className="px-4 py-3 text-sm font-medium">
          <span className="inline-flex flex-wrap items-center gap-2">
            {plugin.name}
            {showProductionWarning && <PluginProductionWarningBadge name={plugin.name} />}
            {llmWarning && <PluginLlmWarningBadge name={plugin.name} kind={llmWarning} />}
            {showLoadBadge && <PluginUiFaultBadge name={plugin.name} kind="load" />}
            {showRenderBadge && <PluginUiFaultBadge name={plugin.name} kind="render" />}
          </span>
        </td>
        <td className="px-4 py-3 text-sm tabular-nums">{plugin.version}</td>
        <td className="px-4 py-3">
          <PluginStateBadge name={plugin.name} state={plugin.state} />
        </td>
        <td className="px-4 py-3 text-sm">
          {shouldShowWorkerWarning(plugin) ? (
            <PluginWorkerWarningBadge name={plugin.name} />
          ) : (
            <span className="text-muted-foreground">{EM_DASH}</span>
          )}
        </td>
        <td className="px-4 py-3 text-sm">
          {health.data ? (
            <HealthResultPill healthy={health.data.healthy === true} />
          ) : health.isError ? (
            <HealthResultPill healthy={false} />
          ) : (
            <span className="text-muted-foreground">{EM_DASH}</span>
          )}
        </td>
        <td className="px-4 py-3 text-right text-sm text-muted-foreground">{t('common.details', 'Details')}</td>
      </tr>

      {expanded && (
        <tr data-testid={'plugin-details-' + plugin.name}>
          <td colSpan={7} className="border-b p-0">
            <div
              className="max-h-[40vh] space-y-4 overflow-y-auto bg-muted/30 px-4 py-3"
              onClick={(event) => event.stopPropagation()}
            >
              {showProductionWarning && (
                <div
                  role="status"
                  data-testid={'plugin-production-callout-' + plugin.name}
                  className="space-y-1 rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950"
                >
                  <h3 className="text-base font-semibold text-amber-700 dark:text-amber-300">
                    {t('plugins.production_warning.badge', 'Not for production')}
                  </h3>
                  <p className="whitespace-pre-wrap break-words text-sm text-amber-700 dark:text-amber-300">
                    {t('plugins.production_warning.body', 'Not intended for production. While active in production this plugin enables the HTTP GraphQL endpoint /api/v1/graphql.')}
                  </p>
                </div>
              )}
              {llmCopy && (
                <div
                  role="status"
                  data-testid={'plugin-llm-callout-' + plugin.name}
                  className="space-y-1 rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950"
                >
                  <h3 className="text-base font-semibold text-amber-700 dark:text-amber-300">
                    {t(llmCopy.badgeKey, llmCopy.badge)}
                  </h3>
                  <p className="whitespace-pre-wrap break-words text-sm text-amber-700 dark:text-amber-300">
                    {t(llmCopy.bodyKey, llmCopy.body)}
                  </p>
                  {plugin.llm_config_error && (
                    <p
                      className="whitespace-pre-wrap break-words text-sm text-amber-700 dark:text-amber-300"
                      data-testid={'plugin-llm-error-' + plugin.name}
                    >
                      {/* React escapes the text node; i18next escaping would show quotes in the reason as entities. */}
                      {t('plugins.llm.error', 'Reason: {{error}}', {
                        error: plugin.llm_config_error,
                        interpolation: { escapeValue: false },
                      })}
                    </p>
                  )}
                </div>
              )}
              <div className="space-y-2">
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.author', 'Author')}</span>
                  <span>{plugin.author ?? EM_DASH}</span>
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.api_version', 'API version')}</span>
                  <span>{plugin.api_version ?? EM_DASH}</span>
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.capabilities', 'Capabilities')}</span>
                  {capabilities.length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {capabilities.map((capability) => (
                        <span
                          key={capability}
                          className="inline-flex items-center rounded-md bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground"
                        >
                          {capability}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span>{EM_DASH}</span>
                  )}
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.bundle', 'UI bundle')}</span>
                  <span className="font-mono text-xs">{plugin.frontend?.bundle ?? EM_DASH}</span>
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.extension_points', 'Extension points')}</span>
                  {extensionPoints.length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {extensionPoints.map((point) => (
                        <span
                          key={point}
                          className="inline-flex items-center rounded-md bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground"
                        >
                          {point}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span>{EM_DASH}</span>
                  )}
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.installed', 'Installed')}</span>
                  <span>{formatDate(plugin.installed_at)}</span>
                </div>
                <div className="flex flex-wrap gap-x-2 text-sm">
                  <span className="min-w-[140px] text-muted-foreground">{t('plugins.row.updated', 'Updated')}</span>
                  <span>{formatDate(plugin.updated_at)}</span>
                </div>
              </div>

              <div className="space-y-2">
                <h3 className="text-lg font-semibold">{t('plugins.table.worker', 'Worker')}</h3>
                {shouldShowWorkerWarning(plugin) ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <PluginWorkerWarningBadge name={plugin.name} />
                    <span className="text-sm text-muted-foreground">
                      {t('plugins.worker.warning_title', 'The worker process has not loaded this version yet. Restart the worker so the plugin takes effect there.')}
                    </span>
                  </div>
                ) : plugin.worker_loaded_at ? (
                  <p className="text-sm">
                    {t('plugins.worker.loaded', 'Loaded by worker: version {{version}} ({{date}})', {
                      version: plugin.worker_loaded_version ?? EM_DASH,
                      date: formatDate(plugin.worker_loaded_at),
                    })}
                  </p>
                ) : (
                  <p className="text-sm text-muted-foreground">{t('plugins.worker.not_loaded', 'Not yet loaded by the worker.')}</p>
                )}
              </div>

              {shouldShowDatabaseError(plugin) || plugin.database_version_table ? (
                <div className="space-y-2" data-testid={'plugin-database-section-' + plugin.name}>
                  <h3 className="text-lg font-semibold">{t('plugins.database.title', 'Database migrations')}</h3>
                  {shouldShowDatabaseError(plugin) ? (
                    <>
                      <div className="flex flex-wrap items-center gap-2">
                        <PluginDatabaseErrorBadge name={plugin.name} />
                        <span
                          className="text-sm text-muted-foreground"
                          data-testid={'plugin-database-error-reason-' + plugin.name}
                        >
                          {plugin.database_error}
                        </span>
                      </div>
                      {plugin.database_error_revision && (
                        <p className="text-sm" data-testid={'plugin-database-error-revision-' + plugin.name}>
                          {t('plugins.database.error_revision', 'Affected revision: {{revision}}', {
                            revision: plugin.database_error_revision,
                          })}
                        </p>
                      )}
                    </>
                  ) : (
                    <>
                      <p className="text-sm" data-testid={'plugin-database-version-table-' + plugin.name}>
                        {t('plugins.database.version_table', 'Version table: {{table}}', {
                          table: plugin.database_version_table,
                        })}
                      </p>
                      <p className="text-sm">{t('plugins.database.up_to_date', 'Schema is up to date.')}</p>
                    </>
                  )}
                </div>
              ) : null}

              <div className="space-y-2">
                <h3 className="text-lg font-semibold">{t('plugins.ui_status.title', 'UI status')}</h3>
                <p className="text-sm">{t(uiStatusKey, uiStatusDefault)}</p>

                {faultEntries.length > 0 && (
                  <div className="max-h-[40vh] space-y-1 overflow-y-auto">
                    {faultEntries.map((entry, index) => (
                      <div key={index} className="text-sm text-destructive whitespace-pre-wrap break-words">
                        <p>{faultReasonText(entry, t)}</p>
                        {entry.kind === 'load' && entry.reason && <p className="text-xs">{entry.reason}</p>}
                      </div>
                    ))}
                  </div>
                )}

                {rejectedEntries.length > 0 && (
                  <div className="space-y-1">
                    <p className="text-xs font-semibold text-muted-foreground">
                      {t('plugins.ui_fault.rejected_slots', 'Rejected slot registrations')}
                    </p>
                    <ul className="list-inside list-disc text-xs text-muted-foreground">
                      {rejectedEntries.map((entry, index) => (
                        <li key={index} className="whitespace-pre-wrap break-words">{entry.slot}</li>
                      ))}
                    </ul>
                  </div>
                )}

                <p className="text-xs text-muted-foreground">
                  {t('plugins.ui_status.capture_hint', 'Already-open forms keep the core editor until they are reopened.')}
                </p>
              </div>

              <div className="space-y-2">
                <h3 className="text-lg font-semibold">{t('plugins.table.health', 'Health check')}</h3>
                {plugin.state !== 'active' ? (
                  <p className="text-sm text-muted-foreground">
                    {t('plugins.health.not_loaded', 'Not loaded — health check unavailable.')}
                  </p>
                ) : (
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      loading={health.isFetching}
                      aria-busy={health.isFetching}
                      disabled={health.isFetching}
                      onClick={(event) => {
                        event.stopPropagation()
                        health.refetch()
                      }}
                      data-testid={'plugin-health-button-' + plugin.name}
                    >
                      {t('plugins.health.run', 'Run health check')}
                    </Button>
                    <div aria-live="polite" data-testid={'plugin-health-result-' + plugin.name}>
                      <HealthResultBlock health={health} />
                    </div>
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p id={killSwitchLabelId} className="text-sm font-medium">{killSwitchLabel}</p>
                    <p className="text-xs text-muted-foreground">{killSwitchHint}</p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={killSwitchActive}
                    aria-labelledby={killSwitchLabelId}
                    aria-disabled={!killSwitchEnabled}
                    disabled={!killSwitchEnabled}
                    onClick={(event) => {
                      event.stopPropagation()
                      handleKillSwitch()
                    }}
                    data-testid={'plugin-kill-switch-' + plugin.name}
                    className={cn(
                      'relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                      killSwitchActive ? 'bg-primary' : 'bg-input',
                      !killSwitchEnabled && 'cursor-not-allowed opacity-50'
                    )}
                  >
                    <span
                      className={cn(
                        'pointer-events-none inline-block h-5 w-5 rounded-full bg-background shadow transform ring-0 transition-transform',
                        killSwitchActive ? 'translate-x-5' : 'translate-x-0'
                      )}
                    />
                  </button>
                </div>
                {killSwitchFailed && (
                  <p className="text-xs text-destructive" aria-live="polite">
                    {t('plugins.kill_switch.failed', 'The status of "{{name}}" could not be changed. Try again.', {
                      name: plugin.name,
                    })}
                  </p>
                )}
              </div>

              <PluginConfigForm plugin={plugin} />
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  )
}

export default function PluginsAdminPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const formatDate = useFormatDate()
  const canManage = useCan('plugins', 'manage')
  const { runtimeStates } = usePluginRuntime()
  const faults = usePluginFaults()

  const q = searchParams.get('q') || ''
  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const { data, isLoading, isError, refetch } = usePlugins()
  const plugins = useMemo(() => (Array.isArray(data) ? data : []), [data])

  const [expandedId, setExpandedId] = useState(null)

  const handleToggleExpand = useCallback((name) => {
    setExpandedId(prev => (prev === name ? null : name))
  }, [])

  const handleRowKeyDown = useCallback((event, name) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      handleToggleExpand(name)
    }
  }, [handleToggleExpand])

  const displayedPlugins = useMemo(() => {
    const term = q.trim().toLowerCase()
    if (!term) return plugins
    return plugins.filter((plugin) => {
      const name = (plugin.name ?? '').toLowerCase()
      const author = (plugin.author ?? '').toLowerCase()
      const apiVersion = (plugin.api_version ?? '').toLowerCase()
      return name.includes(term) || author.includes(term) || apiVersion.includes(term)
    })
  }, [plugins, q])

  return (
    <ErrorBoundary>
      <div className="space-y-6">
        <PageHeader
          title={t('plugins.page.title', 'Plugins')}
          description={t('plugins.page.description', 'Installed plugins, their status and their configuration')}
          icon="puzzle"
        />

        <TableToolbar
          data-testid="plugins-toolbar"
          urlQ={q}
          setQ={setQ}
          placeholder={t('plugins.search.placeholder', 'Search plugins...')}
          ariaLabel={t('plugins.search.accessible_label', 'Search plugins')}
        />

        {isLoading && <TableSkeleton rows={3} cols={6} />}

        {isError && !isLoading && (
          <EmptyState
            variant="error"
            data-testid="plugins-load-error"
            description={t('plugins.load_failed', 'Plugins could not be loaded. Reload plugins.')}
            onButtonClick={() => refetch()}
          />
        )}

        {!isLoading && !isError && plugins.length === 0 && (
          <EmptyState
            variant="empty"
            data-testid="plugins-empty"
            title={t('plugins.empty.title', 'No plugins installed')}
            description={t('plugins.empty.description', 'No plugins were found. Place a plugin in the plugin directory (INDRAMESH_PLUGINS_DIR) or install a plugin package and restart IndraMesh Core.')}
          />
        )}

        {!isLoading && !isError && plugins.length > 0 && displayedPlugins.length === 0 && (
          <EmptyState
            variant="no-results"
            data-testid="plugins-no-results"
            title={t('plugins.no_results.title', 'No matching plugins')}
            description={t('plugins.no_results.description', 'No plugin matches your search. Adjust the search term.')}
          />
        )}

        {!isLoading && !isError && displayedPlugins.length > 0 && (
          <Table data-testid="plugins-table">
            <thead className="sticky top-0 z-10">
              <tr className="border-b bg-background">
                <th scope="col" className="w-8" aria-hidden="true" />
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('plugins.table.version', 'Version')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.status', 'Status')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('plugins.table.worker', 'Worker')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('plugins.table.health', 'Health check')}</th>
                <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.details', 'Details')}</th>
              </tr>
            </thead>
            <tbody>
              {displayedPlugins.map((plugin) => (
                <PluginRow
                  key={plugin.id}
                  plugin={plugin}
                  expanded={expandedId === plugin.name}
                  onToggle={() => handleToggleExpand(plugin.name)}
                  onKeyDown={(event) => handleRowKeyDown(event, plugin.name)}
                  formatDate={formatDate}
                  runtimeState={runtimeStates[plugin.name] ?? 'pending'}
                  faultEntries={faults[plugin.name] || []}
                  canManage={canManage}
                />
              ))}
            </tbody>
          </Table>
        )}
      </div>
    </ErrorBoundary>
  )
}