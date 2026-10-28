import { useState } from 'react'
import { useGroups, useRoleGroups, useAssignGroupRole, useRemoveGroupRole } from '../../shared/hooks/use-groups'
import AssignmentPicker, { useAssignmentPickerState } from '../../shared/components/AssignmentPicker'
import { useTranslation } from 'react-i18next'

export default function RoleGroupAssignmentList({ role, canManage }) {
  const { t } = useTranslation()
  const picker = useAssignmentPickerState()
  const { data, isLoading, isError } = useGroups(picker.q, null, picker.skip, picker.pageSize, {
    roleId: role.id,
    assigned: picker.assignedParam,
  })
  const groups = data?.items || []
  const total = data?.total || 0
  // GroupResponse carries no role-assignment info, unlike UserAdminResponse's
  // `roles` field — resolve the assigned set from the dedicated
  // GET /roles/{id}/groups endpoint instead (ground truth, independent of
  // the current page's filter/search).
  const { data: roleGroupData } = useRoleGroups(role.id)
  const assignedGroupIds = new Set((roleGroupData || []).map((g) => g.id))
  const [pendingGroupId, setPendingGroupId] = useState(null)

  const assignGroupRole = useAssignGroupRole()
  const removeGroupRole = useRemoveGroupRole()

  const handleToggle = async (group, nextChecked) => {
    if (!canManage) return
    setPendingGroupId(group.id)
    try {
      if (nextChecked) {
        await assignGroupRole.mutateAsync({ groupId: group.id, roleId: role.id })
      } else {
        await removeGroupRole.mutateAsync({ groupId: group.id, roleId: role.id })
      }
    } finally {
      setPendingGroupId(null)
    }
  }

  return (
    <AssignmentPicker
      items={groups}
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
      assignedIds={assignedGroupIds}
      pendingId={pendingGroupId}
      canManage={canManage}
      onToggle={handleToggle}
       searchPlaceholder={t('admin.groups.search_placeholder', 'Search by group name')}
       emptyTitle={t('admin.roles.no_groups_assigned', 'No groups assigned')}
       noResultsTitle={t('admin.groups.no_search_results', 'No groups match your search')}
      renderLabel={(group) => <span className="font-semibold">{group.name}</span>}
      testIdPrefix="role-group"
    />
  )
}
