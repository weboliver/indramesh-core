import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

function weekdayLabel(t, day) {
  switch (day) {
    case 0: return t('settings.schedule.weekday.sunday', 'Sunday')
    case 1: return t('settings.schedule.weekday.monday', 'Monday')
    case 2: return t('settings.schedule.weekday.tuesday', 'Tuesday')
    case 3: return t('settings.schedule.weekday.wednesday', 'Wednesday')
    case 4: return t('settings.schedule.weekday.thursday', 'Thursday')
    case 5: return t('settings.schedule.weekday.friday', 'Friday')
    case 6: return t('settings.schedule.weekday.saturday', 'Saturday')
    default: return t('settings.schedule.weekday.sunday', 'Sunday')
  }
}

export function formatRecurrence(schedule, t = (_, defaultValue) => defaultValue) {
  if (!schedule) return t('settings.schedule.recurrence.custom', 'Custom: -')

  const { preset_type, args, cron_expression } = schedule

  switch (preset_type) {
    case 'EVERY_N_MINUTES': {
      const n = args?.interval_minutes ?? 0
      return selectPluralKey('settings.schedule.recurrence.every_minutes', n) === 'settings.schedule.recurrence.every_minutes_one'
        ? interpolateDefault(t('settings.schedule.recurrence.every_minutes_one', 'Every {{count}} minute'), { count: n })
        : interpolateDefault(t('settings.schedule.recurrence.every_minutes_other', 'Every {{count}} minutes'), { count: n })
    }
    case 'EVERY_N_HOURS': {
      const n = args?.interval_hours ?? 0
      return selectPluralKey('settings.schedule.recurrence.every_hours', n) === 'settings.schedule.recurrence.every_hours_one'
        ? interpolateDefault(t('settings.schedule.recurrence.every_hours_one', 'Every {{count}} hour'), { count: n })
        : interpolateDefault(t('settings.schedule.recurrence.every_hours_other', 'Every {{count}} hours'), { count: n })
    }
    case 'DAILY': {
      const hh = String(args?.hour ?? 0).padStart(2, '0')
      const mm = String(args?.minute ?? 0).padStart(2, '0')
      return interpolateDefault(t('settings.schedule.recurrence.daily_at', 'Daily at {{time}}'), { time: `${hh}:${mm}` })
    }
    case 'WEEKLY': {
      const hh = String(args?.hour ?? 0).padStart(2, '0')
      const mm = String(args?.minute ?? 0).padStart(2, '0')
      const day = weekdayLabel(t, args?.day_of_week ?? 0)
      return interpolateDefault(t('settings.schedule.recurrence.weekly_on', 'Weekly on {{day}} at {{time}}'), {
        day,
        time: `${hh}:${mm}`,
      })
    }
    default:
      return interpolateDefault(t('settings.schedule.recurrence.custom_expression', 'Custom: {{expression}}'), {
        expression: cron_expression || '-',
      })
  }
}
