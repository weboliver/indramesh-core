import { useIsFetching } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

export default function LoadingBar() {
  const isFetching = useIsFetching()
  const { t } = useTranslation()

  if (isFetching === 0) return null

  return (
    <div
      data-testid="loading-bar"
      role="progressbar"
      aria-label={t('common.loading_short', 'Loading')}
      className="fixed top-0 left-0 right-0 z-50 h-0.5"
    >
      <div className="h-full w-1/4 bg-primary animate-indeterminate" />
    </div>
  )
}
