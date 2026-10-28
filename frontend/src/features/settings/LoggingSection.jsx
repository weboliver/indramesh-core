import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useLogLevels, useUpdateLogLevel } from '../../shared/hooks/use-other-resources'
import { useCan } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { extractErrorMessage } from '../../shared/lib/utils'
import {
  LOG_LEVEL_NAMES,
  DEFAULT_EXPIRY_MINUTES,
  buildLevelUpdate,
  parseExpiryMinutes,
  remainingDuration,
  formatRemaining,
  expiryRefetchKey,
} from './logging-level-state'

// The six core categories always exist and always render first, in the frozen
// 298 order (CONTEXT D-01/D-10). Dynamic `connector.*`/`plugin.*` categories
// follow in collapsible groups.
const CORE_CATEGORY_ORDER = ['app', 'access', 'security', 'task', 'db', 'llm']

export default function LoggingSection() {
  const { t } = useTranslation()
  const { data, isLoading, isError, error, refetch, dataUpdatedAt } = useLogLevels()
  // One refetch per category+expiry+server-snapshot (survives row re-renders
  // and stops an identical expired response from looping).
  const expiryGuardRef = useRef(new Set())
  const [now, setNow] = useState(() => Date.now())

  const categories = data?.categories || []
  const serverTime = data?.server_time || null

  // A 1s tick runs only while a displayed below-threshold override still has
  // future time left; the interval is torn down on unmount or last expiry.
  const hasFutureExpiry = categories.some(
    (row) => row.source === 'override' && row.expires_at && row.remaining_seconds !== null && row.remaining_seconds !== undefined && row.remaining_seconds > 0
  )
  useEffect(() => {
    if (!hasFutureExpiry) return undefined
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [hasFutureExpiry, dataUpdatedAt])

  const coreRows = CORE_CATEGORY_ORDER
    .map((category) => categories.find((row) => row.category === category))
    .filter(Boolean)
  const dynamicRows = categories.filter((row) => !CORE_CATEGORY_ORDER.includes(row.category))
  const byCategory = (a, b) => a.category.localeCompare(b.category)
  const connectorRows = dynamicRows.filter((row) => row.category.startsWith('connector.')).sort(byCategory)
  const pluginRows = dynamicRows.filter((row) => row.category.startsWith('plugin.')).sort(byCategory)

  const rowProps = {
    serverTime,
    dataUpdatedAt,
    now,
    expiryGuard: expiryGuardRef.current,
    onExpire: refetch,
  }

  return (
    <div className="rounded-lg border p-6 space-y-4" data-testid="logging-section">
      <div className="flex items-center gap-2">
        <LucideIcon name="gauge" size={18} className="text-muted-foreground" />
        <h2 className="text-lg font-semibold">{t('settings.logging.title', 'Logging')}</h2>
      </div>
      <p className="text-xs text-muted-foreground">
        {t('settings.logging.description', 'Set how much detail each log category writes. Changes apply to every process within a few seconds.')}
      </p>

      {isLoading && !data && (
        <div className="space-y-1" data-testid="logging-skeleton">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="h-8 rounded bg-muted/30 animate-shimmer" />
          ))}
        </div>
      )}

      {isError && !data && (
        <div
          className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
          role="alert"
          data-testid="logging-load-error"
        >
          {t('settings.logging.load_error', "Couldn't load the log levels: {{message}}.", {
            message: extractErrorMessage(error),
          })}{' '}
          <Button variant="link" className="p-0 h-auto" onClick={() => refetch()}>
            {t('common.retry', 'Retry')}
          </Button>
        </div>
      )}

      {categories.length > 0 && (
        <div className="space-y-1">
          {coreRows.map((row) => (
            <LoggingLevelRow key={row.category} row={row} {...rowProps} />
          ))}
        </div>
      )}

      {connectorRows.length > 0 && (
        <LoggingCategoryGroup
          name="connectors"
          label={t('settings.logging.group_connectors', 'Connectors')}
          rows={connectorRows}
          rowProps={rowProps}
        />
      )}

      {pluginRows.length > 0 && (
        <LoggingCategoryGroup
          name="plugins"
          label={t('settings.logging.group_plugins', 'Plugins')}
          rows={pluginRows}
          rowProps={rowProps}
        />
      )}
    </div>
  )
}

