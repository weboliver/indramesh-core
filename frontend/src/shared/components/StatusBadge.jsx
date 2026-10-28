import { cn } from "../lib/utils"
import { getContrastStyle } from "../lib/color-utils"

const CATEGORY_STYLES = {
  Active: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  Pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
  Inactive: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
}

export function StatusBadge({ value, className }) {
  if (!value) return null

  // D-10: User-defined color → luminance-based text contrast
  if (value.color) {
    return (
      <span
        className={cn(
          "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
          className
        )}
        style={getContrastStyle(value.color)}
        data-testid="status-badge"
      >
        {value.name}
      </span>
    )
  }

  // Fixed-category styles → use Tailwind dark: variants (D-08, D-09)
  const categoryStyle = CATEGORY_STYLES[value.status_category]
    || "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300"

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        categoryStyle,
        className
      )}
      data-testid="status-badge"
    >
      {value.name}
    </span>
  )
}
