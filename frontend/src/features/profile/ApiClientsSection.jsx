import { useState } from 'react'
import {
  useApiClients, useCreateApiClient, useRevokeApiClient, useDeleteApiClient,
} from '../../shared/hooks/use-api-clients'
import { extractErrorMessage } from '../../shared/lib/utils'
import EmptyState from '../../shared/components/EmptyState'
import { Button } from '../../shared/components/Button'
import Key from 'lucide-react/dist/esm/icons/key'
import Copy from 'lucide-react/dist/esm/icons/copy'
import Check from 'lucide-react/dist/esm/icons/check'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

function formatExpiresAt(expiresAt, neverLabel) {
  return expiresAt ? new Date(expiresAt).toLocaleString() : neverLabel
}

function TokenRevealPanel({ token, onDone }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(token)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="p-4 border rounded-md bg-muted/30 space-y-3">
      <p className="text-sm font-medium text-destructive">
         {t('profile.api_tokens.copy_warning', 'Copy this token now — it will not be shown again.')}
      </p>
      <div className="flex items-center gap-2">
        <input
          readOnly
          value={token}
          onFocus={(e) => e.target.select()}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm font-mono flex-1 min-w-0"
        />
        <Button type="button" onClick={handleCopy} variant="outline" size="sm"
          leftIcon={copied ? <Check /> : <Copy />}>
           {copied ? t('common.copied', 'Copied') : t('common.copy', 'Copy')}
        </Button>
      </div>
       <Button type="button" onClick={onDone} variant="primary" size="sm">{t('common.done', 'Done')}</Button>
    </div>
  )
}

export default function ApiClientsSection() {
  const { t } = useTranslation()
  const { data: clients = [] } = useApiClients(0, 100)
  const createClient = useCreateApiClient()
  const revokeClient = useRevokeApiClient()
  const deleteClient = useDeleteApiClient()

  const [showCreateForm, setShowCreateForm] = useState(false)
  const [clientName, setClientName] = useState('')
  const [expiryEnabled, setExpiryEnabled] = useState(true)
  const [expiryDays, setExpiryDays] = useState(90)
  const [error, setError] = useState(null)
  const [revealedToken, setRevealedToken] = useState(null)

  const computedExpiresAt = expiryEnabled && expiryDays > 0
    ? new Date(Date.now() + expiryDays * 24 * 60 * 60 * 1000)
    : null

  const resetCreateForm = () => {
    setClientName('')
    setExpiryEnabled(true)
    setExpiryDays(90)
  }

  const handleCreate = async (e) => {
    e.preventDefault()
    setError(null)
    try {
      const created = await createClient.mutateAsync({
        name: clientName,
        expires_at: computedExpiresAt ? computedExpiresAt.toISOString() : null,
      })
      resetCreateForm()
      setShowCreateForm(false)
      setRevealedToken(created.token)
    } catch (err) {
       setError(extractErrorMessage(err, t('profile.api_tokens.create_failed', 'Failed to create API token')))
    }
  }

  const handleRevoke = async (client) => {
     if (!(await confirmDialog({ message: interpolateDefault(t('profile.api_tokens.confirm_revoke', 'Revoke API token "{{name}}"? Any integration using it will stop working immediately.'), { name: client.name }), danger: true }))) return
    revokeClient.mutate(client.id)
  }

  const handleDelete = async (client) => {
     if (!(await confirmDialog({ message: interpolateDefault(t('profile.api_tokens.confirm_delete', 'Permanently delete the revoked token "{{name}}"? This cannot be undone.'), { name: client.name }), danger: true }))) return
    deleteClient.mutate(client.id)
  }

  return (
    <div className="rounded-lg border p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
           <h2 className="text-lg font-semibold">{t('profile.api_tokens.title', 'API Tokens')}</h2>
          <p className="text-sm text-muted-foreground">
             {t('profile.api_tokens.description', 'Bearer tokens for the Public API (/public/api/v1).')}
          </p>
        </div>
        {!revealedToken && (
           <Button onClick={() => setShowCreateForm(true)} variant="outline" size="sm">{t('profile.api_tokens.new', '+ New Token')}</Button>
        )}
      </div>

      {revealedToken && (
        <TokenRevealPanel token={revealedToken} onDone={() => setRevealedToken(null)} />
      )}

      {!revealedToken && showCreateForm && (
        <form onSubmit={handleCreate} className="p-3 border rounded-md bg-muted/30 space-y-3">
          <input
            value={clientName}
            onChange={(e) => setClientName(e.target.value)}
             placeholder={t('common.name', 'Name')}
            required
            className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm w-full"
          />

          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={expiryEnabled}
                onChange={(e) => setExpiryEnabled(e.target.checked)}
                className="rounded border-input"
              />
               {t('profile.api_tokens.automatic_expiry', 'Automatic expiry')}
            </label>

            {expiryEnabled && (
              <div>
                 <label className="block text-xs text-muted-foreground mb-1">{t('profile.api_tokens.days_until_expiry', 'Days until expiry')}</label>
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={expiryDays}
                  onChange={(e) => setExpiryDays(parseInt(e.target.value, 10) || 0)}
                  required
                  className="rounded-md border border-input bg-background text-foreground px-3 py-1.5 text-sm w-32"
                />
                {computedExpiresAt && (
                  <p className="text-xs text-muted-foreground mt-1">
                     {interpolateDefault(t('profile.api_tokens.expires_on', 'The token will expire on {{date}}.'), { date: computedExpiresAt.toLocaleDateString() })}
                  </p>
                )}
              </div>
            )}
            {!expiryEnabled && (
               <p className="text-xs text-muted-foreground">{t('profile.api_tokens.never_expires', 'The token will never expire.')}</p>
            )}
          </div>

          <div className="flex items-center gap-2">
             <Button type="submit" variant="primary" size="sm" loading={createClient.isPending}>{t('common.create', 'Create')}</Button>
            <Button type="button" onClick={() => { setShowCreateForm(false); setError(null); resetCreateForm() }} variant="outline" size="sm">
               {t('common.cancel', 'Cancel')}
            </Button>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </form>
      )}

      {clients.length === 0 && !revealedToken && (
        <EmptyState
          icon={Key}
           title={t('profile.api_tokens.empty_title', 'No API tokens')}
           description={t('profile.api_tokens.empty_description', 'Create a token to authenticate against the Public API.')}
        />
      )}

      {clients.length > 0 && (
        <div className="space-y-2">
          {clients.map((client) => {
            const isRevoked = !!client.revoked_at
            return (
              <div
                key={client.id}
                data-testid="api-client-card"
                className="flex items-center justify-between gap-3 p-3 border rounded-md"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium shrink-0 ${
                    isRevoked
                      ? 'bg-muted text-muted-foreground'
                      : 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                  }`}>
                     {isRevoked ? t('common.status.revoked', 'Revoked') : t('common.status.active', 'Active')}
                  </span>
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">{client.name}</div>
                    <div className="text-xs text-muted-foreground font-mono">
                       {interpolateDefault(t('profile.api_tokens.expires_with_prefix', '{{prefix}}… · expires {{date}}'), { prefix: client.token_prefix, date: formatExpiresAt(client.expires_at, t('common.never', 'Never')) })}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Button
                    onClick={() => handleRevoke(client)}
                    variant="destructive"
                    size="sm"
                    disabled={isRevoked}
                  >
                     {t('common.revoke', 'Revoke')}
                  </Button>
                  {isRevoked && (
                    <Button
                      onClick={() => handleDelete(client)}
                      variant="outline"
                      size="sm"
                    >
                       {t('common.delete', 'Delete')}
                    </Button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
