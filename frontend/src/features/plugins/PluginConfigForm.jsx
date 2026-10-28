import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import { useCan } from '../../shared/components/Can'
import { showToast } from '../../shared/components/Toast'
import { cn } from '../../shared/lib/utils'
import { usePluginConfig, useUpdatePluginConfig } from './use-plugins'
import { buildConfigFields, buildConfigPayload, coerceConfigFieldValue } from './plugin-admin-helpers'

const INPUT_CLASS =
  'w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary'

function initialValue(field, original) {
  if (field.kind === 'password') {
    const value = original[field.key]
    return typeof value === 'string' ? value : ''
  }
  if (field.key in original) return original[field.key]
  if (field.defaultValue !== undefined) return field.defaultValue
  if (field.kind === 'boolean') return false
  return undefined
}

function buildDraft(fields, original) {
  const draft = {}
  fields.forEach((field) => {
    draft[field.key] = initialValue(field, original)
  })
  return draft
}

function normalizeDraft(fields, draft) {
  const normalized = { ...draft }
  fields.forEach((field) => {
    if (field.kind !== 'number' && field.kind !== 'integer') return
    const value = normalized[field.key]
    if (value === '' || value === undefined || value === null) {
      delete normalized[field.key]
      return
    }
    const parsed = field.kind === 'integer' ? Number.parseInt(value, 10) : Number(value)
    if (!Number.isNaN(parsed)) normalized[field.key] = parsed
  })
  return normalized
}

export default function PluginConfigForm({ plugin }) {
  const { t } = useTranslation()
  const fields = useMemo(() => buildConfigFields(plugin.config_schema), [plugin.config_schema])
  const canManage = useCan('plugins', 'manage')

  if (!canManage) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plugin-config-no-permission">
        {t('plugins.config.no_permission', 'You lack the permission (plugins:manage).')}
      </p>
    )
  }

  if (plugin.state !== 'active') {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plugin-config-active-only">
        {t('plugins.config.active_only', 'Configuration is only available for active plugins.')}
      </p>
    )
  }

  if (fields.length === 0) {
    return (
      <div className="space-y-2" data-testid={'plugin-config-form-' + plugin.name}>
        <h3 className="text-lg font-semibold">{t('plugins.config.title', 'Configuration')}</h3>
        <p className="text-sm text-muted-foreground">{t('plugins.config.none', 'This plugin has no configuration fields.')}</p>
      </div>
    )
  }

  return <PluginConfigEditor plugin={plugin} fields={fields} />
}

