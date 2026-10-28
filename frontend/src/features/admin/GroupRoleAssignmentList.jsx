import { useState } from 'react'
import { useRoles } from '../../shared/hooks/use-roles'
import { useGroupRoles, useAssignGroupRole, useRemoveGroupRole } from '../../shared/hooks/use-groups'
import AssignmentPicker, { useAssignmentPickerState } from '../../shared/components/AssignmentPicker'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useTranslation } from 'react-i18next'

export default function GroupRoleAssignmentList({ groupId, canManage }) {
  const { t } = useTranslation()
  const picker = useAssignmentPickerState()
  const { data, isLoading, isError } = useRoles(picker.skip, picker.pageSize, {
    includeTotal: true,
    extraParams: { q: picker.q || undefined, group_id: groupId, assigned: picker.assignedParam },
  })
  const roles = data?.items || []
  const total = data?.total || 0
  // RoleListResponse carries no group-assignment info — resolve the assigned
  // set from GET /groups/{id}/roles (ground truth, independent of the
  // current page's filter/search), mirroring RoleGroupAssignmentList.
  const { data: groupRoleData, isLoading: groupRolesLoading } = useGroupRoles(groupId)
  const assignedRoleIds = new Set((groupRoleData || []).map((r) => r.id))
  const [pendingRoleId, setPendingRoleId] = useState(null)

  const assignRole = useAssignGroupRole()
  const removeRole = useRemoveGroupRole()

  const handleToggle = async (role, nextChecked) => {
    if (!canManage) return
    setPendingRoleId(role.id)
    try {
      if (nextChecked) {
        await assignRole.mutateAsync({ groupId, roleId: role.id })
      } else {
        await removeRole.mutateAsync({ groupId, roleId: role.id })
      }
    } finally {
      setPendingRoleId(null)
    }
  }

  return (
    <AssignmentPicker
      items={roles}
      total={total}
      isLoading={isLoading || groupRolesLoading}
      isError={isError}
      q={picker.q}
      onQChange={picker.setQ}
      filter={picker.filter}
      onFilterChange={picker.setFilter}
      page={picker.page}
      pageSize={picker.pageSize}
      onPageChange={picker.setPage}
      onPageSizeChange={picker.setPageSize}
      assignedIds={assignedRoleIds}
      pendingId={pendingRoleId}
      canManage={canManage}
      onToggle={handleToggle}
       searchPlaceholder={t('admin.roles.search_placeholder', 'Search by role name')}
       emptyTitle={t('admin.roles.empty_title', 'No roles yet')}
       emptyDescription={t('admin.roles.no_roles_description', 'There are no roles defined yet.')}
       noResultsTitle={t('admin.roles.no_search_results', 'No roles match your search')}
      renderLabel={(role) => (
        <>
          <span className="font-semibold">{role.name}</span>
          {role.is_system && (
            <LucideIcon
              name="shield"
              size={14}
              className="text-muted-foreground"
               aria-label={t('admin.roles.system_role_label', 'System role — cannot be deleted')}
            />
          )}
        </>
      )}
      testIdPrefix="group-role"
    />
  )
}
