import { useState } from 'react'
import { useGroups, useUserGroups, useAssignUserGroup, useRemoveUserGroup } from '../../shared/hooks/use-groups'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { useTranslation } from 'react-i18next'

export default function GroupAssignmentList({ userId, canManage, isSelf, isSuperuser }) {
  const { t } = useTranslation()
  const { data: groupData, isLoading: groupsLoading, isError: groupsError } = useGroups('', null, 0, 100)
  const groups = groupData?.items || []
  const { data: userGroupData, isLoading: userGroupsLoading } = useUserGroups(userId)
  const userGroupIds = new Set((userGroupData || []).map((g) => g.id))
  const [pendingGroupId, setPendingGroupId] = useState(null)

  const assignGroup = useAssignUserGroup()
  const removeGroup = useRemoveUserGroup()

  const isLoading = groupsLoading || userGroupsLoading

  const handleToggle = async (group, nextChecked) => {
    if (!canManage) return
    setPendingGroupId(group.id)
    try {
      if (nextChecked) {
        await assignGroup.mutateAsync({ userId, groupId: group.id })
      } else {
        await removeGroup.mutateAsync({ userId, groupId: group.id })
      }
    } finally {
      setPendingGroupId(null)
    }
  }

  if (isLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (groupsError) {
    return <EmptyState variant="error" />
  }

  if (groups.length === 0) {
    return (
      <EmptyState
        variant="empty"
        title={t('admin.groups.empty_title', 'No groups yet')}
        description={t('admin.groups.empty_description', 'There are no groups yet. Create the first group to get started.')}
      />
    )
  }

  const inNoGroups = userGroupIds.size === 0
  const lastGroupGuard = isSelf && !isSuperuser && userGroupIds.size <= 1

  return (
    <div className="space-y-2" data-testid="user-group-assignment-list">
      {inNoGroups && (
        <p className="text-sm text-muted-foreground">
          {t('admin.users.no_groups_assigned', 'Not in any groups yet.')}
        </p>
      )}
      {groups.map((group) => {
        const checked = userGroupIds.has(group.id)
        const pending = pendingGroupId === group.id
        const lockOwnLast = checked && lastGroupGuard
        const disabled = !canManage || pending || lockOwnLast
        return (
          <label
            key={group.id}
            className="flex items-center gap-2 p-2 border rounded-md text-sm font-normal cursor-pointer hover:bg-muted/50 transition-colors"
          >
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled}
              onChange={(e) => handleToggle(group, e.target.checked)}
              data-testid={`user-group-toggle-${group.id}`}
            />
            <span className="font-semibold">{group.name}</span>
            {lockOwnLast && (
              <span className="text-xs text-muted-foreground ml-auto">
                {t('admin.groups.cannot_remove_own_last', 'Cannot remove your own last access-granting group')}
              </span>
            )}
          </label>
        )
      })}
    </div>
  )
}