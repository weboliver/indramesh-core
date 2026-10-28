import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import EmptyState from '../../shared/components/EmptyState'
import LoadingSpinner from '../../shared/components/LoadingSpinner'
import { Button } from '../../shared/components/Button'
import { usePluginPages, usePluginRuntime, usePluginRuntimeList } from '../../shared/plugin-runtime/PluginProvider'

function normalizePath(value) {
  return (value || '').replace(/^\/+|\/+$/g, '')
}

function PluginPageFallback({ testId, title, description, retryLabel, onRetry, searchLabel }) {
  return (
    <div>
      <EmptyState
        variant="error"
        title={title}
        description={description}
        data-testid={testId}
      />
      <div className="mt-4 flex justify-center gap-2">
        {onRetry && retryLabel ? (
          <Button onClick={onRetry} variant="primary" size="md">
            {retryLabel}
          </Button>
        ) : null}
        {searchLabel ? (
          <Button asChild variant="outline" size="md">
            <Link to="/search">{searchLabel}</Link>
          </Button>
        ) : null}
      </div>
    </div>
  )
}

export default function PluginRouteResolver() {
  const params = useParams()
  const pluginName = params.pluginName
  const restPath = normalizePath(params['*'])
  const pages = usePluginPages()
  const { runtimeStates, contributions, retry, reportFault, clearFaults } = usePluginRuntime()
  const { data: runtimePlugins, isPending: runtimeListPending } = usePluginRuntimeList()
  const { t } = useTranslation()
  const [attempt, setAttempt] = useState(0)

  const fallbackTestId = `plugin-page-fallback-${pluginName}`
  const runtimeState = runtimeStates[pluginName]
  const pluginPages = pages.filter((entry) => entry.pluginName === pluginName)
  const requestedPath = restPath || normalizePath(pluginPages[0]?.path)
  const page = pluginPages.find((entry) => normalizePath(entry.path) === requestedPath)
  const searchLabel = t('plugins.page.to_search', 'To search')

  // The provider writes `runtimeStates[name]` asynchronously: after the runtime
  // discovery list resolves, an active plugin with a bundle is flipped to
  // `loading` before its bundle settles. While that state is still unknown —
  // and a state is actually coming (the runtime list is in flight, or the
  // plugin is listed with a bundle) — render the page-level spinner. The
  // runtime list is the only source a `plugin-runtime:use` caller may read:
  // the admin `plugins/` list answers 403 for the seeded Editor, which
  // previously flashed the not-found card and added three refused requests per
  // cold deep-link (285-UI-REVIEW F6.1; 289-UI-REVIEW F6).
  const listedPlugin = (Array.isArray(runtimePlugins) ? runtimePlugins : []).find(
    (plugin) => plugin.name === pluginName
  )
  const runtimeStatePending =
    runtimeState === undefined &&
    (runtimeListPending || Boolean(listedPlugin?.frontend?.bundle))

  if (runtimeStatePending || runtimeState === 'pending' || runtimeState === 'loading') {
    return <LoadingSpinner />
  }

  if (runtimeState === 'ui_fault') {
    return (
      <PluginPageFallback
        testId={fallbackTestId}
        title={t('plugins.page.load_failed_title', 'Plugin UI could not be loaded')}
        description={t('plugins.page.load_failed', 'The UI bundle of "{{name}}" could not be loaded.', { name: pluginName })}
        retryLabel={t('plugins.page.retry', 'Try again')}
        onRetry={() => retry(pluginName)}
        searchLabel={searchLabel}
      />
    )
  }

  if (!page) {
    const isNotActive = pluginPages.length === 0 &&
      (contributions.pages || []).some((entry) => entry.pluginName === pluginName)
    return (
      <PluginPageFallback
        testId={fallbackTestId}
        title={t('plugins.page.not_found_title', 'Plugin page not found')}
        description={isNotActive
          ? t('plugins.page.not_active', 'This plugin is not installed or not active.')
          : t('plugins.page.not_found', '"{{path}}" does not belong to an installed, active plugin.', { path: requestedPath || pluginName })}
        searchLabel={searchLabel}
      />
    )
  }

  const PageComponent = page.component
  return (
    <ErrorBoundary
      key={`${pluginName}:${requestedPath}:${attempt}`}
      onError={(error) =>
        reportFault(pluginName, {
          kind: 'slot_render',
          slot: `page:${requestedPath}`,
          reason: error?.message || 'render error',
        })
      }
      fallback={
        <PluginPageFallback
          testId={fallbackTestId}
          description={t('plugins.ui_fault.slot_render', 'A contribution failed while rendering ({{slot}}).', {
            slot: `${pluginName}/${requestedPath}`,
          })}
          retryLabel={t('plugins.page.retry', 'Try again')}
          onRetry={() => {
            // WR-08: the retry supersedes this render fault; a fresh failure
            // re-reports it through the boundary's onError.
            clearFaults(pluginName)
            setAttempt((value) => value + 1)
          }}
          searchLabel={searchLabel}
        />
      }
    >
      <PageComponent />
    </ErrorBoundary>
  )
}