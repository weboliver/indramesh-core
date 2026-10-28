import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSystemSettings } from './use-other-resources'

export function useFormatDate() {
  const { t } = useTranslation()
  const { data: settings = {} } = useSystemSettings()
  const tz = settings.default_timezone || 'UTC'

  return useMemo(() => {
    const formatter = new Intl.DateTimeFormat(undefined, {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })

    return (dateStr) => {
      if (!dateStr) return t('common.date_unavailable', '-')
      const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z')
      return formatter.format(d)
    }
  }, [t, tz])
}

// Phase 300 (D-15): `HH:mm:ss` in the configured system timezone, for the log
// row time column. Same naive-timestamp `Z` handling as useFormatDate.
export function useFormatTime() {
  const { t } = useTranslation()
  const { data: settings = {} } = useSystemSettings()
  const tz = settings.default_timezone || 'UTC'

  return useMemo(() => {
    const formatter = new Intl.DateTimeFormat(undefined, {
      timeZone: tz,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })

    return (dateStr) => {
      if (!dateStr) return t('common.date_unavailable', '-')
      const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z')
      return formatter.format(d)
    }
  }, [t, tz])
}

// Phase 300: full date-time with seconds for the expanded line details
// (UI-SPEC §Expanded line details), same timezone source.
export function useFormatDateTime() {
  const { t } = useTranslation()
  const { data: settings = {} } = useSystemSettings()
  const tz = settings.default_timezone || 'UTC'

  return useMemo(() => {
    const formatter = new Intl.DateTimeFormat(undefined, {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })

    return (dateStr) => {
      if (!dateStr) return t('common.date_unavailable', '-')
      const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z')
      return formatter.format(d)
    }
  }, [t, tz])
}
