import { useState } from 'react'
import { cn } from '../../shared/lib/utils'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const ACTION_OPTIONS = [
  { value: 'read' },
  { value: 'write' },
  { value: 'delete' },
]

function actionLabel(value, t) {
  switch (value) {
    case 'write': return t('permissions.action.write', 'Write')
    case 'delete': return t('permissions.action.delete', 'Delete')
    default: return t('permissions.action.read', 'Read')
  }
}

function roleName(roles, roleId) {
  const role = roles?.find(r => r.id === roleId)
  return role?.name || roleId
}

function OriginTag({ item, t }) {
  if (item.is_direct) {
    return (
      <span className="inline-flex items-center rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
         {t('permissions.direct', 'Direct')}
      </span>
    )
  }
   const level = item.source_entity_type ? item.source_entity_type.replace(/_/g, ' ') : t('permissions.ancestor', 'ancestor')
  return (
    <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
       {interpolateDefault(t('permissions.inherited_from', 'Inherited (from {{level}})'), { level })}
    </span>
  )
}

function AllowDenyBadge({ isDeny, t }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
        isDeny
          ? 'bg-destructive/10 text-destructive'
          : 'bg-green-500/10 text-green-700 dark:text-green-400'
      )}
    >
       {isDeny ? t('permissions.deny', 'Deny') : t('permissions.allow', 'Allow')}
    </span>
  )
}

export function RecordPermissionForm({ entityType, recordId, onCreate, onDelete, roles = [], existingItems = [] }) {
  const { t } = useTranslation()
  const [roleId, setRoleId] = useState('')
  const [action, setAction] = useState('read')
  const [isDeny, setIsDeny] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [deletingId, setDeletingId] = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!roleId) return
    setSubmitting(true)
    setError(null)
    try {
      await onCreate({
        role_id: roleId,
        record_entity_type: entityType,
        record_id: recordId,
        action,
        is_deny: isDeny,
      })
      setRoleId('')
      setAction('read')
      setIsDeny(false)
    } catch (err) {
       setError(err?.response?.data?.detail || t('permissions.add_failed', 'Failed to add permission'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async (item) => {
    if (!item.id) return
    setDeletingId(item.id)
    try {
      await onDelete(item.id)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
         <h4 className="text-sm font-semibold text-foreground">{t('permissions.effective_title', 'Effective Permissions')}</h4>
        <div className="rounded-md border divide-y" data-testid="effective-permissions-list">
          {existingItems.length === 0 && (
             <p className="p-3 text-sm text-muted-foreground">{t('permissions.no_entries', 'No permission entries yet.')}</p>
          )}
          {existingItems.map((item, idx) => (
            <div
              key={`${item.role_id}-${item.action}-${item.is_deny}-${idx}`}
              className="flex items-center justify-between gap-2 p-2 text-sm"
              data-testid="permission-row"
            >
              <div className="flex flex-1 flex-wrap items-center gap-2">
                <span className="font-medium">{roleName(roles, item.role_id)}</span>
                <span className="text-muted-foreground">{item.action}</span>
                 <AllowDenyBadge isDeny={item.is_deny} t={t} />
                 <OriginTag item={item} t={t} />
              </div>
              {item.is_direct && (
                <button
                  type="button"
                  onClick={() => handleDelete(item)}
                  disabled={deletingId === item.id}
                  className="text-muted-foreground hover:text-destructive disabled:opacity-50"
                   aria-label={t('permissions.remove', 'Remove permission')}
                  data-testid="permission-delete-button"
                >
                  <LucideIcon name="trash-2" size={16} />
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-3 rounded-md border p-3">
         <h4 className="text-sm font-semibold text-foreground">{t('permissions.add_title', 'Add Permission')}</h4>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <label htmlFor="permission-role" className="text-xs font-medium text-muted-foreground">
               {t('common.role', 'Role')}
            </label>
            <select
              id="permission-role"
              value={roleId}
              onChange={e => setRoleId(e.target.value)}
              required
              className="w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
              data-testid="permission-role-select"
            >
               <option value="" disabled>{t('permissions.select_role', 'Select role...')}</option>
              {roles.map(role => (
                <option key={role.id} value={role.id}>{role.name}</option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="permission-action" className="text-xs font-medium text-muted-foreground">
               {t('common.action', 'Action')}
            </label>
            <select
              id="permission-action"
              value={action}
              onChange={e => setAction(e.target.value)}
              className="w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
              data-testid="permission-action-select"
            >
              {ACTION_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{actionLabel(opt.value, t)}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex items-center gap-4">
           <span className="text-xs font-medium text-muted-foreground">{t('permissions.effect', 'Effect')}</span>
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="permission-effect"
              checked={!isDeny}
              onChange={() => setIsDeny(false)}
            />
             {t('permissions.allow', 'Allow')}
          </label>
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="permission-effect"
              checked={isDeny}
              onChange={() => setIsDeny(true)}
            />
             {t('permissions.deny', 'Deny')}
          </label>
        </div>

        <div className="flex justify-end">
          <Button
            type="submit"
            variant="primary"
            size="sm"
            disabled={!roleId || submitting}
            loading={submitting}
            data-testid="permission-add-button"
          >
             {t('common.add', 'Add')}
          </Button>
        </div>
      </form>
    </div>
  )
}
