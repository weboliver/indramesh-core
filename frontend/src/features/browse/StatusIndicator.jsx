import { cn } from "../../shared/lib/utils"
import { useTranslation } from 'react-i18next'

const STATUS_DOT_COLORS = {
  Active:   "bg-green-500",
  Pending:  "bg-yellow-500",
  Inactive: "bg-red-500",
}

export function StatusIndicator({ value, className }) {
  const { t } = useTranslation()
  if (!value) {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-muted-foreground", className)}>
        <span className="h-2 w-2 rounded-full bg-muted-foreground/40" />
        <span className="text-xs">{t('common.none', 'None')}</span>
      </span>
    )
  }

  // User-defined color → luminance-based dot color
  if (value.color) {
    return (
      <span className={cn("inline-flex items-center gap-1.5", className)}>
        <span
          className="h-2 w-2 rounded-full"
          style={{ backgroundColor: value.color }}
        />
        <span className="text-sm">{value.name}</span>
      </span>
    )
  }

  // Fixed category → Tailwind classes (dark: variants come from base colors)
  const dotClass = STATUS_DOT_COLORS[value.status_category] || "bg-gray-500"

  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <span className={cn("h-2 w-2 rounded-full", dotClass)} />
      <span className="text-sm">{value.name}</span>
    </span>
  )
}
