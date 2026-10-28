import { DynamicIcon, iconNames } from 'lucide-react/dynamic'

export function LucideIcon({ name, size = 16, className, ...props }) {
  if (!name) return null
  return <DynamicIcon name={name} size={size} className={className} {...props} />
}

export const LUCIDE_ICON_NAMES = iconNames
