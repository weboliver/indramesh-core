// Phase 291 (FEEXT-03, D-10): the shared form-control primitives the generic
// parameter_schema form uses and plan 291-06 exports to plugins through the host
// API. They carry the exact CreateTaskModal control metrics (UI-SPEC spacing
// exceptions) so a generic field matches the host fields next to it. Existing
// modals are deliberately not refactored onto these in this phase.
import { cn } from '../lib/utils'

const controlClasses =
  'w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground disabled:opacity-50'

// aria-describedby wiring: the host form renders help/error paragraphs with
// `${id}-help` / `${id}-error`, so a control with an error is also announced.
function describedBy(id, error, help) {
  const ids = []
  if (help) ids.push(`${id}-help`)
  if (error) ids.push(`${id}-error`)
  return ids.length > 0 ? ids.join(' ') : undefined
}

export function FormField({ id, label, required = false, help, error, children }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-sm font-medium text-foreground">
        {label}
        {required && <span className="text-muted-foreground"> *</span>}
      </label>
      {children}
      {help && (
        <p id={`${id}-help`} className="text-xs text-muted-foreground">
          {help}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}

export function TextInput({ id, error, help, className, ...props }) {
  return (
    <input
      id={id}
      type="text"
      className={cn(controlClasses, className)}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy(id, error, help)}
      {...props}
    />
  )
}

export function NumberInput({ id, min, max, step, error, help, className, ...props }) {
  return (
    <input
      id={id}
      type="number"
      min={min}
      max={max}
      step={step}
      className={cn(controlClasses, className)}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy(id, error, help)}
      {...props}
    />
  )
}

export function SelectInput({ id, options = [], placeholder, error, help, className, ...props }) {
  return (
    <select
      id={id}
      className={cn(controlClasses, className)}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy(id, error, help)}
      {...props}
    >
      {placeholder !== undefined && placeholder !== null && <option value="">{placeholder}</option>}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}

export function CheckboxField({ id, label, checked, onChange, help, disabled = false, ...props }) {
  return (
    <>
      <label className="flex items-center gap-2 text-sm">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={onChange}
          disabled={disabled}
          className="rounded border-input bg-background dark:bg-gray-800"
          {...props}
        />
        {label}
      </label>
      {help && (
        <p id={`${id}-help`} className="text-xs text-muted-foreground">
          {help}
        </p>
      )}
    </>
  )
}