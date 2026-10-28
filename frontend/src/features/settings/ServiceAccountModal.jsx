import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../../shared/components/Button'
import { useSources } from '../../shared/hooks/use-sources'
import { useTargets } from '../../shared/hooks/use-targets'
import { useCreateServiceAccount, useUpdateServiceAccount } from '../../shared/hooks/use-other-resources'
import { extractErrorMessage } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'

export default function ServiceAccountModal({ isOpen, onClose, account }) {
  const { t } = useTranslation()
  const isEditing = !!account
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [sourceBearerToken, setSourceBearerToken] = useState('')
  const [targetBearerToken, setTargetBearerToken] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const { data: sourcesData } = useSources(0, 100, { enabled: isOpen })
  const sources = sourcesData?.items || sourcesData || []
  const { data: targetsData } = useTargets(0, 100, { enabled: isOpen })
  const targets = targetsData?.items || targetsData || []

  const createAccount = useCreateServiceAccount()
  const updateAccount = useUpdateServiceAccount()

  useEffect(() => {
    if (isOpen) {
      setName(account?.name || '')
      setUsername(account?.username || '')
      setSourceId(account?.source_id || '')
      setTargetId(account?.target_id || '')
      // Bearer tokens are write-only (encrypted, never returned by the API) —
      // left blank on edit; only sent if the user types a new value.
      setSourceBearerToken('')
      setTargetBearerToken('')
      setError('')
    }
  }, [isOpen, account])

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) {
       setError(t('settings.service_accounts.validation.name_required', 'Name is required'))
      return
    }
    if (!sourceId && !targetId) {
       setError(t('settings.service_accounts.validation.source_or_target', 'Select at least one of Source or Target'))
      return
    }

    setSaving(true)
    setError('')
    try {
      const payload = {
        name: name.trim(),
        username: username.trim() || undefined,
        source_id: sourceId || undefined,
        target_id: targetId || undefined,
      }
      if (sourceId && sourceBearerToken) payload.source_bearer_token = sourceBearerToken
      if (targetId && targetBearerToken) payload.target_bearer_token = targetBearerToken

      if (isEditing) {
        await updateAccount.mutateAsync({ id: account.id, ...payload })
      } else {
        await createAccount.mutateAsync(payload)
      }
      onClose()
    } catch (err) {
       setError(extractErrorMessage(err, t('settings.service_accounts.save_failed', 'Failed to save service account. Check your connection and try again.')))
    } finally {
      setSaving(false)
    }
  }

  if (!isOpen) return null

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center"
      onClick={onClose}
      data-testid="service-account-modal"
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-xl space-y-4 shadow-lg max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
         <h2 className="text-lg font-semibold">{isEditing ? t('settings.service_accounts.edit_title', 'Edit Service Account') : t('settings.service_accounts.create_title', 'Create Service Account')}</h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-md bg-destructive/10 border border-destructive/30 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="space-y-1.5">
            <label className="text-sm font-medium">{t('common.name', 'Name')}</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
               placeholder={t('settings.service_accounts.name_placeholder', 'My Service Account')}
              data-testid="service-account-modal-name"
            />
          </div>

          <div className="space-y-1.5">
             <label className="text-sm font-medium">{t('common.username', 'Username')}</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
               placeholder={t('common.optional', 'Optional')}
              data-testid="service-account-modal-username"
            />
          </div>

           <p className="text-xs text-muted-foreground">{t('settings.service_accounts.scope_hint', 'Select a Source, a Target, or both — a single service account can hold credentials for either or both.')}</p>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('common.source', 'Source')}</label>
              <select
                value={sourceId}
                onChange={(e) => setSourceId(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="service-account-modal-source"
              >
               <option value="">{t('common.none', '— None —')}</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>{source.name}</option>
                ))}
              </select>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('common.target', 'Target')}</label>
              <select
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="service-account-modal-target"
              >
               <option value="">{t('common.none', '— None —')}</option>
                {targets.map((target) => (
                  <option key={target.id} value={target.id}>{target.name}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('settings.service_accounts.source_token', 'Source Bearer Token')}</label>
              <input
                type="password"
                value={sourceBearerToken}
                onChange={(e) => setSourceBearerToken(e.target.value)}
                disabled={!sourceId}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-50"
               placeholder={isEditing ? t('settings.service_accounts.keep_current', 'Leave blank to keep current') : t('common.optional', 'Optional')}
                data-testid="service-account-modal-source-token"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('settings.service_accounts.target_token', 'Target Bearer Token')}</label>
              <input
                type="password"
                value={targetBearerToken}
                onChange={(e) => setTargetBearerToken(e.target.value)}
                disabled={!targetId}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-50"
                placeholder={isEditing ? t('settings.service_accounts.keep_current', 'Leave blank to keep current') : t('common.optional', 'Optional')}
                data-testid="service-account-modal-target-token"
              />
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose} data-testid="service-account-modal-cancel">
               {t('common.cancel', 'Cancel')}
            </Button>
            <Button type="submit" variant="primary" size="sm" loading={saving} data-testid="service-account-modal-submit">
               {isEditing ? t('common.save_changes', 'Save Changes') : t('settings.service_accounts.create_title', 'Create Service Account')}
            </Button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  )
}
