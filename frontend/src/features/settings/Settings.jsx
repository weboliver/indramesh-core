import { useEffect, useState } from 'react'
import { useSystemSettings, useUpdateSetting } from '../../shared/hooks/use-other-resources'
import PageHeader from '../../shared/components/PageHeader'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { Can } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import { usePluginSettingsSections } from '../../shared/plugin-runtime/PluginProvider'
import { PluginSlotBoundary, PluginSlotErrorRow } from '../../shared/plugin-runtime/PluginSlotBoundary'
import LoggingSection from './LoggingSection'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import Globe from 'lucide-react/dist/esm/icons/globe'
import KeyRound from 'lucide-react/dist/esm/icons/key-round'
import { usePasswordPolicy } from '../../shared/hooks/use-password-policy'
import { PASSWORD_MAX_BYTES, PASSWORD_MIN_LENGTH_FLOOR } from '../../shared/lib/password-policy'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import History from 'lucide-react/dist/esm/icons/history'


// D-06: the per-file maximum defaults to 50 MB until an administrator saves
// an explicit value; the 279-04 backend contract persists only whole
// positive MB values as canonical integer strings.
const DEFAULT_MAX_SIZE_MB = 50

// WR-03: mirrors the backend's OBJECT_FILE_MAX_SIZE_MB_CAP
// (backend/app/api/settings.py) so an implausible value is rejected in the
// UI before it ever reaches the API.
const MAX_SIZE_MB_CAP = 2048


function parseWholePositiveMb(value) {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const parsed = Number(trimmed)
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= MAX_SIZE_MB_CAP ? parsed : null
}


const TIMEZONES = [
  'UTC',
  'Europe/Berlin','Europe/Vienna','Europe/Zurich',
  'Europe/Paris','Europe/Amsterdam','Europe/Brussels',
  'Europe/London','Europe/Dublin',
  'Europe/Madrid','Europe/Rome','Europe/Lisbon',
  'Europe/Stockholm','Europe/Oslo','Europe/Copenhagen','Europe/Helsinki',
  'Europe/Warsaw','Europe/Prague','Europe/Budapest','Europe/Bucharest',
  'Europe/Athens','Europe/Istanbul','Europe/Moscow',
  'America/New_York','America/Chicago','America/Denver','America/Los_Angeles',
  'America/Toronto','America/Vancouver',
  'America/Mexico_City','America/Bogota','America/Sao_Paulo','America/Argentina/Buenos_Aires',
  'America/Santiago','America/Lima',
  'Asia/Dubai','Asia/Kolkata','Asia/Bangkok','Asia/Singapore','Asia/Hong_Kong',
  'Asia/Shanghai','Asia/Tokyo','Asia/Seoul','Asia/Manila',
  'Australia/Sydney','Australia/Melbourne','Australia/Perth',
  'Pacific/Auckland',
]


export default function SettingsPage() {
  const { t } = useTranslation()

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      <PageHeader
        title={t('settings.page.title', 'Settings')}
        description={t('settings.page.description', 'System settings')}
        icon="cog"
      />

      <SystemSettingsSection />
      <Can resource="settings" action="update">
        <PasswordPolicySection />
      </Can>
      <Can resource="audit-log-config" action="update">
        <HistorySection />
      </Can>
      <Can resource="settings" action="read">
        <LoggingSection />
      </Can>
      <PluginSettingsSections />
    </div>
    </ErrorBoundary>
  )
}


function PluginSettingsSections() {
  const sections = usePluginSettingsSections()

  return (
    <>
      {sections.map((entry) => (
        <PluginSettingsSection key={entry.id} entry={entry} />
      ))}
    </>
  )
}


function PluginSettingsSection({ entry }) {
  const slot = 'settingsSection:' + entry.id
  const SectionComponent = entry.component

  return (
    <PluginSlotBoundary
      slot={slot}
      pluginName={entry.pluginName}
      fallback={<PluginSlotErrorRow slot={slot} />}
    >
      <SectionComponent />
    </PluginSlotBoundary>
  )
}


const POLICY_FLAGS = [
  ['require_digit', 'settings.password_policy.require_digit', 'Digit required'],
  ['require_uppercase', 'settings.password_policy.require_uppercase', 'Uppercase letter required'],
  ['require_special', 'settings.password_policy.require_special', 'Special character required'],
]


