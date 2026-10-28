import Loader2 from 'lucide-react/dist/esm/icons/loader-2'
import { cn } from '../lib/utils'
import { useTranslation } from 'react-i18next'

export default function LoadingSpinner({ className = '', size = 32 }) {
  const { t } = useTranslation()
  return (
    <div
      className={cn('flex h-screen items-center justify-center', className)}
      role="status"
      aria-label={t('common.loading', 'Loading...')}
    >
      <Loader2 className="animate-spin text-muted-foreground" size={size} aria-hidden="true" />
    </div>
  )
}
