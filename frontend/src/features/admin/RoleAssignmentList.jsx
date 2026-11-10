import { useState } from 'react'
import { useRoles } from '../../shared/hooks/use-roles'
import { useAssignRole, useRemoveRole } from '../../shared/hooks/use-users'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { useTranslation } from 'react-i18next'

export default function RoleAssignmentList({
  userId,
  assignedRoleNames,
  canManage,
  disableRemoveGuard,
}) {
  const { t } = useTranslation()
  const { data: roleData, isLoading, isError } = useRoles(0, 100, { includeTotal: true })
  const roles = roleData?.items || []
  const [pendingRoleId, setPendingRoleId] = useState(null)

  const assignRole = useAssignRole()
  const removeRole = useRemoveRole()

  const handleToggle = async (role, nextChecked) => {
    if (!canManage) return
    setPendingRoleId(role.id)
    try {
      if (nextChecked) {
        await assignRole.mutateAsync({ userId, roleId: role.id })
      } else {
        await removeRole.mutateAsync({ userId, roleId: role.id })
      }
    } finally {
      setPendingRoleId(null)
    }
  }

  if (isLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (isError) {
    return <EmptyState variant="error" />
  }

  if (roles.length === 0) {
    return (
      <EmptyState
        variant="empty"
         title={t('admin.roles.empty_title', 'No roles yet')}
         description={t('admin.roles.no_roles_description', 'There are no roles defined yet.')}
      />
    )
  }

  return (
    <div className="space-y-2" data-testid="role-assignment-list">
      {roles.map((role) => {
        const checked = (assignedRoleNames || []).includes(role.name)
        const pending = pendingRoleId === role.id
        const lastRoleLock = checked && disableRemoveGuard
        // SESS-06 suppresses ASSIGNMENT only: a role the caller may not grant
        // must stay un-checkable, but an already-assigned stronger role (added
        // earlier by a superuser) stays removable — the API accepts that.
        const disabled = !canManage || pending || lastRoleLock || (role.assignable === false && !checked)
        return (
          <label
            key={role.id}
            className="flex items-center gap-2 p-2 border rounded-md text-sm font-normal cursor-pointer hover:bg-muted/50 transition-colors"
            data-testid="role-assignment-row"
          >
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled}
              onChange={(e) => handleToggle(role, e.target.checked)}
              data-testid={`role-toggle-${role.id}`}
            />
            <span className="font-semibold">{role.name}</span>
            {role.is_system && (
              <LucideIcon
                name="shield"
                size={14}
                className="text-muted-foreground"
                 aria-label={t('admin.roles.system_role_label', 'System role — cannot be deleted')}
                data-testid="system-role-badge"
              />
            )}
            {lastRoleLock && (
              <span className="text-xs text-muted-foreground ml-auto">
                 {t('admin.roles.cannot_remove_last', 'Cannot remove your own last role')}
              </span>
            )}
          </label>
        )
      })}
    </div>
  )
}
