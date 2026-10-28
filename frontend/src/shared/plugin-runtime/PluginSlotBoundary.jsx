import { useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { Button } from '../components/Button'
import { usePluginRuntime } from './PluginProvider'

// ErrorBoundary treats a falsy `fallback` as "render the default EmptyState",
// so a null slot fallback (action button: the button is omitted) is mapped to
// an empty fragment before it reaches the boundary.
const EMPTY_FALLBACK = <></>

export function PluginSlotBoundary({ slot, pluginName, fallback = null, onFault, onError, children }) {
  const { reportFault } = usePluginRuntime()
  const { t } = useTranslation()

  const reportSlotFault = useCallback(
    (error) => {
      if (!pluginName) return
      const copy = t('plugins.ui_fault.slot_render', 'A contribution failed while rendering ({{slot}}).', { slot })
      const detail = error?.message || (error ? String(error) : '')
      reportFault(pluginName, {
        kind: 'slot_render',
        slot,
        reason: detail ? `${copy}: ${detail}` : copy,
      })
    },
    [reportFault, pluginName, slot, t]
  )

  // `onFault` keeps its replace semantics for existing callers; `onError` is
  // additive and always runs after the fault is reported, so a host can react
  // to a crashed contribution without suppressing fault reporting.
  const handleError = useCallback(
    (error) => {
      ;(onFault ?? reportSlotFault)(error)
      onError?.(error)
    },
    [onFault, reportSlotFault, onError]
  )

  return (
    <ErrorBoundary fallback={fallback ?? EMPTY_FALLBACK} onError={handleError}>
      {children}
    </ErrorBoundary>
  )
}

export function PluginSlotErrorRow({ slot }) {
  const { t } = useTranslation()

  return (
    <p role="status" data-testid={'plugin-slot-error-' + slot} className="text-sm text-muted-foreground">
      {t('plugins.slot.failed', 'Plugin content could not be loaded.')}
    </p>
  )
}

// D-16: a compact, visible local fallback for the shared action families. It
// names the resolved label and the host-stamped owner, explains the failure and
// offers an outline retry — never an empty default, never a plugin-wide kill
// switch. Sibling contributions and Core controls stay usable.
function PluginContributionError({ slot, label, owner, onRetry }) {
  const { t } = useTranslation()
  const message = t(
    'plugins.seams.contribution.failed',
    '{{label}} from plugin {{plugin}} could not be loaded. Try again or continue with the Core controls.',
    { label: label ?? '', plugin: owner ?? '' }
  )

  return (
    <div
      role="status"
      data-testid={'plugin-contribution-error-' + slot}
      className="min-w-0 space-y-2 rounded-md border border-input p-4 text-sm text-muted-foreground"
    >
      <p className="min-w-0 break-words">{message}</p>
      {onRetry ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
          onClick={onRetry}
        >
          {t('plugins.seams.contribution.retry', 'Try again')}
        </Button>
      ) : null}
    </div>
  )
}

// One keyed local boundary per contribution. A render error is caught here and
// reported through the existing fault path; the visible fallback replaces the
// old empty default so a failed contribution is never silently absent.
export function PluginContributionBoundary({
  slot,
  pluginName,
  label,
  owner,
  onRetry,
  fallback,
  onError,
  children,
}) {
  return (
    <PluginSlotBoundary
      slot={slot}
      pluginName={pluginName}
      onError={onError}
      fallback={
        fallback ?? (
          <PluginContributionError
            slot={slot}
            label={label}
            owner={owner ?? pluginName}
            onRetry={onRetry}
          />
        )
      }
    >
      {children}
    </PluginSlotBoundary>
  )
}