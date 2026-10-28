import { cloneElement, isValidElement } from "react"
import Loader2 from "lucide-react/dist/esm/icons/loader-2"
import { cn } from "../lib/utils"
import { LucideIcon } from "../lib/lucide-icons"
import { useTranslation } from 'react-i18next'

const sharedClasses =
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 " +
  "disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none"

const iconOnlyClasses =
  "inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 " +
  "disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none"

const variantClasses = {
  primary:     "bg-primary text-primary-foreground hover:bg-primary/90",
  secondary:   "bg-secondary text-secondary-foreground hover:bg-secondary/80",
  outline:     "border border-input bg-background text-foreground hover:bg-accent hover:text-accent-foreground",
  destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
  ghost:       "hover:bg-accent hover:text-accent-foreground",
  link:        "text-primary underline-offset-4 hover:underline",
}

const sizeClasses = {
  sm:   "h-8 rounded-md px-3 text-xs",
  md:   "h-9 rounded-md px-4 py-2",
  lg:   "h-10 rounded-md px-6",
  icon: "h-9 w-9",
}

const iconSizes = { sm: 14, md: 16, lg: 18, icon: 16 }

export function Button({
  variant = "primary",
  size = "md",
  leftIcon,
  rightIcon,
  loading = false,
  disabled = false,
  asChild = false,
  "data-testid": testId = "button",
  "aria-label": ariaLabel,
  className,
  children,
  ...props
}) {
  const { t } = useTranslation()
  const templateStyle =
    variant === "primary"
      ? { background: "hsl(var(--template-primary, var(--primary)))" }
      : undefined

  const iconSize = iconSizes[size] || 16
  const isDisabled = disabled || loading

  const hasChildren = children !== null && children !== undefined
  const baseClasses = hasChildren ? sharedClasses : iconOnlyClasses

  if (asChild && isValidElement(children)) {
    return cloneElement(children, {
      className: cn(
        sharedClasses,
        variantClasses[variant],
        sizeClasses[size],
        children.props.className,
        className
      ),
      "data-testid": testId,
      style: templateStyle,
      disabled: isDisabled,
      "aria-label": loading ? t('common.loading_short', 'Loading') : ariaLabel,
      ...props,
    })
  }

  return (
    <button
      {...props}
      className={cn(baseClasses, variantClasses[variant], sizeClasses[size], className)}
      style={templateStyle}
      disabled={isDisabled}
      data-testid={testId}
       aria-label={loading ? t('common.loading_short', 'Loading') : ariaLabel}
    >
      {!loading && leftIcon && <LucideIcon name={leftIcon} size={iconSize} />}
      {hasChildren ? (
        <span className="relative inline-flex items-center justify-center">
          {loading && (
            <span className="absolute inset-0 flex items-center justify-center">
              <Loader2 className="animate-spin" size={iconSize} />
            </span>
          )}
          <span className={loading ? "opacity-0" : undefined}>{children}</span>
        </span>
      ) : (
        loading && (
          <span className="inline-flex items-center justify-center">
            <Loader2 className="animate-spin" size={iconSize} />
          </span>
        )
      )}
      {!loading && rightIcon && <LucideIcon name={rightIcon} size={iconSize} />}
    </button>
  )
}
