import { LucideIcon } from '../lib/lucide-icons'

export default function PageHeader({ title, description, icon, children }) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2">
          {icon && <LucideIcon name={icon} size={24} className="text-muted-foreground shrink-0" />}
          <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        </div>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {children}
    </div>
  )
}