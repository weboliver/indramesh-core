import { useState } from 'react'
import { useUsers, useAssignRole, useRemoveRole } from '../../shared/hooks/use-users'
import AssignmentPicker, { useAssignmentPickerState } from '../../shared/components/AssignmentPicker'
import { useTranslation } from 'react-i18next'

export default function RoleUserAssignmentList({ role, canManage }) {
  const { t } = useTranslation()
  const picker = useAssignmentPickerState()
  const { data, isLoading, isError } = useUsers(picker.q, picker.skip, picker.pageSize, {
    roleId: role.id,
    assigned: picker.assignedParam,
  })
  const users = data?.items || []
  const total = data?.total || 0
  // Direct-role membership, not group-inherited — mirrors the old inline
  // list's `u.roles.includes(role.name)` check. `roles` (name array) is
  // returned on every list item, so no extra request is needed.
  const assignedUserIds = new Set(
    users.filter((u) => (u.roles || []).includes(role.name)).map((u) => u.id)
  )
  const [pendingUserId, setPendingUserId] = useState(null)

  const assignUserRole = useAssignRole()
  const removeUserRole = useRemoveRole()

  const handleToggle = async (user, nextChecked) => {
    if (!canManage) return
    setPendingUserId(user.id)
    try {
      if (nextChecked) {
        await assignUserRole.mutateAsync({ userId: user.id, roleId: role.id })
      } else {
        await removeUserRole.mutateAsync({ userId: user.id, roleId: role.id })
      }
    } finally {
      setPendingUserId(null)
    }
  }

  return (
    <AssignmentPicker
      items={users}
      total={total}
      isLoading={isLoading}
      isError={isError}
      q={picker.q}
      onQChange={picker.setQ}
      filter={picker.filter}
      onFilterChange={picker.setFilter}
      page={picker.page}
      pageSize={picker.pageSize}
      onPageChange={picker.setPage}
      onPageSizeChange={picker.setPageSize}
      assignedIds={assignedUserIds}
      pendingId={pendingUserId}
      canManage={canManage}
      onToggle={handleToggle}
       searchPlaceholder={t('admin.users.search_placeholder', 'Search by username or email')}
       emptyTitle={t('admin.roles.no_users_assigned', 'No users assigned')}
       noResultsTitle={t('admin.users.no_search_results', 'No users match your search')}
      renderLabel={(user) => (
        <>
          <span className="font-semibold">{user.username}</span>
          {user.email ? (
            <span className="text-sm text-muted-foreground">{user.email}</span>
          ) : null}
        </>
      )}
      testIdPrefix="role-user"
    />
  )
}