function LoggingCategoryGroup({ name, label, rows, rowProps }) {
  const hasActiveOverride = rows.some((row) => row.source === 'override')
  const [open, setOpen] = useState(hasActiveOverride)

  // Fail-visible (LVL-04): an override that appears while collapsed (a server
  // update) auto-expands the group so a live debug override can never hide.
  useEffect(() => {
    if (hasActiveOverride) setOpen(true)
  }, [hasActiveOverride])

  return (
    <div className="pt-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-2 w-full py-2 text-left text-sm font-semibold hover:bg-accent/50 rounded-md"
        aria-expanded={open}
        aria-controls={`logging-group-${name}-content`}
        data-testid={`logging-group-${name}`}
      >
        <LucideIcon name={open ? 'chevron-down' : 'chevron-right'} size={16} aria-hidden="true" />
        <span>{label}</span>
        <span className="text-xs text-muted-foreground font-normal tabular-nums">({rows.length})</span>
      </button>
      {open && (
        <div id={`logging-group-${name}-content`} className="space-y-1">
          {rows.map((row) => (
            <LoggingLevelRow key={row.category} row={row} {...rowProps} />
          ))}
        </div>
      )}
    </div>
  )
}

function LoggingLevelRow({ row, serverTime, dataUpdatedAt, now, expiryGuard, onExpire }) {
  const { t } = useTranslation()
  const canUpdate = useCan('settings', 'update')
  const update = useUpdateLogLevel()
  const selectRef = useRef(null)

  const [draft, setDraft] = useState(null) // { level, minutes } while a below-threshold draft is open
  const [draftFailed, setDraftFailed] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState(null)

  const pending = update.isPending
  const persisted = row.source === 'override' && row.override_level ? row.override_level : row.effective_level
  const selectValue = draft && !draftFailed ? draft.level : persisted
  const draftInvalid = draft !== null && parseExpiryMinutes(draft.minutes) === null
  const sourceLabel = row.source === 'override'
    ? t('settings.logging.source_override', 'override')
    : t('settings.logging.source_env', 'environment')

  // Countdown is calibrated from the server's remaining_seconds (D-07); the
  // displayed value never recomputes from a client wall clock. When it reaches
  // zero the row refetches exactly once per expiry snapshot and keeps showing
  // the last state until the authoritative fresh answer arrives.
  const remaining = remainingDuration({
    remainingSeconds: row.remaining_seconds,
    loadedAt: dataUpdatedAt,
    now,
  })
  const hasOverrideExpiry = row.source === 'override' && Boolean(row.expires_at) && remaining !== null
  const isLiveExpiry = hasOverrideExpiry && remaining > 0
  const isExpired = hasOverrideExpiry && remaining <= 0

  useEffect(() => {
    if (!isExpired) return undefined
    const key = expiryRefetchKey(row.category, row.expires_at, serverTime)
    if (expiryGuard.has(key)) return undefined
    expiryGuard.add(key)
    onExpire()
    return undefined
  }, [isExpired, row.category, row.expires_at, serverTime, expiryGuard, onExpire])

  const send = (value, { onSuccess, onError } = {}) => {
    setSaveError(null)
    setSaved(false)
    update.mutate(
      { category: row.category, value },
      {
        onSuccess: () => {
          setSaved(true)
          if (onSuccess) onSuccess()
        },
        onError: (err) => {
          // Replaced, never stacked: the latest attempt's message wins.
          setSaveError(extractErrorMessage(err))
          setSaved(false)
          if (onError) onError(err)
        },
      }
    )
  }

  const handleSelectChange = (level) => {
    const { action, value } = buildLevelUpdate({
      level,
      baseLevel: row.base_level,
      ttlRequiredBelow: row.ttl_required_below,
    })
    if (action === 'clear') {
      setDraft(null)
      setDraftFailed(false)
      send(null)
      return
    }
    if (action === 'draft') {
      setSaveError(null)
      setSaved(false)
      setDraft({ level, minutes: String(DEFAULT_EXPIRY_MINUTES) })
      setDraftFailed(false)
      return
    }
    setDraft(null)
    setDraftFailed(false)
    send(value)
  }

  const handleApply = () => {
    if (!draft) return
    const { action, value } = buildLevelUpdate({
      level: draft.level,
      baseLevel: row.base_level,
      ttlRequiredBelow: row.ttl_required_below,
      minutes: draft.minutes,
    })
    if (action !== 'update') return
    send(value, {
      onSuccess: () => {
        setDraft(null)
        setDraftFailed(false)
      },
      // A below-threshold failure keeps the draft for retry (UI-SPEC); the
      // select reverts to the persisted value while the draft stays open.
      onError: () => setDraftFailed(true),
    })
  }

  const cancelDraft = () => {
    setDraft(null)
    setDraftFailed(false)
    setSaveError(null)
    selectRef.current?.focus()
  }

  const handleReset = () => {
    setDraft(null)
    setDraftFailed(false)
    send(null)
  }

  const handleKeyDown = (event) => {
    if (event.key === 'Escape' && draft) {
      event.preventDefault()
      cancelDraft()
    }
  }

  const countdown = isLiveExpiry ? (
    <p
      className="text-xs text-amber-600 dark:text-amber-500 flex items-center gap-1 tabular-nums"
      role="timer"
      data-testid={`logging-countdown-${row.category}`}
    >
      <LucideIcon name="timer" size={12} aria-hidden="true" />
      {t('settings.logging.countdown', 'Resets in {{duration}}', {
        duration: formatRemaining(remaining),
      })}
    </p>
  ) : null

  const showControlCluster = canUpdate || countdown

  return (
    <div className="py-1.5" data-testid={`logging-level-row-${row.category}`} onKeyDown={handleKeyDown}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <span className="font-mono text-sm text-foreground">{row.category}</span>
          <p className="text-xs text-muted-foreground">
            {t('settings.logging.effective', 'Effective: {{level}} ({{source}})', {
              level: row.effective_level,
              source: sourceLabel,
            })}
          </p>
        </div>

        {showControlCluster && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {canUpdate && (
              <select
                ref={selectRef}
                value={selectValue}
                onChange={(event) => handleSelectChange(event.target.value)}
                disabled={pending}
                aria-label={t('settings.logging.level_label', 'Level for {{category}}', { category: row.category })}
                data-testid={`logging-level-select-${row.category}`}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-36 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {LOG_LEVEL_NAMES.map((level) => (
                  <option key={level} value={level}>{level}</option>
                ))}
              </select>
            )}

            {canUpdate && row.source === 'override' && (
              <Button
                variant="outline"
                size="sm"
                leftIcon="rotate-ccw"
                loading={pending}
                onClick={handleReset}
                data-testid={`logging-reset-${row.category}`}
              >
                {t('settings.logging.reset', 'Reset')}
              </Button>
            )}

            {canUpdate && pending && (
              <span
                className="text-xs text-muted-foreground"
                role="status"
                aria-live="polite"
                data-testid={`logging-level-saving-${row.category}`}
              >
                {t('common.saving', 'Saving...')}
              </span>
            )}

            {canUpdate && !pending && saved && (
              <span
                className="text-xs text-green-700 dark:text-green-400 flex items-center gap-1"
                role="status"
                aria-live="polite"
                data-testid={`logging-level-saved-${row.category}`}
              >
                <LucideIcon name="circle-check" size={12} aria-hidden="true" />
                {t('settings.logging.saved', 'Saved')}
              </span>
            )}

            {countdown}
          </div>
        )}
      </div>

      {canUpdate && draft && (
        <div className="mt-2" data-testid={`logging-ttl-${row.category}`}>
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm font-medium" htmlFor={`logging-ttl-input-${row.category}`}>
              {t('settings.logging.expires_label', 'Expires after (minutes)')}
            </label>
            <input
              id={`logging-ttl-input-${row.category}`}
              type="number"
              min="1"
              max="1440"
              value={draft.minutes}
              onChange={(event) => setDraft({ ...draft, minutes: event.target.value })}
              aria-invalid={draftInvalid}
              aria-describedby={`logging-ttl-help-${row.category}`}
              data-testid={`logging-ttl-input-${row.category}`}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-24 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button
              variant="outline"
              size="sm"
              disabled={draftInvalid || pending}
              onClick={handleApply}
              data-testid={`logging-apply-${row.category}`}
            >
              {t('settings.logging.apply', 'Apply level')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={cancelDraft}
              data-testid={`logging-cancel-${row.category}`}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
          </div>
          <p id={`logging-ttl-help-${row.category}`} className="text-xs text-muted-foreground">
            {t(
              'settings.logging.expires_help',
              'This level is more verbose than the base ({{base}}), so it needs an expiry. It reverts to the base automatically when the expiry elapses.',
              { base: row.ttl_required_below }
            )}
          </p>
          {draftInvalid && (
            <p className="text-xs text-destructive" role="alert" data-testid={`logging-ttl-validation-${row.category}`}>
              {t('settings.logging.expires_validation', 'Enter a whole number of minutes between 1 and 1440 (24 hours).')}
            </p>
          )}
        </div>
      )}

      {saveError && (
        <p
          className="text-xs text-destructive break-words"
          role="alert"
          data-testid={`logging-level-error-${row.category}`}
        >
          {t('settings.logging.save_error', "Couldn't save the level for {{category}}: {{message}}. The previous setting still applies.", {
            category: row.category,
            message: saveError,
          })}
        </p>
      )}
    </div>
  )
}
