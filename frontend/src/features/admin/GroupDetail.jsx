import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useGroup, useDeleteGroup } from '../../shared/hooks/use-groups'
import { Can, useCan } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import PageHeader from '../../shared/components/PageHeader'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import EmptyState from '../../shared/components/EmptyState'
import TableSkeleton from '../../shared/components/TableSkeleton'
import GroupMemberList from './GroupMemberList'
import GroupRoleAssignmentList from './GroupRoleAssignmentList'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

function GroupDetailContent() {
  const { t } = useTranslation()
  const { id } = useParams()
  const navigate = useNavigate()
  const { data: group, isLoading, isError } = useGroup(id)
  const deleteGroup = useDeleteGroup()
  const canManageMembers = useCan('groups', 'manage')
  const canManageRoles = useCan('roles', 'manage')

  const [deleting, setDeleting] = useState(false)

  if (isLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (isError || !group) {
    return (
      <EmptyState
        variant="error"
        title={t('admin.groups.not_found_title', 'Group not found')}
        description={t('admin.groups.not_found_description', 'This group could not be loaded.')}
      />
    )
  }

  const handleDelete = async () => {
    if (!(await confirmDialog({ message: interpolateDefault(t('admin.groups.confirm_delete', 'Delete group "{{name}}"? This cannot be undone.'), { name: group.name }), danger: true }))) return
    setDeleting(true)
    try {
      await deleteGroup.mutateAsync(group.id)
      navigate('/groups')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6" data-testid="group-detail">
      <Breadcrumb items={[
        { label: t('admin.groups.title', 'Groups'), href: '/groups' },
        { label: group.name },
      ]} />
      <PageHeader title={group.name} icon="users-round">
        <Can resource="groups" action="delete">
          <Button
            variant="destructive"
            size="sm"
            onClick={handleDelete}
            disabled={deleting}
            loading={deleting}
          >
            {t('common.delete', 'Delete')}
          </Button>
        </Can>
      </PageHeader>

      <div className="space-y-2">
        <h2 className="text-sm font-semibold">{t('admin.groups.members_of', 'Members of {{name}}', { name: group.name })}</h2>
        <GroupMemberList groupId={group.id} canManage={canManageMembers} groupName={group.name} />
      </div>

      <div className="space-y-2">
        <h2 className="text-sm font-semibold">{t('admin.groups.assigned_roles_of', 'Assigned Roles of {{name}}', { name: group.name })}</h2>
        <GroupRoleAssignmentList groupId={group.id} canManage={canManageRoles} groupName={group.name} />
      </div>
    </div>
  )
}

export default function GroupDetail() {
  const { t } = useTranslation()
  const canRead = useCan('groups', 'read')

  return (
    <ErrorBoundary>
      <Can resource="groups" action="read">
        <GroupDetailContent />
      </Can>
      {!canRead && (
        <EmptyState
          variant="error"
          title={t('common.access_denied', 'Access denied')}
          description={t('admin.groups.access_denied_description', 'You do not have permission to view groups.')}
        />
      )}
    </ErrorBoundary>
  )
}