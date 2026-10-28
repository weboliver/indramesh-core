import { cn } from '../lib/utils'

export default function SegmentedControl({
  options,
  active,
  onChange,
  name,
  className,
}) {
  return (
    <div
      className={cn('flex', className)}
      data-testid={name ? `segmented-control-${name}` : 'segmented-control'}
      role="tablist"
    >
      {options.map((option) => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          role="tab"
          aria-selected={active === option.value}
          data-testid={
            name
              ? `segmented-${name}-${option.value}`
              : `segmented-${option.value}`
          }
          className={cn(
            'px-4 py-2 text-sm border-b-2 transition-colors',
            active === option.value
              ? 'border-primary font-semibold text-foreground'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