// Saved as one validated value (key password_policy); the backend checks it
// when an administrator creates a user or a superuser resets a password, and
// when a user changes their own password.
function PasswordPolicySection() {
  const { t } = useTranslation()
  const policy = usePasswordPolicy()
  const updateSetting = useUpdateSetting()
  const [draft, setDraft] = useState(policy)
  const [touched, setTouched] = useState(false)
  const [result, setResult] = useState(null)

  useEffect(() => {
    if (!touched) setDraft(policy)
  }, [policy, touched])

  const minLength = Number(draft.min_length)
  const maxLength = Number(draft.max_length)
  const invalid = !Number.isInteger(minLength) || !Number.isInteger(maxLength)
    || minLength < PASSWORD_MIN_LENGTH_FLOOR || maxLength > PASSWORD_MAX_BYTES || minLength > maxLength

  const change = (field, value) => {
    setDraft((prev) => ({ ...prev, [field]: value }))
    setTouched(true)
    setResult(null)
  }

  const save = () => {
    updateSetting.mutate(
      { key: 'password_policy', value: { ...draft, min_length: minLength, max_length: maxLength } },
      {
        onSuccess: () => { setTouched(false); setResult('saved') },
        onError: () => setResult('failed'),
      }
    )
  }

  return (
    <div className="rounded-lg border p-6 space-y-4" data-testid="password-policy-section">
      <div className="flex items-center gap-2">
        <KeyRound size={18} className="text-muted-foreground" />
        <h2 className="text-lg font-semibold">{t('settings.password_policy.title', 'Password policy')}</h2>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        {[
          ['min_length', 'settings.password_policy.min_length', 'Minimum length'],
          ['max_length', 'settings.password_policy.max_length', 'Maximum length'],
        ].map(([field, key, fallback]) => (
          <label key={field} className="flex items-center gap-2 text-sm font-medium text-foreground">
            {t(key, fallback)}
            <input
              type="number"
              min={PASSWORD_MIN_LENGTH_FLOOR}
              max={PASSWORD_MAX_BYTES}
              value={draft[field]}
              onChange={(e) => change(field, e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-20"
              data-testid={`password-policy-${field}`}
            />
          </label>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-4">
        {POLICY_FLAGS.map(([field, key, fallback]) => (
          <label key={field} className="flex items-center gap-1.5 text-sm font-normal">
            <input
              type="checkbox"
              checked={!!draft[field]}
              onChange={(e) => change(field, e.target.checked)}
              data-testid={`password-policy-${field}`}
            />
            {t(key, fallback)}
          </label>
        ))}
      </div>

      <p className={cn('text-xs', invalid ? 'text-destructive' : 'text-muted-foreground')}>
        {interpolateDefault(
          t('settings.password_policy.bounds_hint', 'Lengths between {{floor}} and {{cap}} characters, minimum not above maximum. bcrypt reads at most {{cap}} bytes, so longer passwords would not add strength.'),
          { floor: PASSWORD_MIN_LENGTH_FLOOR, cap: PASSWORD_MAX_BYTES }
        )}
      </p>

      <div className="flex items-center gap-3">
        <Button
          type="button"
          size="sm"
          onClick={save}
          disabled={updateSetting.isPending || invalid || !touched}
          loading={updateSetting.isPending}
          data-testid="password-policy-save"
        >
          {t('settings.password_policy.save', 'Save password policy')}
        </Button>
        {result === 'saved' && (
          <span className="text-sm text-green-600" role="status">
            {t('settings.password_policy.saved', 'Password policy saved.')}
          </span>
        )}
        {result === 'failed' && (
          <span className="text-sm text-destructive" role="alert">
            {t('settings.password_policy.save_failed', 'The password policy could not be saved.')}
          </span>
        )}
      </div>
    </div>
  )
}


function HistorySection() {
  const { t } = useTranslation()
  const { data: settings = {} } = useSystemSettings()
  const updateSetting = useUpdateSetting()

  const historyEnabled = settings.history_enabled === 'true'

  return (
    <div className="rounded-lg border p-6 space-y-4">
      <div className="flex items-center gap-2">
        <History size={18} className="text-muted-foreground" />
         <h2 className="text-lg font-semibold">{t('settings.history.title', 'History')}</h2>
      </div>

      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">
             {historyEnabled ? t('common.status.active', 'Active') : t('common.status.inactive', 'Inactive')}
          </p>
          <p className="text-xs text-muted-foreground max-w-lg">
             {t('settings.history.description', 'Activates audit logging for Objects, Object Values, and Object Refs together, and locks those three toggles on. While History is active, they can only be switched off again by deactivating History here.')}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={historyEnabled}
          onClick={() => {
            if (!updateSetting.isPending) {
              updateSetting.mutate({ key: 'history_enabled', value: historyEnabled ? 'false' : 'true' })
            }
          }}
          disabled={updateSetting.isPending}
          className={cn(
            'relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            historyEnabled ? 'bg-primary' : 'bg-input'
          )}
        >
          <span
            className={cn(
              'pointer-events-none inline-block h-5 w-5 rounded-full bg-background shadow transform ring-0 transition-transform',
              historyEnabled ? 'translate-x-5' : 'translate-x-0'
            )}
          />
        </button>
      </div>
    </div>
  )
}


function SystemSettingsSection() {
  const { t } = useTranslation()
  const { data: settings = {}, isError, refetch } = useSystemSettings()
  const updateSetting = useUpdateSetting()

  const defaultTimezone = settings.default_timezone || 'UTC'

  return (
    <div className="rounded-lg border p-6 space-y-4">
      <div className="flex items-center gap-2">
        <Globe size={18} className="text-muted-foreground" />
         <h2 className="text-lg font-semibold">{t('settings.system.title', 'System Settings')}</h2>
      </div>

      <div className="flex items-center gap-4">
         <label className="text-sm font-medium text-foreground whitespace-nowrap">{t('settings.system.default_timezone', 'Default Timezone')}</label>
        <select
          value={defaultTimezone}
          onChange={(e) => updateSetting.mutate({ key: 'default_timezone', value: e.target.value })}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-60"
        >
          {TIMEZONES.map((tz) => (
            <option key={tz} value={tz}>{tz}</option>
          ))}
        </select>
         {updateSetting.isPending && <span className="text-xs text-muted-foreground">{t('common.saving', 'Saving...')}</span>}
      </div>
      <p className="text-xs text-muted-foreground">
         {t('settings.system.timezone_hint', 'Used as default timezone for new schedules.')}
      </p>

      <ObjectFileLimitCard
        settings={settings}
        isSettingsError={isError}
        onReloadSettings={() => refetch()}
      />
    </div>
  )
}


// D-06/D-07/D-10 (T-279-15/T-279-16): the administrator maximum-size card.
// Absent configuration renders the 50 default; the user edits a local draft
// that only an explicit save sends through the existing useUpdateSetting
// contract (key object_file_max_size_mb, canonical whole positive MB).
// Invalid drafts never reach the mutation, pending disables the save action,
// and load/save failures stay local to this card with their retry actions.
// Reopened panels/settings queries observe a changed limit on their normal
// reload — no live push into already-open panels. Every visible string
// resolves through the 279-06 settings.system.object_file.* keys.
function ObjectFileLimitCard({ settings, isSettingsError, onReloadSettings }) {
  const { t } = useTranslation()
  const updateSetting = useUpdateSetting()
  const persisted = parseWholePositiveMb(settings.object_file_max_size_mb)
  const effectiveValue = persisted !== null ? String(persisted) : String(DEFAULT_MAX_SIZE_MB)

  const [draft, setDraft] = useState(effectiveValue)
  const [touched, setTouched] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  const [failedValue, setFailedValue] = useState(null)

  // D-10: re-sync the local draft whenever a loaded setting arrives and the
  // user is not mid-edit — after a successful save the query invalidation
  // feeds the canonical integer back into the field.
  useEffect(() => {
    if (!touched) setDraft(effectiveValue)
  }, [effectiveValue, touched])

  const draftMb = parseWholePositiveMb(draft)
  const draftInvalid = draftMb === null

  const save = (value) => {
    const mb = parseWholePositiveMb(value)
    if (mb === null) return
    setSaveFailed(false)
    setFailedValue(null)
    updateSetting.mutate(
      { key: 'object_file_max_size_mb', value: mb },
      {
        onSuccess: () => {
          setSaveFailed(false)
          setFailedValue(null)
          setTouched(false)
        },
        onError: () => {
          // Retain the failed attempt locally with its own retry (D-16).
          setSaveFailed(true)
          setFailedValue(String(mb))
        },
      }
    )
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-4">
        <label htmlFor="settings-max-size-input" className="text-sm font-medium text-foreground whitespace-nowrap">
          {t('settings.system.object_file.max_size_label', 'Maximum file size per file (MB)')}
        </label>
        <input
          id="settings-max-size-input"
          type="text"
          inputMode="numeric"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            setTouched(true)
          }}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground w-28"
          data-testid="settings-max-size-input"
        />
        {updateSetting.isPending && (
          <span className="text-xs text-muted-foreground" data-testid="settings-max-size-saving">
            {t('settings.system.object_file.saving', 'Saving maximum file size...')}
          </span>
        )}
        <Button
          type="button"
          size="sm"
          onClick={() => save(draft)}
          disabled={updateSetting.isPending || draftInvalid}
          data-testid="settings-max-size-save"
        >
          {t('settings.system.object_file.save', 'Save maximum file size')}
        </Button>
      </div>

      {draftInvalid && (
        <p className="text-xs text-destructive" data-testid="settings-max-size-validation">
          {t('settings.system.object_file.validation', 'Enter a whole number of at least 1 MB.')}
        </p>
      )}

      {isSettingsError && (
        <div className="flex items-center gap-2" role="alert">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onReloadSettings}
            data-testid="settings-max-size-load-retry"
          >
            {t('settings.system.object_file.load_retry', 'Reload setting')}
          </Button>
        </div>
      )}

      {!isSettingsError && saveFailed && (
        <div className="flex items-center gap-2" aria-live="polite">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => save(failedValue)}
            data-testid="settings-max-size-save-retry"
          >
            {t('settings.system.object_file.save_retry', 'Save maximum file size again')}
          </Button>
        </div>
      )}
    </>
  )
}
