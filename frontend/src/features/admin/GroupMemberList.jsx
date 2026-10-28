import { useState } from 'react'
import { useUserPicker } from '../../shared/hooks/use-users'
import { useGroupMembers, useAssignGroupMember, useRemoveGroupMember } from '../../shared/hooks/use-groups'
import AssignmentPicker, { useAssignmentPickerState } from '../../shared/components/AssignmentPicker'
import { useTranslation } from 'react-i18next'

export default function GroupMemberList({ groupId, canManage }) {
  const { t } = useTranslation()
  const picker = useAssignmentPickerState()
  // Content-gated /users/picker (objects:read) — Editors hold groups:manage
  // but not users:read, so GET /users/ would 403 for the exact tier this UI
  // targets (verifier gap CR-01).
  const { data: userData, isLoading: usersLoading, isError: usersError } = useUserPicker(
    picker.q, undefined, picker.skip, picker.pageSize,
    { groupId, assigned: picker.assignedParam }
  )
  const users = userData?.items || []
  const total = userData?.total || 0
  // The picker endpoint is deliberately lean (no membership info per row) —
  // resolve ground-truth membership from GET /groups/{id}/members instead,
  // capped at MAX_LIMIT so a large group's checkbox state stays correct
  // regardless of which page/filter is currently displayed.
  const { data: memberData, isLoading: membersLoading } = useGroupMembers(groupId, 0, 1000)
  const memberIds = new Set((memberData || []).map((m) => m.id))
  const [pendingUserId, setPendingUserId] = useState(null)

  const assignMember = useAssignGroupMember()
  const removeMember = useRemoveGroupMember()

  const isLoading = usersLoading || membersLoading

  const handleToggle = async (user, nextChecked) => {
    if (!canManage) return
    setPendingUserId(user.id)
    try {
      if (nextChecked) {
        await assignMember.mutateAsync({ groupId, userId: user.id })
      } else {
        await removeMember.mutateAsync({ groupId, userId: user.id })
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
      isError={usersError}
      q={picker.q}
      onQChange={picker.setQ}
      filter={picker.filter}
      onFilterChange={picker.setFilter}
      page={picker.page}
      pageSize={picker.pageSize}
      onPageChange={picker.setPage}
      onPageSizeChange={picker.setPageSize}
      assignedIds={memberIds}
      pendingId={pendingUserId}
      canManage={canManage}
      onToggle={handleToggle}
       searchPlaceholder={t('admin.users.search_placeholder', 'Search by username or email')}
       emptyTitle={t('admin.groups.no_members', 'No members in this group yet.')}
       emptyDescription={t('admin.groups.no_members_hint', 'Assign users below to grant them this group’s access.')}
       noResultsTitle={t('admin.users.no_search_results', 'No users match your search')}
      renderLabel={(user) => (
        <>
          <span className="font-semibold">{user.username}</span>
          {user.email ? (
            <span className="text-sm text-muted-foreground">{user.email}</span>
          ) : null}
        </>
      )}
      testIdPrefix="group-member"
    />
  )
}
