import FolderOpen from 'lucide-react/dist/esm/icons/folder-open'
import AlertTriangle from 'lucide-react/dist/esm/icons/alert-triangle'
import SearchX from 'lucide-react/dist/esm/icons/search-x'
import { Button } from './Button'
import { useTranslation } from 'react-i18next'
import { PHASE231_AUTO_BUTTONS, PHASE231_EMPTY_STATE_DEFAULTS } from '../lib/phase231-dynamic-contract'

const VARIANT_DEFAULTS = {
  empty: { ...PHASE231_EMPTY_STATE_DEFAULTS.empty, icon: FolderOpen },
  error: { ...PHASE231_EMPTY_STATE_DEFAULTS.error, icon: AlertTriangle },
  'no-results': { ...PHASE231_EMPTY_STATE_DEFAULTS['no-results'], icon: SearchX },
}

export default function EmptyState({
  variant,
  icon: IconProp,
  title: titleProp,
  description: descProp,
  buttonLabel,
  onButtonClick,
  'data-testid': testId = 'empty-state',
}) {
  const { t } = useTranslation()
  const defaults = VARIANT_DEFAULTS[variant] || {}
  const Icon = IconProp || defaults.icon
  const title = titleProp || (defaults.titleKey ? t(defaults.titleKey, defaults.title) : defaults.title)
  const description = descProp || (defaults.descriptionKey ? t(defaults.descriptionKey, defaults.description) : defaults.description)

   const autoBtn = PHASE231_AUTO_BUTTONS[variant]
  const showAutoBtn = autoBtn && onButtonClick

  const iconColorClass = variant === 'error' ? 'text-destructive/70' : 'text-muted-foreground'

  return (
    <div
      className="rounded-lg border border-dashed p-12 text-center"
      data-testid={testId}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {Icon && <Icon className={`mx-auto h-12 w-12 ${iconColorClass}`} aria-hidden="true" />}
      {title && <h3 className="mt-4 text-lg font-semibold">{title}</h3>}
      {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      {showAutoBtn && (
        <Button onClick={onButtonClick} variant={autoBtn.variant} size="md" className="mt-4">
          {t(autoBtn.key, autoBtn.label)}
        </Button>
      )}
      {!showAutoBtn && buttonLabel && onButtonClick && (
        <Button onClick={onButtonClick} variant="primary" size="md" className="mt-4">
          {buttonLabel}
        </Button>
      )}
    </div>
  )
}