function PluginConfigEditor({ plugin, fields }) {
  const { t } = useTranslation()
  const { data, isLoading, isError, refetch } = usePluginConfig(plugin.name, { enabled: true })
  const updateConfig = useUpdatePluginConfig()
  const [draft, setDraft] = useState({})
  const [touched, setTouched] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)

  const original = useMemo(() => (data && typeof data === 'object' ? data : {}), [data])

  useEffect(() => {
    if (!touched) setDraft(buildDraft(fields, original))
  }, [fields, original, touched])

  const handleChange = (key, value) => {
    setDraft((prev) => ({ ...prev, [key]: value }))
    setTouched(true)
  }

  const handleSave = () => {
    if (updateConfig.isPending) return
    setSaveFailed(false)
    const payload = buildConfigPayload(fields, normalizeDraft(fields, draft), original)
    updateConfig.mutate(
      { name: plugin.name, config: payload.config },
      {
        onSuccess: () => {
          setSaveFailed(false)
          setTouched(false)
          showToast(t('plugins.config.saved', 'Configuration of "{{name}}" has been saved.', { name: plugin.name }))
        },
        onError: () => setSaveFailed(true),
      }
    )
  }

  const stop = (event) => event.stopPropagation()

  const renderField = (field) => {
    const value = draft[field.key]
    const inputId = `plugin-config-${plugin.name}-${field.key}`
    const label = (
      <label htmlFor={inputId} className="text-sm font-semibold text-foreground">
        {field.key}
        {field.required && <span className="text-destructive"> *</span>}
      </label>
    )

    if (field.kind === 'unsupported') {
      return (
        <div key={field.key} className="space-y-1" data-testid={'plugin-config-unsupported-' + field.key}>
          <span className="text-sm font-semibold text-foreground">{field.key}</span>
          <p className="text-xs text-muted-foreground">
            {t('plugins.config.unsupported', 'This field ({{type}}) is not supported in the UI and can only be set via the API.', {
              type: field.unsupportedType,
            })}
          </p>
        </div>
      )
    }

    if (field.kind === 'boolean') {
      const checked = value === true
      return (
        <div key={field.key} className="space-y-1">
          <div className="flex items-center gap-2">
            <button
              type="button"
              id={inputId}
              role="switch"
              aria-checked={checked}
              onClick={(event) => {
                stop(event)
                handleChange(field.key, !checked)
              }}
              className={cn(
                'relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                checked ? 'bg-primary' : 'bg-input'
              )}
              data-testid={'plugin-config-field-' + field.key}
            >
              <span
                className={cn(
                  'pointer-events-none inline-block h-5 w-5 rounded-full bg-background shadow transform ring-0 transition-transform',
                  checked ? 'translate-x-5' : 'translate-x-0'
                )}
              />
            </button>
            {label}
          </div>
        </div>
      )
    }

    return (
      <div key={field.key} className="space-y-1">
        {label}
        {field.kind === 'enum' ? (
          <select
            id={inputId}
            value={value ?? ''}
            onChange={(event) => handleChange(field.key, coerceConfigFieldValue(field, event.target.value))}
            className={INPUT_CLASS}
            data-testid={'plugin-config-field-' + field.key}
          >
            {!field.required && <option value="">—</option>}
            {field.options.map((option) => (
              <option key={String(option.value)} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={inputId}
            type={field.kind === 'password' ? 'password' : field.kind === 'string' ? 'text' : 'number'}
            step={field.kind === 'integer' ? '1' : field.kind === 'number' ? 'any' : undefined}
            value={value ?? ''}
            onChange={(event) => handleChange(field.key, event.target.value)}
            className={INPUT_CLASS}
            data-testid={'plugin-config-field-' + field.key}
          />
        )}
        {field.kind === 'password' && value && (
          <p className="text-xs text-muted-foreground">{t('plugins.config.secret_set', 'Set — leave unchanged to keep the value.')}</p>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-3 rounded-md border p-4" data-testid={'plugin-config-form-' + plugin.name}>
      <h3 className="text-lg font-semibold">{t('plugins.config.title', 'Configuration')}</h3>

      {isError ? (
        <div className="flex flex-wrap items-center gap-3" role="alert">
          <span className="text-xs text-destructive">
            {t('plugins.config.load_failed', 'Configuration could not be loaded. Reload configuration.')}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={(event) => {
              stop(event)
              refetch()
            }}
            data-testid={'plugin-config-load-retry-' + plugin.name}
          >
            {t('plugins.config.load_retry', 'Reload configuration')}
          </Button>
        </div>
      ) : (
        <>
          <div className={cn('space-y-3', isLoading && 'opacity-70')}>{fields.map(renderField)}</div>

          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              size="sm"
              onClick={(event) => {
                stop(event)
                handleSave()
              }}
              disabled={updateConfig.isPending || !touched}
              data-testid={'plugin-config-save-' + plugin.name}
            >
              {updateConfig.isPending
                ? t('plugins.config.saving', 'Saving configuration...')
                : t('plugins.config.save', 'Save configuration')}
            </Button>

            {saveFailed && (
              <div className="flex items-center gap-2" aria-live="polite">
                <span className="text-xs text-destructive">
                  {t('plugins.config.save_failed', 'Configuration could not be saved. Save configuration again.')}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={(event) => {
                    stop(event)
                    handleSave()
                  }}
                  data-testid={'plugin-config-save-retry-' + plugin.name}
                >
                  {t('plugins.config.save_retry', 'Save configuration again')}
                </Button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}